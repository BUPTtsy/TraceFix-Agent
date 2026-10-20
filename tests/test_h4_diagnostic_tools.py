"""H4 诊断工具必须保持冻结验证与父工作区隔离。"""
import csv
from pathlib import Path

import pytest

from tracefix.execution.repository import run_git, safe_index_files
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import commit_workspace
from tracefix.runtime.contracts import BrowserAction, FileEdit, FinishRepair, PatchProposal, Phase, TestSpec as FrozenSpec, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import ToolRejected
from tracefix.tools.fingerprints import diff_fingerprint
from tracefix.tools.git_history import GitHistoryInput, read_history
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.local import GlobInput, GrepInput, LocalTools, ReadInput
from tracefix.workers.contracts import WorkerResult


@pytest.fixture
def repair(tmp_path, monkeypatch):
    monkeypatch.delenv('TRACEFIX_AGENT_MODE', raising=False)
    engine, state = make_engine(tmp_path / 'run')
    state.mode, state.phase, state.repair_mode = 'repair', Phase.DIAGNOSE, 'hybrid'
    state.patch_base_commit = engine.workspace.head()
    state.evidence_refs = [engine.put(state, {'reproduced': True})]
    engine.store.save(state)
    return engine, state


@pytest.mark.asyncio
async def test_browser_diagnosis_keeps_frozen_plan_and_baseline(repair):
    engine, state = repair
    frozen = engine.put(state, [BrowserAction(kind='navigate', value=state.url).model_dump()])
    state.replay_plan_ref = frozen
    binding = (state.test_spec_ref, state.test_spec_hash, list(state.evidence_refs), state.reproduced)
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
    result = await pipeline.execute('BrowserSnapshot', {}, 'diagnostic-snapshot')
    assert result.executed and not result.is_error
    assert state.observation_ref
    assert state.replay_plan_ref == frozen
    assert engine.get(state, frozen) == [BrowserAction(kind='navigate', value=state.url).model_dump()]
    assert binding == (state.test_spec_ref, state.test_spec_hash, state.evidence_refs, state.reproduced)
    denied = await pipeline.execute('BrowserNavigate', {'value': 'http://unauthorized:3000'}, 'bad-url')
    assert denied.is_error
    assert len(engine.browser.calls) == 1
    worker = build_runtime_tools(engine, state, WorkerResult, {'worker_depth': 1})
    assert not worker.registry.contains('BrowserSnapshot')
    state.repair_mode = 'pipeline'
    assert not build_runtime_tools(engine, state, FinishRepair).registry.contains('BrowserSnapshot')


@pytest.mark.parametrize('mode', ['unit', 'static', 'build'])
def test_non_gui_diagnosis_has_no_browser_tools(repair, mode):
    engine, state = repair
    spec = FrozenSpec(goal=state.goal, mode=mode, authorized_actions=[])
    state.test_spec_ref = engine.put(state, spec.model_dump(mode='json'))
    state.test_spec_hash = digest(spec)
    runtime = build_runtime_tools(engine, state, FinishRepair)
    assert not runtime.registry.contains('BrowserSnapshot')
    assert runtime.registry.contains('GitHistory')


def test_git_history_reads_frozen_path_only_and_rejects_future(repair):
    engine, state = repair
    path = 'src/value.ts'
    frozen = engine.source['commit']
    root = engine.scopes.projects[state.scope_id].root
    (root / path).write_text('future secret = true\n', encoding='utf-8')
    (root / 'private.txt').write_text('private data\n', encoding='utf-8')
    safe_index_files(root, [path, 'private.txt'])
    commit_workspace(root, '未来提交不应可见')
    future = run_git(root, 'rev-parse', 'HEAD').decode().strip()
    result = read_history(engine, state, GitHistoryInput(path=path))
    assert result['output'].splitlines() == [frozen]
    result = read_history(engine, state, GitHistoryInput(path=path, action='show'))
    assert 'future secret' not in result['output']
    result = read_history(engine, state, GitHistoryInput(path=path, action='blame', end_line=1))
    assert 'future secret' not in result['output']
    for options in ({'path': path, 'revision': future}, {'path': path, 'revision': '--all'},
                    {'path': path, 'revision': 'HEAD'}, {'path': '../private.txt'},
                    {'path': '/absolute'}, {'path': 'private.txt'}, {'path': '.git/config'},
                    {'path': 'src/value.ts', 'action': 'blame', 'start_line': 4, 'end_line': 1}):
        with pytest.raises((ToolRejected, PermissionError)):
            read_history(engine, state, GitHistoryInput(**options))


