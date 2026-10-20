from copy import deepcopy

import pytest

from tracefix.config import Project
from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.execution.workspace import Workspace, CandidateRejected
from tracefix.knowledge.scope import ScopeResolver
from tracefix.runtime.contracts import FileEdit, PatchProposal, GuidanceConstraints, digest


@pytest.fixture
def workspace_case(tmp_path):
    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src/value.py').write_bytes(b'value = 1\n')
    (source / 'src/value.test.py').write_bytes(b'assert value == 1\n')
    run_git(source, 'init', '-q', '--initial-branch=main')
    safe_index_files(source, ['src/value.py', 'src/value.test.py'])
    safe_commit(source, '测试冻结基线', 'Test', 'test@localhost')
    scopes = ScopeResolver({'app': Project(id='app', repo_id='app', root=source, allowed_files=['**'])})
    context = scopes.context('app')
    workspace, snapshot = Workspace.export(scopes, context, 'HEAD', tmp_path / 'workspace')
    return scopes, context, workspace, snapshot


def proposal(*edits):
    return PatchProposal(summary='新增辅助模块', evidence_refs=['reproduced'], edits=list(edits))


def create(path='src/new/helpers.py', content='answer = 42\n'):
    return FileEdit(path=path, content=content, before_hash=digest(b''), operation='create')


def test_overlay_new_file_restores_without_materializing_and_rejects_disk_drift(workspace_case):
    _, _, workspace, snapshot = workspace_case
    workspace.stage('src/new.py', 'value = 2\n')
    assert workspace.edit_operation('src/new.py') == 'create'
    assert workspace.baseline_bytes('src/new.py') == b''
    assert workspace.read('src/new.py') == 'value = 2\n'
    assert 'src/new.py' in list(workspace.files())
    assert workspace.source_bytes('src/new.py') == (b'', b'value = 2\n')
    exported = workspace.export_overlay()
    assert exported['files']['src/new.py']['disk_exists'] is False
    workspace.clear_staged()
    workspace.restore_overlay(exported)
    assert workspace.read('src/new.py') == 'value = 2\n'
    assert not (workspace.root / 'src/new.py').exists()
    workspace.check_frozen(snapshot)
    (workspace.root / 'src/new.py').write_bytes(b'')
    with pytest.raises(CandidateRejected, match='DISK_DRIFT'):
        workspace.restore_overlay(exported)


def test_create_apply_manifest_branch_restart_and_reconcile(workspace_case):
    scopes, context, workspace, snapshot = workspace_case
    original = deepcopy(snapshot)
    baseline = workspace.head()
    patch = proposal(create())
    workspace.stage_many({'src/new/helpers.py': 'answer = 42\n'})
    result = workspace.apply(patch, base=baseline)
    assert 'new file mode 100644' in workspace.diff(baseline)
    assert result['patch_hash'] == digest(workspace.diff(baseline).encode())
    workspace.check_frozen(snapshot)
    assert snapshot == original
    workspace.validate_repository(scopes, context, snapshot, base_commit=baseline, patch_hash=result['patch_hash'])
    assert workspace.reconcile(patch, base=baseline) == result
    workspace.branch('tracefix/run_h4')
    resumed = Workspace(workspace.root, ['**'])
    resumed.validate_repository(scopes, context, snapshot, base_commit=baseline,
                                patch_hash=result['patch_hash'], branch='tracefix/run_h4')
    resumed.check_frozen(snapshot)
    assert resumed.repository_snapshot == original
    assert resumed.read('src/new/helpers.py') == 'answer = 42\n'


def test_create_requires_nonexistent_target_even_when_existing_file_is_empty(workspace_case):
    _, _, workspace, _ = workspace_case
    target = workspace.root / 'src/value.py'
    target.write_bytes(b'')
    with pytest.raises(ValueError, match='存在性'):
        workspace.apply(proposal(create('src/value.py')))
    assert target.read_bytes() == b''
    with pytest.raises(ValueError, match='存在性'):
        workspace.apply(proposal(FileEdit(path='src/missing.py', content='value = 1\n', before_hash=digest(b''))))


def test_mixed_create_modify_prevalidates_entire_patch(workspace_case):
    _, _, workspace, _ = workspace_case
    patch = proposal(create(), FileEdit(path='src/value.py', content='value = 2\n', before_hash='bad'))
    with pytest.raises(ValueError, match='基准哈希'):
        workspace.apply(patch)
    assert not (workspace.root / 'src/new').exists()
    assert (workspace.root / 'src/value.py').read_bytes() == b'value = 1\n'


def test_failed_new_file_index_restores_files_directories_and_index(workspace_case, monkeypatch):
    _, _, workspace, snapshot = workspace_case
    index = (workspace.root / '.git/index').read_bytes()
    edit = FileEdit(path='src/value.py', content='value = 2\n', before_hash=digest(b'value = 1\n'))

    def fail(*args):
        raise OSError('index write failed')

    monkeypatch.setattr('tracefix.execution.workspace.safe_index_files', fail)
    with pytest.raises(OSError, match='index write failed'):
        workspace.apply(proposal(edit, create()))
    assert not (workspace.root / 'src/new').exists()
    assert workspace.read('src/value.py') == 'value = 1\n'
    assert (workspace.root / '.git/index').read_bytes() == index
    workspace.check_frozen(snapshot)


