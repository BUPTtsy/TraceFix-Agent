import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('target_preparation_tests', ROOT / 'bugboard/scripts/prepare_target.py')
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


@pytest.fixture
def remote(tmp_path):
    source = tmp_path / 'upstream'
    source.mkdir()
    (source / 'package.json').write_text('{}\n', encoding='utf-8')
    (source / 'package-lock.json').write_text('{}\n', encoding='utf-8')
    (source / 'application.txt').write_text('original\n', encoding='utf-8')
    helper.git(tmp_path, 'init', '-q', str(source))
    helper.git(source, 'add', '.')
    helper.git(source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
               'commit', '-qm', '创建离线远程测试基线')
    revision = helper.git(source, 'rev-parse', 'HEAD')
    repository = tmp_path / 'remote.git'
    helper.git(tmp_path, 'clone', '--bare', str(source), str(repository))
    return str(repository), revision


def test_clone_pins_commit_and_reuses_clean_cache_offline(tmp_path, remote, monkeypatch):
    url, revision = remote
    cache = helper.prepare_target(tmp_path, url=url, revision=revision)
    assert helper.git(cache, 'rev-parse', 'HEAD') == revision
    assert (cache / 'application.txt').read_text(encoding='utf-8') == 'original\n'
    original_git = helper.git

    def offline_git(root, *arguments):
        assert arguments[0] not in ('clone', 'fetch', 'pull')
        return original_git(root, *arguments)

    monkeypatch.setattr(helper, 'git', offline_git)
    assert helper.prepare_target(tmp_path, url=url, revision=revision) == cache


def test_dirty_cache_is_rejected_without_overwriting(tmp_path, remote):
    url, revision = remote
    cache = helper.prepare_target(tmp_path, url=url, revision=revision)
    (cache / 'application.txt').write_text('local change\n', encoding='utf-8')
    with pytest.raises(ValueError, match='拒绝覆盖'):
        helper.prepare_target(tmp_path, url=url, revision=revision)
    assert (cache / 'application.txt').read_text(encoding='utf-8') == 'local change\n'


def test_invalid_revision_cleans_partial_checkout(tmp_path, remote):
    url, revision = remote
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='测试仓库 Git 操作失败'):
        helper.prepare_target(tmp_path, url=url, revision='missing-revision', cache=cache)
    assert not cache.exists()
    assert not list(tmp_path.glob('.bugboard-clone-*'))


def test_existing_cache_source_mismatch_is_rejected(tmp_path, remote):
    url, revision = remote
    cache = helper.prepare_target(tmp_path, url=url, revision=revision)
    with pytest.raises(ValueError, match='来源'):
        helper.prepare_target(tmp_path, url=url + '-other', revision=revision, cache=cache)


@pytest.mark.parametrize('url,revision', [
    ('https://user:secret@example.com/repo.git', 'main'),
    ('https://example.com/repo.git', '--detach'),
    ('https://example.com/repo.git\n', 'main'),
])
def test_credentials_and_option_injection_are_rejected(url, revision):
    with pytest.raises(ValueError):
        helper.repository_settings(url, revision)


def test_precise_mutation_check_rejects_missing_or_duplicate_anchor(tmp_path):
    (tmp_path / 'evals').mkdir()
    (tmp_path / 'evals/cases.py').write_text(
        "CASES = [{'id':'B01','file':'application.txt','before':'anchor'}]", encoding='utf-8')
    source = tmp_path / 'source'
    source.mkdir()
    target = source / 'application.txt'
    for contents in ('missing', 'anchor anchor'):
        target.write_text(contents, encoding='utf-8')
        with pytest.raises(ValueError, match='B01'):
            helper.verify_cases(source, tmp_path)
    target.write_text('unique anchor', encoding='utf-8')
    helper.verify_cases(source, tmp_path)


def registration_tool():
    specification = importlib.util.spec_from_file_location(
        'test_repository_registration', ROOT / 'scripts/github/configure_test_repository.py')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize('exists', [False, True])
def test_configuration_uses_managed_registry_and_preserves_other_projects(tmp_path, monkeypatch, exists):
    tool = registration_tool()
    calls = []

    class Registry:
        def __init__(self, path, **options):
            calls.append(('construct', path, options))

        def get(self, identifier):
            if not exists:
                raise tool.RepositoryError('REPOSITORY_NOT_FOUND', '未注册的仓库', 404)
            return {'repo_id': identifier}

        def register(self, fields):
            calls.append(('register', fields))
            return {**fields, 'commit': 'a' * 40}

        def update(self, identifier, fields):
            calls.append(('update', fields))
            assert identifier == 'test-bug-repo'
            return {**fields, 'commit': 'a' * 40}

    monkeypatch.setattr(tool, 'RepositoryRegistry', Registry)
    monkeypatch.setattr(tool, 'load_dotenv', lambda *args, **options: None)
    root = tmp_path / 'repositories/test-bug-repo'
    assert tool.main(['--registry', str(tmp_path / 'projects.yaml'), '--data', str(tmp_path),
                      '--root', str(root)]) == 0
    assert calls[0][2]['authorized_roots'] == [root.parent]
    operation, fields = calls[1]
    assert operation == ('update' if exists else 'register')
    assert fields['checkout_mode'] == 'managed'
    assert fields['default_ref'] == 'main'
    assert fields['owner'] == 'BUPTtsy' and fields['repo'] == 'Test-Bug-repo'
    assert fields['publish_branch'] is False
    assert fields['profile']['commands']['prepare'] == ['node', '--version']
    assert fields['profile']['commands']['unit'] == ['node', '--test', 'tests/tasks.test.mjs']
    assert fields['repo_id'] != 'bugboard'


def test_configuration_rejects_publish_without_deployment_credential(monkeypatch, capsys):
    tool = registration_tool()
    monkeypatch.setattr(tool, 'load_dotenv', lambda *args, **options: None)
    monkeypatch.delenv('TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP', raising=False)
    with pytest.raises(SystemExit) as error:
        tool.main(['--publish-branch'])
    assert error.value.code == 2
    assert 'credential_ref' in capsys.readouterr().err
