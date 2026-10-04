import pytest

from tracefix.execution.workspace import CandidateRejected, Workspace, commit_workspace
from tracefix.execution.repository import safe_index_files
from tracefix.runtime.contracts import FileEdit, PatchProposal, Phase, RunState, StagedCandidateRef, digest
from tracefix.runtime.local_tools import EditInput, LocalTools, ReadInput
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.tool_handlers import build_runtime_tools, materialize_patch_proposal
from tracefix.storage.artifacts import Artifacts


def workspace(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / 'app.py').write_bytes(b'alpha = 1\r\nbeta = "ok"\r\n')
    return Workspace(root, ['*.py', '*.ts'])


def test_unique_local_edit_preserves_crlf_and_materializes(tmp_path):
    bound = workspace(tmp_path)
    path = tmp_path / 'repo' / 'app.py'
    result = bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')],
        expected_overlay_revision=0, expected_overlay_hash=digest(path.read_bytes()))
    assert result['status'] == 'staged'
    assert result['written'] is False and result['disk_written'] is False
    assert b'\r\n' in bound.staged_content('app.py')
    ref = StagedCandidateRef(**result['staged_ref'])
    edits = bound.materialize_staged_candidates([ref])
    assert edits == [FileEdit(path='app.py', before_hash=result['disk_before_hash'],
                              content=bound.staged_content('app.py').decode())]


def test_local_edit_rejects_ambiguous_and_no_change(tmp_path):
    bound = workspace(tmp_path)
    path = tmp_path / 'repo' / 'app.py'
    current = digest(path.read_bytes())
    with pytest.raises(CandidateRejected) as ambiguous:
        bound.stage_local_edit('app.py', [('= ', '=  ')],
                               expected_overlay_revision=0, expected_overlay_hash=current)
    assert ambiguous.value.details['error_code'] == 'ANCHOR_AMBIGUOUS'
    with pytest.raises(CandidateRejected) as unchanged:
        bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 1')],
                               expected_overlay_revision=0, expected_overlay_hash=current)
    assert unchanged.value.details['error_code'] == 'NO_CHANGE'


def test_local_edit_rejects_disk_drift(tmp_path):
    bound = workspace(tmp_path)
    path = tmp_path / 'repo' / 'app.py'
    current = digest(path.read_bytes())
    bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')],
                          expected_overlay_revision=0, expected_overlay_hash=current)
    path.write_bytes(path.read_bytes().replace(b'alpha', b'changed'))
    with pytest.raises(CandidateRejected) as drift:
        bound.stage_local_edit('app.py', [('alpha = 2', 'alpha = 3')],
                               expected_overlay_revision=1,
                               expected_overlay_hash=digest(bound.staged_content('app.py')))
    assert drift.value.details['error_code'] == 'DISK_DRIFT'


def test_materialize_rejects_stale_ref(tmp_path):
    bound = workspace(tmp_path)
    path = tmp_path / 'repo' / 'app.py'
    result = bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')],
                                   expected_overlay_revision=0,
                                   expected_overlay_hash=digest(path.read_bytes()))
    ref = StagedCandidateRef(**result['staged_ref'])
    bound.stage_local_edit('app.py', [('beta = "ok"', 'beta = "new"')],
                           expected_overlay_revision=1,
                           expected_overlay_hash=digest(bound.staged_content('app.py')))
    with pytest.raises(CandidateRejected) as stale:
        bound.materialize_staged_candidates([ref])
    assert stale.value.details['error_code'] == 'STALE_REF'


def test_unicode_bom_keeps_untouched_bytes(tmp_path):
    bound = workspace(tmp_path)
    original = '\ufeffalpha = "中文“引号”"\r\nbeta = 1\r\n'.encode('utf-8')
    (bound.root / 'app.py').write_bytes(original)
    bound.stage_local_edit('app.py', [('beta = 1', 'beta = 2')], expected_overlay_hash=digest(original))
    assert bound.staged_content('app.py') == original.replace(b'beta = 1', b'beta = 2')


@pytest.mark.parametrize('extension', ['.js', '.jsx', '.mjs', '.cjs'])
def test_legal_javascript_jsx_is_not_checked_with_typescript_parser(extension):
    diagnostics = Workspace.syntax_diagnostics('App' + extension, 'export const App = () => <div/>;\n')
    assert all(item['kind'] == 'unavailable' for item in diagnostics)


def test_same_baseline_blocks_and_multiple_files(tmp_path):
    bound = workspace(tmp_path)
    (bound.root / 'other.py').write_bytes(b'first = 1\nsecond = 2\n')
    first = bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 20'),
        ('beta = "ok"', 'beta = "修正“完成”"')], expected_overlay_revision=0)
    second = bound.stage_local_edit('other.py', [('first = 1', 'first = 3')],
                                   expected_overlay_revision=0)
    edits = bound.materialize_staged_candidates([first['staged_ref'], second['staged_ref']])
    assert [edit.path for edit in edits] == ['app.py', 'other.py']
    assert edits[0].content == 'alpha = 20\r\nbeta = "修正“完成”"\r\n'
    assert len(first['affected_ranges']) == 2


def test_overlap_not_found_stale_and_no_fuzzy(tmp_path):
    bound = workspace(tmp_path)
    for blocks, code in [([('alpha = 1', 'alpha = 2'), ('= 1', '= 3')], 'OVERLAP'),
                         ([('alpha = 1\n', 'alpha = 2\n')], 'ANCHOR_NOT_FOUND')]:
        with pytest.raises(CandidateRejected) as rejected:
            bound.stage_local_edit('app.py', blocks, expected_overlay_revision=0)
        assert rejected.value.details['error_code'] == code
        assert rejected.value.details['written'] is False
        assert rejected.value.details['next_step']
        assert bound.staged_content('app.py') is None
    bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')], expected_overlay_revision=0)
    with pytest.raises(CandidateRejected) as stale:
        bound.stage_local_edit('app.py', [('alpha = 2', 'alpha = 3')], expected_overlay_revision=0)
    assert stale.value.details['error_code'] == 'STALE_BASE'
    with pytest.raises(ValueError):
        EditInput(file_path=str(bound.root / 'app.py'), old_string='alpha', new_string='x',
                  expected_overlay_revision=1, replace_all=True)