def test_new_overlay_local_edits_and_candidate_snapshot_remain_creates(workspace_case):
    _, _, workspace, _ = workspace_case
    workspace.stage('src/new.py', 'value = 2\n')
    result = workspace.stage_local_edit('src/new.py', [('2', '3')], expected_overlay_revision=1)
    candidate = workspace.materialize_staged_candidates([result['staged_ref']])[0]
    assert candidate.operation == 'create'
    assert candidate.before_hash == digest(b'')
    assert candidate.content == 'value = 3\n'
    snapshot = workspace.export_overlay()
    workspace.clear_staged()
    workspace.restore_overlay(snapshot)
    assert workspace.materialize_staged_candidates([result['staged_ref']])[0] == candidate


def test_guidance_counts_created_lines_and_rejects_batch_without_partial_overlay(workspace_case):
    _, _, workspace, _ = workspace_case
    workspace.guidance_constraints = [GuidanceConstraints(max_files_changed=1, max_lines_changed=1)]
    with pytest.raises(PermissionError):
        workspace.stage_many({'src/one.py': 'one = 1\n', 'src/two.py': 'two = 2\n'})
    assert workspace.export_overlay()['files'] == {}
    workspace.stage('src/one.py', 'one = 1\n')
    with pytest.raises(PermissionError):
        workspace.apply(proposal(create('src/one.py', 'one = 1\nsecond = 2\n')))
    assert not (workspace.root / 'src/one.py').exists()


def test_second_candidate_modifies_previously_created_file(workspace_case):
    _, _, workspace, snapshot = workspace_case
    base = workspace.head()
    workspace.apply(proposal(create('src/new.py', 'value = 1\n')), base=base)
    workspace.stage('src/new.py', 'value = 2\n')
    assert workspace.edit_operation('src/new.py') == 'modify'
    workspace.apply(proposal(FileEdit(path='src/new.py', content='value = 2\n',
                                     before_hash=digest(b'value = 1\n'))), base=base)
    workspace.check_frozen(snapshot)
    assert '+value = 2' in workspace.diff(base)
    assert workspace.commit_bytes(base, 'src/new.py') == b''


@pytest.mark.parametrize('mutation', ['untracked', 'indexed', 'empty_directory', 'deleted', 'mode'])
def test_manifest_rejects_side_channel_addition_deletion_and_mode_change(workspace_case, mutation):
    _, _, workspace, snapshot = workspace_case
    if mutation in {'untracked', 'indexed'}:
        (workspace.root / 'src/sneaked.py').write_bytes(b'value = 99\n')
        if mutation == 'indexed':
            safe_index_files(workspace.root, ['src/sneaked.py'])
    elif mutation == 'empty_directory':
        (workspace.root / 'unused').mkdir()
    elif mutation == 'deleted':
        (workspace.root / 'src/value.py').unlink()
    else:
        run_git(workspace.root, 'update-index', '--chmod=+x', 'src/value.py')
    with pytest.raises(PermissionError):
        workspace.check_frozen(snapshot)


def test_approved_test_write_is_exact_and_shared_with_scope(workspace_case):
    scopes, context, workspace, snapshot = workspace_case
    path = 'src/value.test.py'
    with pytest.raises(PermissionError):
        workspace.stage(path, 'assert value == 2\n')
    workspace.approved_test_files = (path,)
    workspace.stage(path, 'assert value == 2\n')
    assert scopes.path(context, path, write=True, approved_test_files=(path,)).is_file()
    with pytest.raises(PermissionError):
        scopes.path(context, 'src/another.test.py', write=True, approved_test_files=(path,))
    with pytest.raises(PermissionError):
        workspace.stage('src/another.test.py', 'assert True\n')
    workspace.apply(proposal(FileEdit(path=path, content='assert value == 2\n',
                                     before_hash=snapshot['files'][path])))
    workspace.check_frozen(snapshot)


@pytest.mark.parametrize('path', ['tests/oracle/expected.py', 'tests/package-lock.json',
                                 'tests/output.snap', 'tests/__snapshots__/expected.py',
                                 '.github/workflows/tests.yml', '.circleci/test.yml',
                                 'Jenkinsfile', 'tests/protected.py'])
def test_test_approval_never_overrides_hard_protection(tmp_path, path):
    workspace = Workspace(tmp_path, ['**'], protected_paths=['tests/protected.py'], approved_test_files=(path,))
    scopes = ScopeResolver({'app': Project(id='app', repo_id='app', root=tmp_path, allowed_files=['**'],
        repository_config={'protected_paths': ['tests/protected.py']})})
    with pytest.raises(PermissionError):
        workspace.stage(path, 'assert True\n')
    with pytest.raises(PermissionError):
        scopes.path(scopes.context('app'), path, write=True, approved_test_files=(path,))
