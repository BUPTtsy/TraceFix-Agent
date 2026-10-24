import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import shutil

import pytest
import yaml

from tracefix.execution.repository import run_git, safe_commit
from tracefix.execution.workspace import Workspace
from tracefix.config import load_projects
from tracefix.knowledge.scope import ScopeResolver
from tracefix.knowledge.memory import MemoryLibrary
from tracefix.storage.artifacts import Artifacts
from tracefix.service.session import Session
from tracefix.storage.store import MemoryStore
from langgraph.checkpoint.memory import InMemorySaver
from tracefix.repository_control import dispatch
from tracefix.repository_registry import RepositoryError, RepositoryRegistry


def _checkout(tmp_path, name, remote):
    root = tmp_path / name
    root.mkdir()
    run_git(root, 'init', '-q', '--initial-branch=main')
    (root / 'src').mkdir()
    (root / 'src' / 'main.py').write_text('value = 1\n', encoding='utf-8')
    run_git(root, 'add', 'src/main.py')
    safe_commit(root, '初始化', 'Test', 'test@localhost')
    run_git(root, 'remote', 'add', 'origin', f'https://github.com/{remote}.git')
    return root


def _profile(project):
    return {
        'project': project,
        'commands': {name: ['echo', name] for name in ('start', 'reset', 'static', 'unit', 'build')},
        'url': 'http://app:3000', 'allowed_origins': ['http://app:3000'],
    }


def _fields(root, project, remote):
    owner, repo = remote.split('/')
    return {'repo_id': project, 'root': str(root), 'owner': owner, 'repo': repo,
            'default_ref': 'main', 'profile': _profile(project),
            'allowed_files': ['src/**'], 'allowed_urls': ['http://app:3000'],
            'credential_ref': 'deploy/github', 'enabled': True}


def test_repository_register_rejects_identity_conflicts_and_freezes(tmp_path, monkeypatch):
    root_a = _checkout(tmp_path, 'a', 'acme/a')
    root_b = _checkout(tmp_path, 'b', 'acme/b')
    registry_path = tmp_path / 'projects.yaml'
    monkeypatch.setenv('TRACEFIX_REPOSITORY_ROOTS', json.dumps([str(tmp_path)]))
    registry = RepositoryRegistry(registry_path, data_root=tmp_path / 'data')
    first = registry.register(_fields(root_a, 'Alpha', 'acme/a'))
    assert first['repo_id'] == 'alpha'
    with pytest.raises(RepositoryError) as duplicate:
        registry.register(_fields(root_b, 'ALPHA', 'acme/b'))
    assert duplicate.value.code == 'REPO_ID_CONFLICT'
    with pytest.raises(RepositoryError) as origin:
        registry.register(_fields(root_b, 'beta', 'acme/a'))
    assert origin.value.code == 'REPOSITORY_BINDING_MISMATCH'
    frozen = registry.freeze('alpha', 'run_alpha')
    assert Path(frozen['projects_path']).is_file()
    assert frozen['commit'] == first['commit']
    registry.update('alpha', {'enabled': False})
    assert not registry.get('alpha')['enabled']
    assert RepositoryRegistry(Path(frozen['projects_path'])).get('alpha')['enabled']
    with pytest.raises(RepositoryError) as immutable:
        RepositoryRegistry(Path(frozen['projects_path'])).update('alpha', {'enabled': False})
    assert immutable.value.code == 'FROZEN_CONFIG'


def test_control_bridge_uses_stable_json_contract(tmp_path, monkeypatch):
    root = _checkout(tmp_path, 'repo', 'acme/repo')
    path = tmp_path / 'projects.yaml'
    monkeypatch.setenv('TRACEFIX_REPOSITORY_ROOTS', json.dumps([str(tmp_path)]))
    result = dispatch('repository.register', _fields(root, 'repo-one', 'acme/repo'), projects_path=path)
    assert result['repo_id'] == 'repo-one'
    assert dispatch('repository.get', {'id': 'REPO-ONE'}, projects_path=path)['projectId'] == 'repo-one'
    with pytest.raises(RepositoryError):
        dispatch('repository.get', {'id': 'missing'}, projects_path=path)


