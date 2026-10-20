from pathlib import Path

import pytest

from tracefix.config import Project
from tracefix.execution.harness_revision import (RevisionReceipt, materialize_revision,
    tracked_files, validate_workspace_revision)
from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.scope import ScopeResolver
from tracefix.runtime.contracts import digest
from tracefix.runtime.harness_contracts import HarnessJob


def repository(root: Path):
    (root / 'src').mkdir(parents=True)
    (root / 'src/value.ts').write_text('export const value = false;\n', encoding='utf-8')
    run_git(root, 'init', '-q', '--initial-branch=main')
    safe_index_files(root, ['src/value.ts'])
    safe_commit(root, 'baseline', 'CI', 'ci@localhost')
    run_git(root, 'config', 'remote.origin.url', 'https://github.com/owner/app.git')
    run_git(root, 'update-ref', 'refs/remotes/origin/main', run_git(root, 'rev-parse', 'HEAD').decode().strip())
    return root


def setup_revision(tmp_path):
    source = repository(tmp_path / 'source')
    project = Project(id='app', repo_id='owner/app', root=source, allowed_files=['src/**'])
    scopes = ScopeResolver({'app': project})
    original_workspace, original = Workspace.export(scopes, scopes.context('app'), 'HEAD', tmp_path / 'original')
    origin = original['commit']
    candidate = tmp_path / 'candidate'
    run_git(None, 'clone', '--no-hardlinks', '--', str(original_workspace.root), str(candidate), local_clone=True)
    export_base = run_git(candidate, 'rev-parse', 'HEAD').decode().strip()
    run_git(candidate, 'checkout', '-B', 'tracefix/export', export_base)
    (candidate / 'src/value.ts').write_text('export const value = true;\n', encoding='utf-8')
    safe_index_files(candidate, ['src/value.ts'])
    expected_patch_hash = digest(Workspace(candidate, project.allowed_files).diff(base=export_base).encode())
    safe_commit(candidate, 'candidate', 'CI', 'ci@localhost')
    accepted = run_git(candidate, 'rev-parse', 'HEAD').decode().strip()
    patch = run_git(candidate, 'diff', '--no-color', export_base, accepted, '--').decode()
    assert digest(patch.encode()) == expected_patch_hash
    candidate_files = tracked_files(candidate, accepted)
    job = HarnessJob(scope_id='app', repo_id='owner/app', console_task_id='console',
        goal_hash=digest('goal'), origin_base_commit=origin, acceptance_refs=['test'],
        local_commit_authorized=True, accepted_commit=accepted)
    receipt = RevisionReceipt(scope_id='app', job_id=job.job_id, session_id='session', run_id='run',
        origin_base_commit=origin, input_commit=origin, candidate_commit=accepted,
        candidate_repository=str(candidate), export_base_commit=export_base,
        input_tree=run_git(candidate, 'rev-parse', f'{export_base}^{{tree}}').decode().strip(),
        candidate_tree=run_git(candidate, 'rev-parse', f'{accepted}^{{tree}}').decode().strip(),
        input_files=tracked_files(candidate, export_base), candidate_files=candidate_files,
        patch_hash=digest(patch.encode()), verification_ref='verification',
        registered_config_hash=digest(project.model_dump(mode='json')))
    materialized, snapshot = materialize_revision(receipt, tmp_path / 'materialized', job=job,
        project=project, original_snapshot=original)
    return source, project, original, job, receipt, materialized, snapshot