@pytest.mark.asyncio
async def test_write_read_search_and_finish_create_only_overlay(repair):
    engine, state = repair
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
    target = engine.workspace.root / 'src/new.ts'
    before = diff_fingerprint(engine.workspace, state)
    result = await pipeline.execute('Write', {'file_path': str(target), 'content': 'export const created = true;\n'}, 'create')
    assert result.executed and not result.is_error
    assert not target.exists()
    assert before != diff_fingerprint(engine.workspace, state)
    local = LocalTools(engine, {}, state)
    assert 'created' in local.read(ReadInput(file_path=str(target)))['content']
    assert str(target) in local.glob(GlobInput(pattern='**/*.ts'))['files']
    assert local.grep(GrepInput(pattern='created'))['matches']
    result = await pipeline.execute('finish_repair', {'summary': '新增授权辅助模块'}, 'finish')
    assert result.executed and not result.is_error
    proposal = pipeline.submission_value
    assert proposal.edits[0].operation == 'create'
    assert proposal.edits[0].before_hash == digest(b'')
    assert not target.exists()


def task():
    return {'task_id': 'attempt', 'role': 'independent-attempt', 'phase': 'DIAGNOSE',
            'objective': 'try a concrete fix in an isolated workspace',
            'prompt': 'Edit the allowed source and return the actual observed result.',
            'allowed_tools': ['Read', 'Edit', 'Write', 'RunTests'],
            'allowed_files': ['src/value.ts'], 'writable_files': ['src/value.ts'],
            'write_enabled': True, 'expected_output': 'candidate with evidence',
            'completion_criteria': ['run the available tests'], 'constraints': ['stay in scope']}


@pytest.mark.asyncio
async def test_worker_write_isolated_candidate_no_parent_patch(repair, monkeypatch):
    from tracefix.runtime.engine import Engine

    engine, state = repair
    parent_root = engine.workspace.root
    parent_bytes = engine.workspace.read('src/value.ts')
    observed = []

    async def model_call(self, child, schema, context, **kwargs):
        assert self is not engine and self.workspace is not engine.workspace
        assert context['worker_same_model'] is True
        assert self.workspace.root != parent_root
        runtime = build_runtime_tools(self, child, schema, context)
        assert runtime.registry.contains('RunTests')
        assert runtime.registry.contains('Edit')
        assert not runtime.registry.contains('agent.delegate')
        assert not runtime.registry.contains('BrowserSnapshot')
        result = await runtime.pipeline().execute('Write', {
            'file_path': str(self.workspace.root / 'src/value.ts'),
            'content': 'export const repaired = true;\n'}, 'child-write')
        assert not result.is_error
        observed.append(self.workspace.root)
        assert engine.workspace.read('src/value.ts') == parent_bytes
        return WorkerResult(summary='attempt done', files_touched=['src/value.ts'], patch_hash='untrusted')

    monkeypatch.setattr(Engine, 'model_call', model_call)
    runtime = build_runtime_tools(engine, state, FinishRepair)
    result = await runtime.pipeline().execute('agent.delegate', task(), 'attempt-delegate')
    assert result.executed and not result.is_error
    assert engine.workspace.read('src/value.ts') == parent_bytes
    assert not engine.workspace.export_overlay()['files']
    assert not state.patch_ref and not state.validation_refs
    assert not observed[0].exists()
    value = result.result
    assert value['patch_hash'] is None
    candidate = engine.get(state, value['data']['attempt']['candidate_ref'])
    assert candidate['applied_to_parent'] is False
    assert candidate['requires_parent_edit_and_verify'] is True
    assert candidate['edits'][0]['content'] == 'export const repaired = true;\n'


@pytest.mark.asyncio
async def test_worker_failure_discards_overlay(repair, monkeypatch):
    from tracefix.runtime.engine import Engine

    engine, state = repair
    roots = []

    async def fail(self, child, schema, context, **kwargs):
        roots.append(self.workspace.root)
        self.workspace.stage('src/value.ts', 'export const failed = true;\n')
        raise RuntimeError('attempt failed')

    monkeypatch.setattr(Engine, 'model_call', fail)
    with pytest.raises(RuntimeError, match='attempt failed'):
        await build_runtime_tools(engine, state, FinishRepair).handlers['agent.delegate'](task(), 'failure')
    assert not roots[0].exists()
    assert not engine.workspace.export_overlay()['files']