def test_new_syntax_rejected_then_corrected_and_baseline_error_preserved(tmp_path):
    bound = workspace(tmp_path)
    with pytest.raises(CandidateRejected) as invalid:
        bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = (')], expected_overlay_revision=0)
    assert invalid.value.details['error_code'] == 'NEW_SYNTAX_ERROR'
    assert invalid.value.details['added_diagnostics'][0]['line'] == 1
    assert bound.staged_content('app.py') is None
    corrected = bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')],
                                      expected_overlay_revision=0)
    assert corrected['status'] == 'staged'
    (bound.root / 'broken.ts').write_bytes(b'const value = ;\nconst other = 1;\n')
    original = bound.stage_local_edit('broken.ts', [('other = 1', 'other = 2')],
                                     expected_overlay_revision=0)
    assert original['diagnostics'][0]['baseline']
    with pytest.raises(CandidateRejected) as replaced_error:
        bound.stage_local_edit('broken.ts', [('const value = ;', 'const value = "unterminated;')],
                               expected_overlay_revision=1)
    assert replaced_error.value.details['error_code'] == 'NEW_SYNTAX_ERROR'


def test_real_artifact_restoration_run_scope_diff_binding_and_drift(tmp_path):
    bound = workspace(tmp_path)
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = RunState(scope_id='scope', run_id='current', goal='修正', url='http://app')
    result = bound.stage_local_edit('app.py', [('alpha = 1', 'alpha = 2')],
                                   expected_overlay_revision=0)
    result = bound.bind_staged_candidate(result, state, artifacts)
    assert artifacts.exists(state.scope_id, state.run_id, result['diff_ref'])
    restored = Workspace(bound.root, ['*.py'])
    refs = [StagedCandidateRef(**result['staged_ref'])]
    materialized = restored.materialize_staged_candidates(refs, state=state, artifacts=artifacts)
    assert materialized[0].content.startswith('alpha = 2')
    other = state.model_copy(update={'run_id': 'other'})
    with pytest.raises(CandidateRejected):
        restored.materialize_staged_candidates(refs, state=other, artifacts=artifacts)
    wrong = [refs[0].model_copy(update={'diff_ref': 'wrong.diff'})]
    with pytest.raises(CandidateRejected):
        restored.materialize_staged_candidates(wrong, state=state, artifacts=artifacts)
    (bound.root / 'app.py').write_bytes(b'concurrent = 3\n')
    with pytest.raises(CandidateRejected) as drift:
        restored.materialize_staged_candidates(refs, state=state, artifacts=artifacts)
    assert drift.value.details['error_code'] == 'DISK_DRIFT'


@pytest.mark.asyncio
async def test_pipeline_error_feedback_correction_and_native_materialization(tmp_path):
    engine, state = make_engine(tmp_path / 'run')
    project = engine.scopes.projects[state.scope_id]
    project.allowed_files = ['src/**', '*.py']
    (project.root / 'app.py').write_bytes(b'first = 1\nsecond = 1\n')
    safe_index_files(project.root, ['app.py'])
    commit_workspace(project.root, 'Stage3 exact edit fixture')
    context = engine.scopes.context(state.scope_id)
    engine.workspace, source = Workspace.export(engine.scopes, context, 'HEAD', tmp_path / 'export')
    state.mode, state.phase = 'repair', Phase.DIAGNOSE
    state.source_manifest = digest(source)
    state.evidence_refs = [engine.artifacts.put(state.scope_id, state.run_id, {'public': True})]
    engine.store.save(state)
    runtime = build_runtime_tools(engine, state, PatchProposal)
    path = str(engine.workspace.root / 'app.py')
    tools = LocalTools(engine, {}, state)
    read = tools.read(ReadInput(file_path=path))
    ambiguous = await runtime.pipeline().execute('Edit', {'file_path': path,
        'edits': [{'old_string': '= 1', 'new_string': '= 2'}],
        'expected_overlay_hash': read['overlay_hash'], 'expected_overlay_revision': 0}, 'ambiguous')
    assert ambiguous.is_error and ambiguous.error['error_code'] == 'ANCHOR_AMBIGUOUS'
    assert ambiguous.result['match_count'] == 2 and not ambiguous.executed
    corrected = await runtime.pipeline().execute('Edit', {'file_path': path,
        'edits': [{'old_string': 'first = 1', 'new_string': 'first = 2'}],
        'expected_overlay_hash': read['overlay_hash'], 'expected_overlay_revision': 0}, 'corrected')
    assert corrected.executed and not corrected.is_error
    proposed = await runtime.pipeline().execute('propose_patch', {'summary': '修正 first',
        'evidence_refs': state.evidence_refs, 'staged_refs': [corrected.result['staged_ref']]}, 'submit')
    assert proposed.executed and not proposed.is_error
    assert runtime.submissions['propose_patch'].edits[0].content == 'first = 2\nsecond = 1\n'
    assert engine.workspace.path('app.py').read_bytes() == b'first = 1\nsecond = 1\n'
    result = engine.workspace.apply(runtime.submissions['propose_patch'])
    assert result['patch_hash'] and engine.workspace.path('app.py').read_bytes().startswith(b'first = 2')
