import pytest

from tracefix.model.gateway import ModelOutputError
from tracefix.runtime.contracts import FileEdit, PatchProposal, digest
from tracefix.runtime.repair_progress import normalized_diff_fingerprint
from tracefix.runtime.smoke import make_engine
from tracefix.tools.fingerprints import diff_fingerprint


def test_new_file_progress_identity_survives_materialization(tmp_path):
    content = 'export const added = true;\n'
    engine, state = make_engine(tmp_path)
    workspace = engine.workspace
    state.patch_base_commit = workspace.head()
    empty = (diff_fingerprint(workspace, state), normalized_diff_fingerprint(workspace, state))
    workspace.stage('src/added.ts', content)
    staged = (diff_fingerprint(workspace, state), normalized_diff_fingerprint(workspace, state))
    assert all(before != after for before, after in zip(empty, staged))
    workspace.apply(PatchProposal(summary='增加源码模块', evidence_refs=['baseline'], edits=[
        FileEdit(path='src/added.ts', before_hash=digest(b''), operation='create', content=content)]))
    workspace.clear_staged()
    assert (diff_fingerprint(workspace, state), normalized_diff_fingerprint(workspace, state)) == staged


@pytest.mark.parametrize('path,content,alternative', [
    ('src/query.sql', 'select 1;\n', 'select 2;\n'),
    ('src/icon.svg', '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
     '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>\n'),
])
def test_complete_candidate_identity_and_failed_candidate_gate_survive_materialization(
        tmp_path, monkeypatch, path, content, alternative):
    engine, state = make_engine(tmp_path)
    workspace = engine.workspace
    state.patch_base_commit = workspace.head()
    patch = PatchProposal(summary='增加授权实现文件', evidence_refs=[state.test_spec_ref], edits=[
        FileEdit(path=path, before_hash=digest(b''), operation='create', content=content)])
    empty = engine.candidate_fingerprint()
    proposed = engine.candidate_fingerprint(patch)
    assert proposed != empty
    engine.validate_patch_candidate(patch, state=state)
    workspace.stage(path, content)
    assert path not in set(workspace.files())
    def reject_overlay_export():
        raise AssertionError('候选指纹不得重入 overlay 导出锁')
    monkeypatch.setattr(workspace, 'export_overlay', reject_overlay_export)
    assert engine.candidate_fingerprint() == proposed
    applied = workspace.apply(patch, base=state.patch_base_commit)
    assert engine.candidate_fingerprint() == proposed
    assert not workspace._staged_files and path in workspace._created_entries
    workspace.check_frozen(engine.source)
    state.failed_candidate_signatures = [{
        'fingerprint': engine.candidate_fingerprint(), 'source_manifest': state.source_manifest,
        'environment_digest': state.environment_digest, 'test_spec_hash': state.test_spec_hash,
        'patch_hash': applied['patch_hash'], 'feedback_ref': 'failed-verification.json'}]
    workspace.apply(PatchProposal(summary='尝试不同候选', evidence_refs=[state.test_spec_ref], edits=[
        FileEdit(path=path, before_hash=digest(content.encode()), content=alternative)]),
        base=state.patch_base_commit)
    assert engine.candidate_fingerprint() != proposed
    repeated = PatchProposal(summary='重复已经失败的候选', evidence_refs=[state.test_spec_ref], edits=[
        FileEdit(path=path, before_hash=digest(alternative.encode()), content=content)])
    assert engine.candidate_fingerprint(repeated) == proposed
    with pytest.raises(ModelOutputError) as rejected:
        engine.validate_patch_candidate(repeated, state=state)
    assert rejected.value.details['error_code'] == 'FAILED_CANDIDATE_REPEATED'
    assert rejected.value.details['feedback_ref'] == 'failed-verification.json'
