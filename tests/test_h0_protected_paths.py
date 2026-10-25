import pytest

from tracefix.config import Project
from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.execution.workspace import ProtectedPathError, Workspace, is_frozen_path, is_protected_path
from tracefix.knowledge.scope import ScopeResolver
from tracefix.runtime.contracts import FileEdit, PatchProposal, digest


@pytest.mark.parametrize('relative', [
    '__snapshots__/a.snap', 'src/__snapshots__/view.data', 'snapshots/page.snap',
    '.github/workflows/ci.yml', '.gitlab-ci.yml', '.gitlab-ci.yaml',
    '.circleci/config.yml', 'Jenkinsfile', 'Jenkinsfile.release',
])
def test_default_protection_is_shared_and_readable(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('baseline', encoding='utf-8')
    workspace = Workspace(tmp_path, ['**'])
    scopes = ScopeResolver({'app': Project(id='app', repo_id='app', root=tmp_path, allowed_files=['**'])})
    ctx = scopes.context('app')
    assert is_protected_path(relative)
    assert is_frozen_path(relative)
    assert workspace.path(relative) == scopes.path(ctx, relative) == path
    for authorize in (lambda: workspace.path(relative, write=True), lambda: scopes.path(ctx, relative, write=True)):
        with pytest.raises(ProtectedPathError) as caught:
            authorize()
        assert caught.value.error_code == 'protected_path'
        assert caught.value.details == {'path': relative}


@pytest.mark.parametrize('relative', ['src/useLock.ts', 'src/Testimonial.tsx', 'src/LockService.ts', 'latest-run/value.ts'])
def test_protection_does_not_freeze_business_names(tmp_path, relative):
    workspace = Workspace(tmp_path, ['**'], protected_paths=[])
    scopes = ScopeResolver({'app': Project(id='app', repo_id='app', root=tmp_path, allowed_files=['**'])})
    assert not is_frozen_path(relative)
    assert not is_protected_path(relative)
    assert workspace.path(relative, write=True) == scopes.path(scopes.context('app'), relative, write=True)


def test_custom_paths_add_to_defaults_and_patch_preflight_is_atomic(tmp_path):
    for relative in ('src/value.ts', 'src/permissions.ts', '.github/ci.yml'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'baseline')
    workspace = Workspace(tmp_path, ['**'], protected_paths=['src/permissions.ts'])
    project = Project(id='app', repo_id='app', root=tmp_path, allowed_files=['**'],
                      repository_config={'protected_paths': ['src/permissions.ts']})
    scopes = ScopeResolver({'app': project})
    with pytest.raises(ProtectedPathError):
        scopes.path(scopes.context('app'), 'src/permissions.ts', write=True)
    with pytest.raises(ProtectedPathError):
        workspace.apply(PatchProposal(summary='原子保护测试', evidence_refs=[], edits=[
            FileEdit(path=relative, before_hash=digest(b'baseline'), content='changed')
            for relative in ('src/value.ts', 'src/permissions.ts')]))
    assert workspace.read('src/value.ts') == 'baseline'
    assert workspace.read('src/permissions.ts') == 'baseline'
    with pytest.raises(ProtectedPathError):
        workspace.path('.github/ci.yml', write=True)


def test_existing_frozen_rejections_remain_distinct(tmp_path):
    workspace = Workspace(tmp_path, ['**'])
    with pytest.raises(PermissionError) as caught:
        workspace.path('src/value.test.ts', write=True)
    assert not isinstance(caught.value, ProtectedPathError)


def test_export_and_resume_keep_repository_protection(tmp_path, monkeypatch):
    from tracefix.repository_registry import RepositoryRegistry

    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src/permissions.ts').write_bytes(b'export const permission = false;\n')
    (source / 'src/value.ts').write_bytes(b'export const value = false;\n')
    run_git(source, 'init', '-q', '--initial-branch=main')
    safe_index_files(source, ['src/permissions.ts', 'src/value.ts'])
    safe_commit(source, '测试基线', 'CI', 'ci@localhost')
    run_git(source, 'config', 'remote.origin.url', 'https://github.com/owner/app.git')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    monkeypatch.setattr(registry, 'prepare_codegraph', lambda *args: None)
    config = registry.register({'repo_id': 'app', 'root': str(source), 'owner': 'owner', 'repo': 'app',
        'default_ref': 'main', 'allowed_files': ['**'], 'allowed_urls': ['http://app:3000'],
        'profile': {'commands': {name: ['node', name] for name in ('start', 'reset', 'static', 'unit', 'build')}},
        'protected_paths': ['src/permissions.ts']})
    updated = registry.update('app', {'require_approval': True})
    assert updated['protected_paths'] == ['src/permissions.ts']
    assert updated['config_digest'] != config['config_digest']
    frozen = registry.freeze('app', 'run_h0')
    assert frozen['repository_config']['require_approval'] is True
    assert frozen['repository_config']['protected_paths'] == ['src/permissions.ts']
    config = updated
    project = Project(id='app', repo_id='owner/app', root=source, allowed_files=['**'], repository_config=config)
    scopes = ScopeResolver({'app': project})
    ctx = scopes.context('app')
    workspace, snapshot = Workspace.export(scopes, ctx, 'main', tmp_path / 'workspace')
    assert workspace.protected_paths == ['src/permissions.ts']
    with pytest.raises(ProtectedPathError):
        workspace.path('src/permissions.ts', write=True)
    restored = Workspace(workspace.root, ['**'])
    restored.validate_repository(scopes, ctx, snapshot)
    assert restored.protected_paths == ['src/permissions.ts']
    (restored.root / 'src/permissions.ts').write_bytes(b'export const permission = true;\n')
    with pytest.raises(ProtectedPathError):
        restored.check_frozen(snapshot)
    watched = ScopeResolver({'app': project}, source_path=registry.path)
    registry.update('app', {'protected_paths': ['src/other.ts']})
    with pytest.raises(PermissionError, match='写入策略已变更'):
        watched.path(watched.context('app'), 'src/value.ts')
