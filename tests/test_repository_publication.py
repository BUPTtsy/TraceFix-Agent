import base64
import subprocess
from types import SimpleNamespace

import pytest

from tracefix.execution.repository import run_git, safe_commit
from tracefix.runtime.contracts import digest
from tracefix.service.publication import prepare_publication


def fixture(tmp_path, *, publish=False):
    source = tmp_path / 'source'
    source.mkdir()
    run_git(source, 'init', '-q', '--initial-branch=main')
    (source / 'src').mkdir()
    (source / 'src' / 'main.py').write_text('value = 1\n', encoding='utf-8')
    run_git(source, 'add', 'src/main.py')
    safe_commit(source, '初始化测试基线', 'Test', 'test@localhost')
    run_git(source, 'remote', 'add', 'origin', 'https://github.com/acme/app.git')
    base = run_git(source, 'rev-parse', 'HEAD').decode().strip()
    (source / 'src' / 'main.py').write_text('value = 2\n', encoding='utf-8')
    raw = run_git(source, 'diff', '--no-color', 'HEAD', '--')
    run_git(source, 'checkout', '--', 'src/main.py')
    result = {'run_id': 'run_one', 'repo_id': 'app', 'outcome': 'FIX_VERIFIED',
              'run_status': 'COMPLETED', 'patch_verification': 'verified', 'diff': raw.decode(),
              'report': {'patch_hash': digest(raw)}}
    config = {'root': str(source), 'owner': 'acme', 'repo': 'app', 'commit': base,
              'repo_id': 'app', 'frozen_run_id': 'run_one', 'allowed_files': ['src/**'],
              'publish_branch': publish, 'credential_ref': 'deploy/app'}
    settings = SimpleNamespace(data_root=tmp_path / 'data', resolve_credential=lambda reference:
        {'GITHUB_PERSONAL_ACCESS_TOKEN': 'unit-test-token'})
    return source, result, config, settings


def test_publication_uses_source_history_and_retries_idempotently(tmp_path):
    source, result, config, settings = fixture(tmp_path)
    publication = prepare_publication(result, config, settings, run_id='run_one')
    assert publication['status'] == 'local'
    checkout = settings.data_root / 'publications' / 'run_one' / 'checkout'
    assert run_git(checkout, 'rev-parse', 'HEAD^').decode().strip() == config['commit']
    assert run_git(checkout, 'rev-parse', 'HEAD').decode().strip() == publication['commit']
    assert run_git(source, 'rev-parse', 'HEAD').decode().strip() == config['commit']
    assert prepare_publication(result, config, settings, run_id='run_one') == publication


@pytest.mark.parametrize('fault', ['unverified', 'wrong_hash', 'outside', 'wrong_run', 'frozen_test'])
def test_publication_rejects_unverified_unbound_or_unauthorized_changes(tmp_path, fault):
    source, result, config, settings = fixture(tmp_path)
    if fault == 'unverified':
        result['patch_verification'] = 'unverified'
    elif fault == 'wrong_hash':
        result['report']['patch_hash'] = 'forged'
    elif fault == 'outside':
        config['allowed_files'] = ['server/**']
    elif fault == 'frozen_test':
        result['diff'] = result['diff'].replace('main.py', 'main.test.py')
        result['report']['patch_hash'] = digest(result['diff'].encode())
        config['allowed_files'] = ['**']
    else:
        config['frozen_run_id'] = 'different_run'
    publication = prepare_publication(result, config, settings, run_id='run_one')
    assert publication['status'] == ('skipped' if fault == 'unverified' else 'failed')
    assert publication['commit'] is None
    assert run_git(source, 'status', '--porcelain') == b''


def test_publication_push_creates_only_new_branch_and_is_idempotent(tmp_path, monkeypatch):
    source, result, config, settings = fixture(tmp_path, publish=True)
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    pushes = []

    def git(root, *arguments, **options):
        if arguments[0] in {'push', 'ls-remote'}:
            assert options.pop('github_token') == 'unit-test-token'
            if arguments[0] == 'push':
                pushes.append(arguments)
                command = ['git', '-C', str(root), 'push', arguments[1], str(remote), arguments[3]]
            else:
                command = ['git', 'ls-remote', '--heads', str(remote), arguments[3]]
            return subprocess.run(command, check=True, capture_output=True).stdout
        return run_git(root, *arguments, **options)

    monkeypatch.setattr('tracefix.service.publication.run_git', git)
    first = prepare_publication(result, config, settings, run_id='run_one')
    assert first['status'] == 'published'
    assert '--force-with-lease=refs/heads/codex/tracefix-run_one:' == pushes[0][1]
    assert prepare_publication(result, config, settings, run_id='run_one') == first
    assert len(pushes) == 1
    parent = subprocess.run(['git', '--git-dir', str(remote), 'rev-parse', first['commit'] + '^'],
                            check=True, capture_output=True).stdout.decode().strip()
    assert parent == config['commit']


def test_push_failure_preserves_local_publication_commit(tmp_path, monkeypatch):
    source, result, config, settings = fixture(tmp_path, publish=True)

    def failing_git(root, *arguments, **options):
        if arguments[0] == 'ls-remote':
            raise RuntimeError('remote unavailable')
        return run_git(root, *arguments, **options)

    monkeypatch.setattr('tracefix.service.publication.run_git', failing_git)
    publication = prepare_publication(result, config, settings, run_id='run_one')
    assert publication['status'] == 'failed'
    assert publication['branch'] == 'codex/tracefix-run_one'
    assert len(publication['commit']) == 40
    assert result['patch_verification'] == 'verified'


def test_git_authentication_stays_in_child_environment_and_errors_are_redacted(tmp_path, monkeypatch):
    source, result, config, settings = fixture(tmp_path)
    real_run = subprocess.run
    captured = []
    token = 'secret-value'
    authorization = 'AUTHORIZATION: basic ' + base64.b64encode(('x-access-token:' + token).encode()).decode()

    def process(command, **options):
        if 'push' in command:
            captured.append((command, options['env']))
            return SimpleNamespace(returncode=1, stdout=b'', stderr=(token + authorization).encode())
        return real_run(command, **options)

    monkeypatch.setattr('tracefix.execution.repository.subprocess.run', process)
    with pytest.raises(RuntimeError) as rejected:
        run_git(source, 'push', 'https://github.com/acme/app.git', 'HEAD:refs/heads/codex/test', github_token=token)
    assert token not in str(rejected.value) and authorization not in str(rejected.value)
    assert token not in ' '.join(captured[0][0])
    assert captured[0][1]['GIT_CONFIG_VALUE_0'] == authorization
