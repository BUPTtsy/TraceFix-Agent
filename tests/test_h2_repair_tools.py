import copy

import pytest
from pydantic import ValidationError

from tracefix.execution.repository import safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.runtime.contracts import DiagnosisDraft, FinishRepair, PatchProposal, Phase, digest
from tracefix.runtime.diagnosis import DiagnosisDraft as LegacyDiagnosisDraft
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import ToolOperationUnknown, model_tool_name
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.repair import RevertOverlayInput


@pytest.fixture
def repair(tmp_path, request):
    engine, state = make_engine(tmp_path / 'run')
    project = engine.scopes.projects[state.scope_id]
    case = getattr(request.node, 'callspec', None)
    file_count = 9 if case and case.params.get('kind') == 'files' else 2
    for index in range(file_count):
        (project.root / 'src' / f'value{index}.py').write_bytes(b'value = 1\n')
    safe_index_files(project.root, [f'src/value{index}.py' for index in range(file_count)])
    commit_workspace(project.root, '混合修复工具测试基线')
    context = engine.scopes.context(state.scope_id)
    workspace, source = Workspace.export(engine.scopes, context, 'HEAD', tmp_path / 'workspace')
    engine.workspace, engine.source, engine.context = workspace, source, context
    engine.browser.workspace = engine.model.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, source)
    state.mode, state.phase, state.repair_mode = 'repair', Phase.DIAGNOSE, 'hybrid'
    state.patch_base_commit = workspace.head()
    evidence = engine.artifacts.put(state.scope_id, state.run_id, {'reproduced': True})
    state.evidence_refs = [evidence]
    engine.store.save(state)
    return engine, state


def stage(engine, path='src/value0.py', content='value = 2\n'):
    engine.workspace.stage(path, content)


def test_finish_contract_and_tool_names(repair):
    assert LegacyDiagnosisDraft is DiagnosisDraft
    assert FinishRepair(summary='修复').diagnosis is None
    for injected in ({'edits': []}, {'proposal': {}}, {'before_hash': 'fake'}):
        with pytest.raises(ValidationError):
            FinishRepair(summary='修复', **injected)
    for selection in ({}, {'all': True, 'paths': ['src/value0.py']}):
        with pytest.raises(ValidationError):
            RevertOverlayInput(**selection)
    engine, state = repair
    runtime = build_runtime_tools(engine, state, FinishRepair)
    assert runtime.registry.contains('finish_repair')
    assert runtime.registry.contains('RevertOverlay')
    assert not runtime.registry.contains('propose_patch')
    assert not build_runtime_tools(engine, state, PatchProposal).registry.contains('propose_patch')
    assert model_tool_name('finish_repair') == 'FinishRepair'
    worker = build_runtime_tools(engine, state, FinishRepair, {'worker_depth': 1,
        'worker_allowed_files': ['src/value0.py'], 'worker_write_enabled': True,
        'worker_allowed_tools': ['finish_repair', 'RevertOverlay']})
    assert not worker.registry.contains('finish_repair') and not worker.registry.contains('RevertOverlay')


@pytest.mark.asyncio
async def test_finish_materializes_actual_overlay_without_applying_or_verifying(repair):
    engine, state = repair
    before = engine.workspace.path('src/value0.py').read_bytes()
    stage(engine)
    runtime = build_runtime_tools(engine, state, FinishRepair)
    pipeline = runtime.pipeline()
    result = await pipeline.execute('FinishRepair', {'summary': '修复取值'}, 'finish')
    assert not result.is_error
    proposal = pipeline.submission_value
    assert isinstance(proposal, PatchProposal)
    assert proposal == runtime.submissions['finish_repair']
    assert proposal.edits[0].before_hash == digest(before)
    assert proposal.edits[0].content == 'value = 2\n'
    assert proposal.evidence_refs == state.evidence_refs
    assert engine.workspace.path('src/value0.py').read_bytes() == before
    assert state.phase == Phase.DIAGNOSE and not state.validation_refs
    assert result.result['candidate_fingerprint'] == engine.candidate_fingerprint(proposal)
    assert result.result['normalized_diff_fingerprint']
    assert state.loop_evidence['repair']['history'][-1]['accepted']