@pytest.mark.parametrize('fault,code', [
    ('root', 'ROOT_NOT_AUTHORIZED'), ('files', 'INVALID_ALLOWED_FILES'),
    ('profile', 'INVALID_PROFILE'), ('profile_ref', 'INVALID_PROFILE_REF'),
    ('profile_project', 'PROFILE_MISMATCH'), ('ref', 'INVALID_REF'),
    ('urls', 'PROFILE_URL_NOT_AUTHORIZED'),
])
def test_invalid_registration_leaves_no_visible_record(tmp_path, fault, code):
    root = _checkout(tmp_path, 'repo', 'acme/repo')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[root], data_root=tmp_path / 'data')
    fields = _fields(root, 'repo-one', 'acme/repo')
    if fault == 'root':
        fields['root'] = str(tmp_path / 'outside')
    elif fault == 'files':
        fields['allowed_files'] = ['../host']
    elif fault == 'profile':
        fields['profile'] = {'commands': {}}
    elif fault == 'profile_ref':
        fields.pop('profile')
        fields['profile_ref'] = '../outside.yaml'
    elif fault == 'profile_project':
        fields['profile']['project'] = 'other'
    elif fault == 'ref':
        fields['default_ref'] = 'missing-ref'
    elif fault == 'urls':
        fields['allowed_urls'] = ['https://different.example']
    with pytest.raises(RepositoryError) as rejected:
        registry.register(fields)
    assert rejected.value.code == code
    assert registry.list() == []
    assert not registry.path.exists()


def test_duplicate_root_disabled_start_and_atomic_write_failure(tmp_path, monkeypatch):
    root = _checkout(tmp_path, 'repo', 'acme/repo')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    registry.register(_fields(root, 'first', 'acme/repo'))
    before = registry.path.read_bytes()
    with pytest.raises(RepositoryError) as conflict:
        registry.register(_fields(root, 'second', 'acme/repo'))
    assert conflict.value.code == 'ROOT_CONFLICT'
    assert registry.path.read_bytes() == before
    registry.update('first', {'enabled': False})
    with pytest.raises(RepositoryError) as disabled:
        registry.freeze('first', 'disabled-run')
    assert disabled.value.code == 'REPOSITORY_DISABLED'
    assert not (tmp_path / 'data' / 'frozen-repositories' / 'disabled-run' / 'projects.yaml').exists()
    before = registry.path.read_bytes()
    def failing_replace(*args):
        raise OSError('replace failed')
    monkeypatch.setattr('tracefix.repository_registry.os.replace', failing_replace)
    with pytest.raises(OSError):
        registry.update('first', {'enabled': True})
    assert registry.path.read_bytes() == before
    assert not registry.get('first')['enabled']


def test_concurrent_registration_preserves_both_records(tmp_path):
    root_a = _checkout(tmp_path, 'a', 'acme/a')
    root_b = _checkout(tmp_path, 'b', 'acme/b')
    path = tmp_path / 'projects.yaml'
    def register(identifier, root):
        registry = RepositoryRegistry(path, authorized_roots=[tmp_path])
        return registry.register(_fields(root, identifier, 'acme/' + identifier))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(register, identifier, root) for identifier, root in [('a', root_a), ('b', root_b)]]
        assert {future.result()['repo_id'] for future in results} == {'a', 'b'}
    assert {record['repo_id'] for record in RepositoryRegistry(path).list()} == {'a', 'b'}