@pytest.mark.asyncio
async def test_worker_runs_fixed_tests_with_full_source_but_scoped_tools(repair, monkeypatch):
    from tracefix.runtime.engine import Engine
    from tracefix.tools import local as local_module

    engine, state = repair
    helper = 'src/helper.ts'
    helper_content = 'export const inherited = true;\n'
    engine.workspace.apply(PatchProposal(summary='父候选新增模块', evidence_refs=state.evidence_refs,
        edits=[FileEdit(path=helper, before_hash=digest(b''), content=helper_content, operation='create')]),
        base=state.patch_base_commit)
    engine.runner = DockerRunner(engine.profile, engine.workspace, state.run_id)
    engine.runner._started = True
    engine.runner.resolved_image_ids = {'app': 'sha256:' + 'a' * 64}
    commands, child_refs = [], []

    async def process(command, timeout, **kwargs):
        commands.append(command)
        if command[1] == 'run':
            assert command[command.index('--name') + 1].startswith(engine.runner.name + '-shell-')
            assert '--network=none' in command and '--read-only' in command
            fields = next(csv.reader([command[command.index('--mount') + 1]]))
            assert 'readonly' in fields
            staging = Path(next(value[4:] for value in fields if value.startswith('src=')))
            assert (staging / helper).read_text(encoding='utf-8') == helper_content
            assert (staging / 'src/value.ts').read_text(encoding='utf-8') == 'export const repaired = true;\n'
            assert 'sha256:' + 'a' * 64 in command
            return {'passed': True, 'exit_code': 0, 'output': 'TAP version 13\n1..1\nok 1 - isolated repair passes\n'}
        assert command[1:3] == ['rm', '-f']
        return {'passed': True, 'exit_code': 0, 'output': ''}

    async def model_call(self, child, schema, context, **kwargs):
        pipeline = build_runtime_tools(self, child, schema, context).pipeline()
        denied = await pipeline.execute('Read', {'file_path': str(self.workspace.root / helper)}, 'deny-helper')
        assert denied.is_error
        inherited = self.workspace.staged_content(helper)
        assert inherited == helper_content.encode()
        result = await pipeline.execute('Write', {'file_path': str(self.workspace.root / 'src/value.ts'),
            'content': 'export const repaired = true;\n'}, 'try-write')
        assert not result.is_error
        tests = await pipeline.execute('RunTests', {'target': 'unit'}, 'try-tests')
        assert not tests.is_error and tests.result['passed'] is True
        assert tests.result['summary']['passed'] == 1
        child_refs.append(tests.result['result_ref'])
        return WorkerResult(summary='isolated tests passed', files_touched=['src/value.ts'])

    monkeypatch.setattr(local_module, 'process', process)
    monkeypatch.setattr(Engine, 'model_call', model_call)
    result = await build_runtime_tools(engine, state, FinishRepair).pipeline().execute(
        'agent.delegate', task(), 'self-tested-attempt')
    assert not result.is_error
    assert len(commands) == 2
    assert not engine.workspace.export_overlay()['files']
    assert engine.workspace.path(helper).read_text(encoding='utf-8') == helper_content
    candidate = engine.get(state, result.result['data']['attempt']['candidate_ref'])
    assert candidate['self_tests'][0]['passed'] is True
    assert candidate['self_tests'][0]['run_id'] != state.run_id
    output = engine.artifacts.read(state.scope_id, state.run_id, candidate['self_tests'][0]['output_ref'])
    assert b'isolated repair passes' in output
    assert not state.validation_refs and not ((state.loop_evidence or {}).get('repair') or {}).get('self_test_refs')
    engine.workspace.stage('src/value.ts', 'export const repaired = true;\n')
    rejected = await build_runtime_tools(engine, state, FinishRepair).pipeline().execute(
        'FinishRepair', {'summary': '父模型仍需独立自测', 'self_test_refs': child_refs}, 'deny-child-evidence')
    assert rejected.is_error and rejected.error['error_code'] == 'INVALID_SELF_TEST_REF'


@pytest.mark.asyncio
async def test_worker_rejects_candidate_when_parent_overlay_changes(repair, monkeypatch):
    from tracefix.runtime.engine import Engine

    engine, state = repair
    roots = []

    async def model_call(self, child, schema, context, **kwargs):
        roots.append(self.workspace.root)
        self.workspace.stage('src/value.ts', 'export const worker = true;\n')
        engine.workspace.stage('src/new-parent.ts', 'export const parent = true;\n')
        return WorkerResult(summary='stale attempt', files_touched=['src/value.ts'])

    monkeypatch.setattr(Engine, 'model_call', model_call)
    result = await build_runtime_tools(engine, state, FinishRepair).pipeline().execute(
        'agent.delegate', task(), 'stale-attempt')
    assert result.is_error and '父工作区已变化' in result.error['message']
    assert not roots[0].exists()
    assert engine.workspace.staged_content('src/value.ts') is None
    assert engine.workspace.staged_content('src/new-parent.ts') == b'export const parent = true;\n'