@pytest.mark.asyncio
async def test_large_finish_submission_survives_shaping_and_operation_replay(repair):
    engine, state = repair
    content = 'value = 2\n' + '# 足够大的合法候选\n' * 5000
    stage(engine, content=content)
    runtime = build_runtime_tools(engine, state, FinishRepair)
    store_artifact = lambda value: engine.artifacts.put(state.scope_id, state.run_id, value)
    pipeline = runtime.pipeline(store_artifact=store_artifact)
    result = await pipeline.execute('FinishRepair', {'summary': '修复大文件'}, 'big-finish')
    assert not result.is_error and pipeline.submission_value.edits[0].content == content
    # 新 RuntimeTools 无进程内 submissions，必须从已保存的完整 operation 回执恢复。
    replay = build_runtime_tools(engine, state, FinishRepair).pipeline(store_artifact=store_artifact)
    replay_result = await replay.execute('FinishRepair', {'summary': '修复大文件'}, 'big-finish')
    assert not replay_result.is_error and replay.submission_value.edits[0].content == content


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,code', [('empty', 'NO_CHANGE'), ('noop', 'NO_CHANGE'),
    ('syntax', 'NEW_SYNTAX_ERROR'), ('drift', 'DISK_DRIFT'), ('files', 'TOO_MANY_FILES'),
    ('repeated', 'FAILED_CANDIDATE_REPEATED'), ('protected', 'protected_path')])
async def test_finish_rejections_return_to_same_segment_and_never_unknown(repair, monkeypatch, kind, code):
    engine, state = repair
    unknown = []
    monkeypatch.setattr(engine.store, 'mark_unknown', lambda *args, **kwargs: unknown.append(args))
    if kind == 'noop':
        stage(engine, content='value = 1\n')
    elif kind == 'syntax':
        stage(engine, content='value = (\n')
    elif kind == 'files':
        for index in range(9):
            stage(engine, f'src/value{index}.py')
    elif kind != 'empty':
        stage(engine)
    if kind == 'drift':
        engine.workspace.path('src/value0.py').write_bytes(b'value = 3\n')
    if kind == 'protected':
        engine.workspace.protected_paths.append('src/value0.py')
    if kind == 'repeated':
        from tracefix.tools.repair import build_repair_proposal
        proposal = build_repair_proposal(engine, state, FinishRepair(summary='已失败候选'))
        state.failed_candidate_signatures = [{'fingerprint': engine.candidate_fingerprint(proposal),
            'source_manifest': state.source_manifest, 'environment_digest': state.environment_digest,
            'test_spec_hash': state.test_spec_hash, 'feedback_ref': state.evidence_refs[0], 'patch_hash': 'failed'}]
        engine.store.save(state)
    runtime = build_runtime_tools(engine, state, FinishRepair)
    pipeline = runtime.pipeline()
    result = await pipeline.execute('FinishRepair', {'summary': '修复'}, 'rejected')
    assert result.is_error and result.error['error_code'] == code
    assert state.phase == Phase.DIAGNOSE and state.repair_segment == 0
    assert not runtime.submissions and pipeline.submission_value is None and not unknown
    assert not state.validation_refs
    assert state.loop_evidence['repair']['history'][-1]['accepted'] is False


def self_test(engine, state, **overrides):
    result = {'kind': 'repair_self_test', 'run_id': state.run_id, 'scope_id': state.scope_id,
        'source_manifest': state.source_manifest, 'target': 'unit', 'passed': True, 'exit_code': 0,
        'overlay_revision': engine.workspace.overlay_revision, 'diff_fingerprint': 'candidate',
        'output_ref': engine.artifacts.put(state.scope_id, state.run_id, 'TAP version 13', 'txt')}
    result.update(overrides)
    ref = engine.artifacts.put(state.scope_id, state.run_id, result)
    state.loop_evidence = {'repair': {'self_test_refs': [ref], 'preserved': True}}
    engine.store.save(state)
    return ref


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['valid', 'unlisted', 'run', 'scope', 'type', 'source', 'output'])
async def test_finish_self_test_refs_are_whitelisted_and_bound(repair, kind):
    engine, state = repair
    stage(engine)
    overrides = {'run': {'run_id': 'another-run'}, 'scope': {'scope_id': 'another-scope'},
        'type': {'kind': 'verification'}, 'source': {'source_manifest': 'another-source'},
        'output': {'output_ref': 'missing-output'}}.get(kind, {})
    ref = self_test(engine, state, **overrides)
    if kind == 'unlisted':
        state.loop_evidence['repair']['self_test_refs'] = []
        engine.store.save(state)
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
    result = await pipeline.execute('FinishRepair', {'summary': '修复', 'self_test_refs': [ref]}, 'finish')
    assert state.loop_evidence['repair']['preserved']
    if kind == 'valid':
        assert not result.is_error and ref in pipeline.submission_value.evidence_refs
    else:
        assert result.is_error and result.error['error_code'] == 'INVALID_SELF_TEST_REF'
    assert not state.validation_refs


