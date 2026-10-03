from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.config import Project
from tracefix.cli.main import Session
from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.scope import ScopeResolver
from tracefix.remote import prepare_checkout
from tracefix.runtime.contracts import digest
from tracefix.runtime.smoke import make_engine


def repository(root, origin=True):
    (root / 'src').mkdir(parents=True)
    (root / 'src/value.ts').write_bytes(b'export const value = false;\n')
    run_git(root, 'init', '-q', '--initial-branch=main')
    safe_index_files(root, ['src/value.ts'])
    safe_commit(root, 'baseline', 'CI', 'ci@localhost')
    if origin:
        run_git(root, 'config', 'remote.origin.url', 'https://github.com/owner/app.git')
    run_git(root, 'update-ref', 'refs/remotes/origin/main', run_git(root, 'rev-parse', 'HEAD').decode().strip())
    return root


def exported(tmp_path):
    root = repository(tmp_path / 'source')
    scopes = ScopeResolver({'app': Project(id='app', repo_id='owner/app', root=root)})
    ctx = scopes.context('app')
    workspace, snapshot = Workspace.export(scopes, ctx, 'HEAD', tmp_path / 'workspace')
    return root, scopes, ctx, workspace, snapshot


@pytest.mark.parametrize('entry', ['config.worktree', 'commondir', 'objects/info/alternates'])
def test_nested_smoke_initializes_without_using_parent_metadata(tmp_path, entry):
    parent = repository(tmp_path / 'parent')
    target = parent / '.git' / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('untrusted parent metadata\n')
    before = {path.relative_to(parent): path.read_bytes()
              for path in (parent / '.git').rglob('*') if path.is_file()}

    engine, state = make_engine(parent / 'smoke')

    assert engine.workspace.root.joinpath('.git').is_dir()
    assert state.source_manifest
    assert Path(run_git(parent / 'smoke/code', 'rev-parse', '--show-toplevel').decode().strip()).resolve() == (
        parent / 'smoke/code').resolve()
    assert before == {path.relative_to(parent): path.read_bytes()
                      for path in (parent / '.git').rglob('*') if path.is_file()}
    with pytest.raises(PermissionError, match='外部对象目录'):
        run_git(parent, 'status', '--porcelain')


@pytest.mark.parametrize('key,value', [('include.path', 'missing-config'),
                                      ('core.worktree', 'other'), ('core.bare', 'true')])