def test_materialize_revision_validates_candidate_binding_and_workspace_diff(tmp_path):
    source, project, original, job, receipt, workspace, snapshot = setup_revision(tmp_path)
    assert snapshot['commit'] == job.accepted_commit
    assert tracked_files(workspace.root, job.accepted_commit) == receipt.candidate_files
    assert validate_workspace_revision(workspace, snapshot, job=job, project=project,
        original_snapshot=original, receipts=[receipt])['files'] == receipt.candidate_files
    (workspace.root / 'src/value.ts').write_text('tampered\n', encoding='utf-8')
    with pytest.raises(PermissionError, match='patch hash'):
        validate_workspace_revision(workspace, snapshot, job=job, project=project,
            original_snapshot=original, receipts=[receipt])
    (workspace.root / 'src/value.ts').write_text('export const value = true;\n', encoding='utf-8')
    (source / 'src/value.ts').write_text('registered drift\n', encoding='utf-8')
    with pytest.raises(PermissionError, match='漂移'):
        validate_workspace_revision(workspace, snapshot, job=job, project=project,
            original_snapshot=original, receipts=[receipt])


def test_legacy_registered_validation_is_not_used_for_derived_candidate(tmp_path):
    _source, project, original, job, receipt, workspace, snapshot = setup_revision(tmp_path)
    with pytest.raises((PermissionError, RuntimeError)):
        workspace.validate_repository(ScopeResolver({'app': project}),
            ScopeResolver({'app': project}).context('app'), snapshot)
    assert validate_workspace_revision(workspace, snapshot, job=job, project=project,
        original_snapshot=original, receipts=[receipt])


@pytest.mark.parametrize('field', ['candidate_tree', 'candidate_files', 'patch_hash', 'registered_config_hash'])
def test_revision_bytes_lineage_and_config_are_immutable(tmp_path, field):
    _source, project, original, job, receipt, workspace, snapshot = setup_revision(tmp_path)
    updates = {'candidate_tree': '0' * 40, 'candidate_files': {'src/value.ts': '0' * 64},
        'patch_hash': '0' * 64, 'registered_config_hash': '0' * 64}
    forged = receipt.model_copy(update={field: updates[field]})
    with pytest.raises(PermissionError):
        validate_workspace_revision(workspace, snapshot, job=job, project=project,
            original_snapshot=original, receipts=[forged])


def test_second_increment_must_inherit_previous_accepted_candidate(tmp_path):
    source, project, original, job, first, workspace, snapshot = setup_revision(tmp_path)
    base = workspace.head()
    (workspace.root / 'src/second.ts').write_text('export const second = 2;\n', encoding='utf-8')
    safe_index_files(workspace.root, ['src/second.ts'])
    patch_hash = digest(workspace.diff(base=base).encode())
    safe_commit(workspace.root, '第二增量', 'CI', 'ci@localhost')
    candidate = workspace.head()
    second = RevisionReceipt(scope_id='app', job_id=job.job_id, session_id='second-session', run_id='second-run',
        origin_base_commit=job.origin_base_commit, input_commit=first.candidate_commit, candidate_commit=candidate,
        candidate_repository=str(workspace.root), export_base_commit=base,
        input_tree=run_git(workspace.root, 'rev-parse', base + '^{tree}').decode().strip(),
        candidate_tree=run_git(workspace.root, 'rev-parse', candidate + '^{tree}').decode().strip(),
        input_files=first.candidate_files, candidate_files=tracked_files(workspace.root, candidate), patch_hash=patch_hash,
        verification_ref='second-verification', previous_receipt_ref='first', previous_run_id=first.run_id,
        registered_config_hash=digest(project.model_dump(mode='json')))
    job.accepted_commit = candidate
    materialized, source_snapshot = materialize_revision(second, tmp_path / 'third-workspace', job=job, project=project,
        original_snapshot=original, previous=first, receipts=[first, second])
    assert (materialized.root / 'src/second.ts').read_text(encoding='utf-8') == 'export const second = 2;\n'
    assert source_snapshot['files'] == second.candidate_files
    assert run_git(source, 'rev-parse', '--verify', candidate + '^{commit}', check=False) == b''
    forged = second.model_copy(update={'input_commit': job.origin_base_commit})
    with pytest.raises(PermissionError, match='继承'):
        validate_workspace_revision(materialized, source_snapshot, job=job, project=project,
            original_snapshot=original, receipts=[first, forged])