def test_cross_process_registration_does_not_lose_updates(tmp_path):
    root_a = _checkout(tmp_path, 'process-a', 'acme/process-a')
    root_b = _checkout(tmp_path, 'process-b', 'acme/process-b')
    path = tmp_path / 'projects.yaml'
    script = (
        "import json,sys; from tracefix.repository_registry import RepositoryRegistry; "
        "p=RepositoryRegistry(sys.argv[1]); f=json.loads(sys.argv[2]); p.register(f)"
    )
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'backend' / 'packages' / 'agent' / 'src'),
                   'TRACEFIX_REPOSITORY_ROOTS': json.dumps([str(tmp_path)])}
    processes = []
    for identifier, root, repo in [('proc-a', root_a, 'process-a'), ('proc-b', root_b, 'process-b')]:
        processes.append(subprocess.Popen([sys.executable, '-c', script, str(path), json.dumps(
            _fields(root, identifier, 'acme/' + repo))], env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), results
    assert {item['repo_id'] for item in RepositoryRegistry(path).list()} == {'proc-a', 'proc-b'}


def test_a_b_a_frozen_scopes_workspace_memory_and_artifacts(tmp_path):
    root_a = _checkout(tmp_path, 'source-a', 'acme/a')
    root_b = _checkout(tmp_path, 'source-b', 'acme/b')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    registry.register(_fields(root_a, 'a', 'acme/a'))
    fields_b = _fields(root_b, 'b', 'acme/b')
    fields_b['credential_ref'] = 'deploy/other'
    registry.register(fields_b)
    outputs = []
    artifacts = Artifacts(tmp_path / 'data' / 'artifacts')
    memory = MemoryLibrary(tmp_path / 'data' / 'memory.sqlite3', cross_run=False)
    for identifier, run_id in [('a', 'run_a1'), ('b', 'run_b1'), ('a', 'run_a2')]:
        frozen = registry.freeze(identifier, run_id)
        scopes = ScopeResolver(load_projects(Path(frozen['projects_path'])), Path(frozen['projects_path']))
        context = scopes.context(identifier)
        workspace, source = Workspace.export(scopes, context, frozen['commit'], tmp_path / 'data' / 'workspaces' / run_id)
        assert source['repository_config']['frozen_run_id'] == run_id
        reference = artifacts.put(identifier, run_id, {'repo': identifier})
        memory.note(identifier, run_id, {'text': identifier + ' evidence'}, source_manifest=source['commit'])
        outputs.append((frozen, scopes, context, workspace, source, reference))
    assert len({item[3].root for item in outputs}) == 3
    assert outputs[0][0]['repository_config']['credential_ref'] == outputs[2][0]['repository_config']['credential_ref']
    assert outputs[1][0]['repository_config']['credential_ref'] != outputs[0][0]['repository_config']['credential_ref']
    assert memory.working_memory('a', 'run_b1')['finding'] == []
    assert artifacts.json('b', 'run_b1', outputs[0][5]) == {'repo': 'b'}
    with pytest.raises(PermissionError):
        outputs[0][3].validate_repository(outputs[1][1], outputs[1][2], outputs[0][4])
    before = deepcopy(outputs[0][4])
    registry.update('a', {'allowed_files': ['src/main.py'], 'credential_ref': 'deploy/new'})
    outputs[0][3].validate_repository(outputs[0][1], outputs[0][2], before)
    assert outputs[0][0]['repository_config']['allowed_files'] == ['src/**']
    assert registry.freeze('a', 'run_a1')['config_digest'] == outputs[0][0]['config_digest']
    with pytest.raises(RepositoryError) as conflict:
        registry.freeze('b', 'run_a1')
    assert conflict.value.code == 'RUN_ID_CONFLICT'


def test_ref_is_resolved_again_for_new_run_and_snapshot_digest_is_checked(tmp_path):
    root = _checkout(tmp_path, 'repo', 'acme/repo')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    first = registry.register(_fields(root, 'app', 'acme/repo'))
    (root / 'src' / 'main.py').write_text('value = 2\n', encoding='utf-8')
    run_git(root, 'add', 'src/main.py')
    safe_commit(root, '更新', 'Test', 'test@localhost')
    snapshot = registry.freeze('app', 'run_new')
    assert snapshot['commit'] != first['commit']
    assert snapshot['repository_config']['profile']['source_commit'] == snapshot['commit']
    path = Path(snapshot['projects_path'])
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    raw['projects'][0]['repository_config']['frozen_run_id'] = 'tampered'
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    with pytest.raises(RepositoryError):
        load_projects(path)


@pytest.mark.asyncio
async def test_managed_session_consumes_frozen_registry_without_tty(tmp_path, monkeypatch):
    root = _checkout(tmp_path, 'repo', 'acme/app')
    data = tmp_path / 'data'
    path = tmp_path / 'projects.yaml'
    registry = RepositoryRegistry(path, authorized_roots=[tmp_path], data_root=data)
    registry.register(_fields(root, 'app', 'acme/app'))
    frozen = registry.freeze('app', 'service_run')
    monkeypatch.setenv('TRACEFIX_API_KEY', 'test-only')
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    args = SimpleNamespace(project='app', projects=frozen['projects_path'], profile=frozen['profile_path'],
                           console_db=str(tmp_path / 'console.sqlite3'), data=str(data), goal='service test',
                           mode='test', plain=True, url=None, spec=None, skills='skills', run=True,
                           command=None, execution_mode='batch')
    session = Session(args, MemoryStore(), InMemorySaver())
    session.begin_console_run()
    calls = []
    monkeypatch.setattr('tracefix.service.session.DockerRunner', lambda *arguments: SimpleNamespace(browser_command=lambda: []))
    monkeypatch.setattr('tracefix.service.session.MCPBrowser', lambda *arguments: SimpleNamespace())
    monkeypatch.setattr('tracefix.service.session.Gateway', lambda: SimpleNamespace())
    async def drive(state):
        calls.append(state.run_id)
    session.drive = drive
    await session.create_agent()
    await session.task
    state = session.state()
    assert calls == ['service_run']
    assert state.run_id == frozen['repository_config']['frozen_run_id']
    assert session.engine.profile.source_commit == frozen['commit']
    assert session.engine.memory.cross_run is False
    assert session.engine.retriever.cross_run is False
    registry.update('app', {'allowed_files': ['src/main.py'], 'credential_ref': 'deploy/changed'})
    session.validate_workspace(state)
    assert state.remote_config['credential_ref'] == 'deploy/github'
    assert session.engine.workspace.allowed_files == ['src/**']


def test_frozen_profile_tamper_is_rejected_before_runtime(tmp_path):
    root = _checkout(tmp_path, 'repo', 'acme/app')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    registry.register(_fields(root, 'app', 'acme/app'))
    frozen = registry.freeze('app', 'run_profile')
    profile_path = Path(frozen['profile_path'])
    payload = yaml.safe_load(profile_path.read_text(encoding='utf-8'))
    payload['commands']['start'] = ['unapproved', 'command']
    profile_path.write_text(yaml.safe_dump(payload), encoding='utf-8')
    for source in [registry, RepositoryRegistry(Path(frozen['projects_path']))]:
        with pytest.raises(RepositoryError) as rejected:
            source.freeze('app', 'run_profile')
        assert rejected.value.code == 'FROZEN_PROFILE_MISMATCH'


def test_managed_registration_and_run_checkouts_are_isolated_and_idempotent(tmp_path, monkeypatch):
    source = _checkout(tmp_path, 'remote-source', 'acme/remote')
    roots = tmp_path / 'checkouts'
    data = tmp_path / 'data'
    registry = RepositoryRegistry(tmp_path / 'profiles' / 'projects.yaml',
                                  authorized_roots=[roots], data_root=data)
    calls = []

    def clone(settings, destination):
        calls.append((settings, destination))
        shutil.copytree(source, destination)
        run_git(destination, 'update-ref', 'refs/remotes/origin/main', 'HEAD')

    monkeypatch.setattr('tracefix.repository_registry.prepare_checkout', clone)
    fields = _fields(source, 'remote-app', 'acme/remote')
    fields.pop('root')
    fields['checkout_mode'] = 'managed'
    registered = registry.register(fields)
    assert registered['root'] == str(roots / 'remote-app')
    assert registered['checkout_mode'] == 'managed'
    assert registered['publish_branch'] is False
    first = registry.freeze('remote-app', 'managed_run_one')
    assert first['repository_config']['root'] == str(data / 'frozen-repositories' / 'managed_run_one' / 'checkout')
    assert first['commit'] == registered['commit']
    (source / 'src' / 'main.py').write_text('value = 2\n', encoding='utf-8')
    run_git(source, 'add', 'src/main.py')
    safe_commit(source, '更新远端测试版本', 'Test', 'test@localhost')
    assert registry.freeze('remote-app', 'managed_run_one') == first
    second = registry.freeze('remote-app', 'managed_run_two')
    assert second['commit'] != first['commit']
    assert second['repository_config']['profile']['source_commit'] == second['commit']
    assert second['config_digest'] != first['config_digest']
    assert registry.get('remote-app') == registered
    assert run_git(Path(registered['root']), 'rev-parse', 'HEAD').decode().strip() == registered['commit']
    assert len(calls) == 3
    assert RepositoryRegistry(Path(first['projects_path'])).freeze('remote-app', 'managed_run_one') == first


@pytest.mark.parametrize('invalid,code', [
    ('profile', 'INVALID_PROFILE'), ('publish', 'INVALID_PUBLISH_BRANCH'),
    ('mode', 'INVALID_CHECKOUT_MODE'), ('files', 'INVALID_ALLOWED_FILES'),
])
def test_managed_invalid_configuration_never_clones(tmp_path, monkeypatch, invalid, code):
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path])
    fields = _fields(tmp_path / 'checkout', 'remote-app', 'acme/remote')
    fields['checkout_mode'] = 'managed'
    if invalid == 'profile':
        fields['profile'] = {'commands': {}}
    elif invalid == 'publish':
        fields['publish_branch'] = 'true'
    elif invalid == 'mode':
        fields['checkout_mode'] = 'remote'
    else:
        fields['allowed_files'] = ['../outside']

    def unexpected_clone(*arguments):
        pytest.fail('无效注册不得拉取仓库')

    monkeypatch.setattr('tracefix.repository_registry.prepare_checkout', unexpected_clone)
    with pytest.raises(RepositoryError) as rejected:
        registry.register(fields)
    assert rejected.value.code == code
    assert registry.list() == []