@pytest.mark.asyncio
async def test_revert_single_multiple_all_and_repeat_after_new_overlay(repair):
    engine, state = repair
    stage(engine)
    stage(engine, 'src/value1.py')
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
    first = await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py']}, 'one')
    assert not first.is_error and first.result['reverted_paths'] == ['src/value0.py']
    assert engine.workspace.staged_content('src/value1.py') == b'value = 2\n'
    stage(engine)
    again = await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py']}, 'again')
    assert not again.is_error and engine.workspace.staged_content('src/value0.py') is None
    stage(engine)
    both = await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py', 'src/value1.py']}, 'both')
    assert set(both.result['reverted_paths']) == {'src/value0.py', 'src/value1.py'}
    stage(engine)
    all_result = await pipeline.execute('RevertOverlay', {'all': True}, 'all')
    assert all_result.result['reverted_paths'] == ['src/value0.py']
    assert engine.workspace.path('src/value0.py').read_bytes() == b'value = 1\n'


@pytest.mark.asyncio
async def test_revert_rejects_all_paths_atomically_and_invalidates_staged_refs(repair):
    engine, state = repair
    candidate = engine.workspace.stage_local_edit('src/value0.py', [('1', '2')],
                                                 expected_overlay_revision=0)
    engine.workspace.bind_staged_candidate(candidate, state, engine.artifacts)
    engine.store.save(state)
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
    denied = await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py', '../outside']}, 'denied')
    assert denied.is_error and engine.workspace.staged_content('src/value0.py') == b'value = 2\n'
    assert state.staged_candidate_refs
    accepted = await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py']}, 'accepted')
    assert not accepted.is_error and not state.staged_candidate_refs
    assert not engine.workspace._staged_candidates


@pytest.mark.asyncio
async def test_revert_persistence_failure_after_change_preserves_unknown_boundary(repair, monkeypatch):
    engine, state = repair
    stage(engine)
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()

    def unavailable(*args, **kwargs):
        raise OSError('模拟状态持久化失败')

    monkeypatch.setattr(engine, 'changed', unavailable)
    with pytest.raises(ToolOperationUnknown):
        await pipeline.execute('RevertOverlay', {'paths': ['src/value0.py']}, 'persist-failed')
    assert engine.workspace.staged_content('src/value0.py') is None
    assert engine.workspace.path('src/value0.py').read_bytes() == b'value = 1\n'


def test_overlay_snapshot_round_trip_restores_candidates_bytes_and_versions(repair):
    engine, state = repair
    runtime = build_runtime_tools(engine, state, FinishRepair)  # 初始化共享工作区锁。
    candidate = engine.workspace.stage_local_edit('src/value0.py', [('1', '2')],
                                                 expected_overlay_revision=0)
    engine.workspace.bind_staged_candidate(candidate, state, engine.artifacts)
    snapshot = engine.workspace.export_overlay()
    engine.workspace.clear_staged()
    engine.workspace.restore_overlay(snapshot)
    assert engine.workspace.export_overlay() == snapshot
    edits = engine.workspace.materialize_staged_candidates(state.staged_candidate_refs,
                                                           state=state, artifacts=engine.artifacts)
    assert edits[0].content == 'value = 2\n' and runtime.submissions == {}


@pytest.mark.parametrize('kind', ['binding', 'head', 'path', 'version', 'bytes', 'disk', 'candidate'])
def test_overlay_restore_rejects_tampering_and_preserves_current_overlay(repair, kind):
    engine, _ = repair
    stage(engine)
    saved = engine.workspace.export_overlay()
    broken = copy.deepcopy(saved)
    if kind == 'binding':
        broken['repository_snapshot'] = 'other'
    elif kind == 'head':
        broken['head'] = 'other'
    elif kind == 'path':
        broken['file_versions']['../escape'] = 1
    elif kind == 'version':
        broken['file_versions']['src/value0.py'] = 900
    elif kind == 'bytes':
        broken['files']['src/value0.py']['content_b64'] = 'not base64!'
    elif kind == 'disk':
        broken['files']['src/value0.py']['disk_hash'] = 'other'
    else:
        broken['candidates']['fake'] = {'ref': 'fake', 'path': 'src/value0.py'}
    stage(engine, content='value = 3\n')
    current = engine.workspace.export_overlay()
    with pytest.raises((ValueError, PermissionError, OSError)):
        engine.workspace.restore_overlay(broken)
    assert engine.workspace.export_overlay() == current