def test_nested_git_init_does_not_read_parent_config(tmp_path, key, value):
    parent = repository(tmp_path / 'parent')
    if key == 'core.worktree':
        other = tmp_path / 'other'
        other.mkdir()
        value = str(other)
    run_git(parent, 'config', key, value)
    before = (parent / '.git/config').read_bytes()

    child = repository(parent / 'child', origin=False)

    assert Path(run_git(child, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == child.resolve()
    assert (parent / '.git/config').read_bytes() == before
    assert run_git(child, 'config', '--get', key, check=False) == (b'false\n' if key == 'core.bare' else b'')
    with pytest.raises(PermissionError):
        run_git(parent, 'status', '--porcelain')
    with pytest.raises(PermissionError):
        run_git(parent, 'init', '-q')


@pytest.mark.parametrize('entry', ['config.worktree', 'commondir', 'objects/info/alternates'])
def test_git_init_rejects_existing_unsafe_metadata(tmp_path, entry):
    root = repository(tmp_path / 'source')
    target = root / '.git' / entry
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('untrusted metadata\n')
    before = (root / '.git/config').read_bytes()

    with pytest.raises(PermissionError, match='外部对象目录'):
        run_git(root, 'init', '-q')

    assert (root / '.git/config').read_bytes() == before


@pytest.mark.parametrize('change', ['origin', 'head', 'branch', 'dirty', 'root', 'source'])
def test_existing_checkout_requires_real_identity(tmp_path, change):
    root = repository(tmp_path / 'checkout')
    settings = {'repository': 'owner/app', 'base': 'main'}
    assert prepare_checkout(settings, root)['action'] == 'existing'
    if change == 'origin':
        run_git(root, 'config', 'remote.origin.url', 'https://example.test/owner/app.git')
    elif change == 'head':
        safe_commit(root, 'unexpected head', 'CI', 'ci@localhost')
    elif change == 'branch':
        run_git(root, 'switch', '-c', 'other')
    elif change == 'dirty':
        (root / 'untracked').write_text('unexpected')
    elif change == 'root':
        root = root / 'src'
    else:
        settings['source_commit'] = 'HEAD~1'
    with pytest.raises((PermissionError, RuntimeError, ValueError)):
        prepare_checkout(settings, root)


@pytest.mark.parametrize('change', ['scope', 'repo', 'root', 'origin', 'head', 'branch',
                                    'untracked', 'delete', 'type', 'legacy', 'source_head',
                                    'source_branch', 'source_root', 'source_tree', 'tracking'])
def test_resume_rejects_identity_and_manifest_drift(tmp_path, change):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    workspace.validate_repository(scopes, ctx, snapshot)
    if change == 'source_head':
        safe_commit(root, 'source drift', 'CI', 'ci@localhost')
    elif change == 'source_branch':
        run_git(root, 'switch', '-c', 'other')
    elif change == 'source_root':
        scopes.projects['app'].root = root / 'src'
    elif change == 'source_tree':
        snapshot['source_tree'] = '0' * 40
    elif change == 'tracking':
        run_git(workspace.root, 'update-index', '--force-remove', 'src/value.ts')
    elif change in {'scope', 'repo'}:
        snapshot['scope_id' if change == 'scope' else 'repo_id'] = 'wrong'
    elif change == 'root':
        snapshot['workspace_root'] = str(root)
    elif change == 'origin':
        run_git(workspace.root, 'config', 'remote.origin.url', 'https://github.com/other/app.git')
    elif change == 'head':
        safe_commit(workspace.root, 'unexpected', 'CI', 'ci@localhost')
    elif change == 'branch':
        run_git(workspace.root, 'switch', '-c', 'other')
    elif change == 'untracked':
        (workspace.root / 'unexpected').write_text('unexpected')
    elif change == 'delete':
        (workspace.root / 'src/value.ts').unlink()
    elif change == 'type':
        (workspace.root / 'src/value.ts').unlink()
        (workspace.root / 'src/value.ts').mkdir()
    else:
        snapshot.pop('repository_version')
    with pytest.raises(PermissionError):
        workspace.validate_repository(scopes, ctx, snapshot)


@pytest.mark.parametrize('entry', ['.git', 'src/value.ts', 'src/empty-link'])
def test_links_are_rejected_without_following(tmp_path, entry):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    target = workspace.root / entry
    try:
        if entry == '.git':
            target.rename(workspace.root / '.original-git')
            target.symlink_to(workspace.root / '.original-git', target_is_directory=True)
        else:
            if target.exists():
                target.unlink()
            target.symlink_to(root / 'missing')
    except OSError as error:
        pytest.skip(f'系统未授予临时测试符号链接权限：{error}')
    with pytest.raises(PermissionError):
        workspace.validate_repository(scopes, ctx, snapshot)


def test_git_environment_and_execution_extensions_are_isolated(tmp_path, monkeypatch):
    root = repository(tmp_path / 'source')
    other = repository(tmp_path / 'other')
    marker = tmp_path / 'executed'
    script = tmp_path / 'evil.sh'
    script.write_text(f'#!/bin/sh\necho executed > "{marker.as_posix()}"\ncat\n')
    script.chmod(0o755)
    for key, value in {'core.hooksPath': str(tmp_path), 'core.fsmonitor': str(script),
        'diff.external': str(script), 'diff.evil.textconv': str(script),
        'filter.evil.clean': str(script), 'filter.evil.process': str(script),
        'filter.evil.smudge': str(script), 'filter.evil.required': 'true'}.items():
        run_git(root, 'config', key, value)
    for name in ('pre-commit', 'post-checkout'):
        hook = tmp_path / name
        hook.write_bytes(script.read_bytes())
        hook.chmod(0o755)
    (root / '.gitattributes').write_text('*.ts filter=evil diff=evil\n')
    global_config = tmp_path / 'global-config'
    global_config.write_text(f'[core]\n hooksPath = {tmp_path.as_posix()}\n[init]\n templateDir = {tmp_path.as_posix()}\n')
    monkeypatch.setenv('GIT_DIR', str(other / '.git'))
    monkeypatch.setenv('GIT_WORK_TREE', str(other))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.worktree')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(other))
    before = (root / '.git/config').read_bytes()
    assert Path(run_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve() == root.resolve()
    safe_index_files(root, ['src/value.ts', '.gitattributes'])
    safe_commit(root, 'safe', 'CI', 'ci@localhost')
    workspace = Workspace(root, ['src/**'])
    (root / 'src/value.ts').write_text('export const value = true;\n')
    assert '+export const value = true;' in workspace.diff()
    run_git(root, 'add', 'src/value.ts')
    workspace.branch('tracefix/run_extensions')
    assert not marker.exists()
    assert (root / '.git/config').read_bytes() == before


def test_legal_continuation_retains_bound_baseline(tmp_path):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    baseline = workspace.head()
    (workspace.root / 'src/value.ts').write_text('export const value = true;\n')
    patch_hash = digest(workspace.diff(baseline).encode())
    workspace.validate_repository(scopes, ctx, snapshot, base_commit=baseline, patch_hash=patch_hash)
    branch = workspace.branch('tracefix/run_legal')
    workspace.validate_repository(scopes, ctx, snapshot, base_commit=baseline,
                                  patch_hash=patch_hash, branch=branch)
    assert workspace.diff() == ''
    assert workspace.diff(baseline)
    (workspace.root / 'src/value.ts').write_text('export const value = "second";\n')
    workspace.branch(branch)
    workspace.validate_repository(scopes, ctx, snapshot, base_commit=baseline,
        patch_hash=digest(workspace.diff(baseline).encode()), branch=branch)
    (workspace.root / 'src/value.ts').write_text('export const value = "third-uncommitted";\n')
    workspace.validate_repository(scopes, ctx, snapshot, base_commit=baseline,
        patch_hash=digest(workspace.diff(baseline).encode()), branch=branch)


def test_bind_rejects_tampered_workspace_before_runner(tmp_path, monkeypatch):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    (workspace.root / 'unexpected').write_text('untracked')
    session = SimpleNamespace(scopes=scopes, ctx=ctx)
    session.validate_workspace = lambda *args: Session.validate_workspace(session, *args)
    state = SimpleNamespace(scope_id='app', source_manifest=digest(snapshot), patch_base_commit=None,
                            patch_hash=None, local_branch=None, run_id='run_fixture')
    monkeypatch.setattr('tracefix.cli.main.DockerRunner', lambda *_: pytest.fail('runner must not be constructed'))
    with pytest.raises(PermissionError):
        Session.bind(session, state, None, workspace, snapshot)


@pytest.mark.parametrize('change', ['contents', 'delete', 'type'])
def test_mountpoints_are_prepared_and_frozen_before_snapshot(tmp_path, change):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    for name in ('dist', 'node_modules'):
        assert snapshot['entries'][name]['kind'] == 'directory'
        assert not snapshot['entries'][name]['tracked']
    workspace.prepare_mountpoints()
    workspace.check_frozen(snapshot)
    if change == 'contents':
        (workspace.root / 'dist/injected.js').write_text('injected')
    else:
        (workspace.root / 'dist').rmdir()
        if change == 'type':
            (workspace.root / 'dist').write_text('not a mountpoint')
    workspace.repository_snapshot = None
    with pytest.raises(PermissionError):
        workspace.check_frozen(snapshot)
    if change == 'delete':
        assert not (workspace.root / 'dist').exists()


def test_candidate_history_cannot_hide_frozen_edits(tmp_path):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    baseline = workspace.head()
    run_git(workspace.root, 'switch', '-c', 'tracefix/run_history')
    (workspace.root / 'unauthorized.txt').write_text('hidden earlier addition')
    safe_index_files(workspace.root, ['unauthorized.txt'])
    safe_commit(workspace.root, 'unauthorized', 'CI', 'ci@localhost')
    run_git(workspace.root, 'update-index', '--force-remove', 'unauthorized.txt')
    (workspace.root / 'unauthorized.txt').unlink()
    safe_commit(workspace.root, 'hide addition', 'CI', 'ci@localhost')
    with pytest.raises(PermissionError, match='候选提交'):
        workspace.validate_repository(scopes, ctx, snapshot, base_commit=baseline,
            patch_hash=None, branch='tracefix/run_history')


@pytest.mark.parametrize('change', ['missing', 'legacy', 'incomplete', 'digest', 'untracked',
                                    'mount_contents', 'mount_delete', 'mount_type', 'tracking', 'root', 'index_type'])
async def test_runner_refuses_drift_before_any_docker_side_effect(tmp_path, change):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    requested = digest(snapshot)
    if change == 'missing':
        workspace.repository_snapshot = None
    elif change == 'legacy':
        snapshot.pop('repository_version')
    elif change == 'incomplete':
        snapshot.pop('source_repository')
    elif change == 'root':
        snapshot['workspace_root'] = str(root)
    elif change == 'digest':
        requested = '0' * 64
    elif change == 'untracked':
        (workspace.root / 'unexpected').write_text('untracked')
    elif change == 'tracking':
        run_git(workspace.root, 'update-index', '--force-remove', 'src/value.ts')
    elif change == 'index_type':
        run_git(workspace.root, 'update-index', '--chmod=+x', 'src/value.ts')
    elif change == 'mount_contents':
        (workspace.root / 'node_modules/injected.js').write_text('untrusted')
    else:
        (workspace.root / 'node_modules').rmdir()
        if change == 'mount_type':
            (workspace.root / 'node_modules').write_text('wrong type')
    runner = DockerRunner(SimpleNamespace(), workspace, 'run_gate')
    async def docker(*args, **kwargs):
        pytest.fail('Docker must not be dispatched')
    runner.docker = docker
    with pytest.raises(PermissionError):
        await runner.start(requested)


async def test_runner_accepts_bound_export_and_allowed_continuation(tmp_path):
    root, scopes, ctx, workspace, snapshot = exported(tmp_path)
    calls = []
    runner = DockerRunner(SimpleNamespace(image='fixture', port=3000, health_path='/health',
                                         commands={'start': ['node', 'fixture']}),
                          workspace, 'run_gate')
    async def docker(*args, **kwargs):
        calls.append(args)
        return {'passed': True}
    runner.docker = docker
    assert (await runner.start(digest(snapshot)))['passed']
    (workspace.root / 'src/value.ts').write_text('export const value = true;\n')
    workspace.validate_repository(scopes, ctx, snapshot, base_commit=workspace.head(),
                                   patch_hash=digest(workspace.diff().encode()))
    assert (await runner.start(digest(snapshot)))['passed']
    assert sum(args[:2] == ('network', 'create') for args in calls) == 2


@pytest.mark.parametrize('key,value', [('core.worktree', 'other'), ('core.bare', 'true')])
def test_local_git_redirect_is_rejected_before_index_writes(tmp_path, key, value):
    root = repository(tmp_path / 'source')
    other = repository(tmp_path / 'other')
    snapshots = {path: path.read_bytes() for repo in (root, other)
                 for path in (repo / '.git/index', repo / 'src/value.ts')}
    run_git(root, 'config', key, str(other) if value == 'other' else value)
    with pytest.raises(PermissionError, match='core.worktree/core.bare'):
        safe_index_files(root, ['src/value.ts'])
    assert all(path.read_bytes() == content for path, content in snapshots.items())


def test_executable_export_is_explicitly_unsupported(tmp_path):
    root = repository(tmp_path / 'source')
    run_git(root, 'update-index', '--chmod=+x', 'src/value.ts')
    safe_commit(root, 'executable script', 'CI', 'ci@localhost')
    scopes = ScopeResolver({'app': Project(id='app', repo_id='owner/app', root=root)})
    with pytest.raises(PermissionError, match='可执行文件模式'):
        Workspace.export(scopes, scopes.context('app'), 'HEAD', tmp_path / 'workspace')


async def test_missing_workspace_rejects_before_continuation_marker(engine_session):
    session, engine, state = engine_session
    web = session.workspace_commands.call
    engine.store.save(state)
    session.publish_console()
    web('run.ended', {'id': session.console_run_id, 'exitCode': 1})
    before = web('run', {'id': session.console_run_id})
    session.run_id = None
    engine.workspace.root.rename(engine.workspace.root.with_name('saved-workspace'))

    with pytest.raises(PermissionError, match='工作区根目录或 .git 入口不可信'):
        await session.continue_task(session.console_run_id, '缺失工作区不得继续')

    after = web('run', {'id': session.console_run_id})
    assert after['continuationCount'] == 0
    assert after.get('continuationMarkers', []) == before.get('continuationMarkers', [])
    assert after['agentRunId'] == before['agentRunId'] == state.run_id
    assert after.get('logs', []) == before.get('logs', [])