def test_managed_checkout_failure_does_not_register_or_freeze(tmp_path, monkeypatch):
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path])
    fields = _fields(tmp_path / 'checkout', 'remote-app', 'acme/remote')
    fields['checkout_mode'] = 'managed'

    def failing_clone(*arguments):
        raise RuntimeError('network error')

    monkeypatch.setattr('tracefix.repository_registry.prepare_checkout', failing_clone)
    with pytest.raises(RepositoryError) as rejected:
        registry.register(fields)
    assert rejected.value.code == 'CHECKOUT_FAILED'
    assert registry.list() == []
    assert not registry.path.exists()


def test_legacy_config_without_new_checkout_fields_remains_valid(tmp_path):
    root = _checkout(tmp_path, 'source', 'acme/app')
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', authorized_roots=[tmp_path], data_root=tmp_path / 'data')
    registry.register(_fields(root, 'app', 'acme/app'))
    raw = yaml.safe_load(registry.path.read_text(encoding='utf-8'))
    config = raw['projects'][0]['repository_config']
    config.pop('checkout_mode')
    config.pop('publish_branch')
    from tracefix.repository_registry import config_digest
    config['config_digest'] = config_digest(config)
    registry.path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    assert registry.get('app') == config
    assert registry.freeze('app', 'legacy_run')['commit'] == config['commit']


def test_checkout_rejects_plain_file_and_nonempty_directory_before_network(tmp_path):
    from tracefix.remote import prepare_checkout
    destination = tmp_path / 'file'
    destination.write_text('preserve', encoding='utf-8')
    with pytest.raises(ValueError, match='必须是目录'):
        prepare_checkout({'repository': 'owner/app', 'base': 'main'}, destination)
    directory = tmp_path / 'directory'
    directory.mkdir()
    (directory / 'keep').write_text('preserve', encoding='utf-8')
    with pytest.raises(ValueError, match='非空'):
        prepare_checkout({'repository': 'owner/app', 'base': 'main'}, directory)
    assert destination.read_text(encoding='utf-8') == 'preserve'
    assert (directory / 'keep').read_text(encoding='utf-8') == 'preserve'


def test_new_checkout_checks_requested_source_commit(tmp_path, monkeypatch):
    from tracefix.remote import prepare_checkout
    destination = tmp_path / 'checkout'
    clone_arguments = []

    def fake_git(root, *arguments, **options):
        if root is None:
            clone_arguments.append(arguments)
            destination.mkdir()
            run_git(destination, 'init', '-q', '--initial-branch=main')
            (destination / 'source.py').write_text('value = 1\n', encoding='utf-8')
            run_git(destination, 'add', 'source.py')
            safe_commit(destination, '初始化远程拉取测试', 'Test', 'test@localhost')
            run_git(destination, 'remote', 'add', 'origin', 'https://github.com/owner/app.git')
            run_git(destination, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
            return b''
        if arguments == ('rev-parse', '--verify', 'wrong^{commit}'):
            return b'0' * 40
        return run_git(root, *arguments, **options)

    monkeypatch.setattr('tracefix.remote.run_git', fake_git)
    with pytest.raises(PermissionError, match='source commit'):
        prepare_checkout({'repository': 'owner/app', 'base': 'main', 'source_commit': 'wrong'}, destination)
    assert clone_arguments[0][-2] == 'https://github.com/owner/app.git'
