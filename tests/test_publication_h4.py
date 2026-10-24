from types import SimpleNamespace

import pytest

from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.service.publication import publish_files


def publication_case(tmp_path, *, path='src/new.py'):
    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src/value.py').write_bytes(b'value = 1\n')
    run_git(source, 'init', '-q', '--initial-branch=main')
    safe_index_files(source, ['src/value.py'])
    safe_commit(source, '测试发布基线', 'Test', 'test@localhost')
    run_git(source, 'remote', 'add', 'origin', 'https://github.com/acme/app.git')
    base = run_git(source, 'rev-parse', 'HEAD').decode().strip()
    files = {path: b'answer = 42\n'}
    config = {'root': str(source), 'owner': 'acme', 'repo': 'app', 'commit': base,
              'repo_id': 'app', 'frozen_run_id': 'run_h4', 'allowed_files': ['**']}
    settings = SimpleNamespace(data_root=tmp_path / 'data')
    return source, files, config, settings


def test_publication_adds_authorized_new_file_and_retry_is_idempotent(tmp_path):
    source, files, config, settings = publication_case(tmp_path, path='src/new/helpers.py')
    first = publish_files(config, settings.data_root, run_id='run_h4', files=files)
    assert first['status'] == 'local'
    checkout = settings.data_root / 'publications/run_h4/checkout'
    assert (checkout / 'src/new/helpers.py').read_bytes() == b'answer = 42\n'
    assert run_git(checkout, 'rev-parse', 'HEAD^').decode().strip() == config['commit']
    assert run_git(source, 'rev-parse', 'HEAD').decode().strip() == config['commit']
    assert publish_files(config, settings.data_root, run_id='run_h4', files=files) == first


def test_publication_modifies_existing_file_with_exact_bytes(tmp_path):
    _, _, config, settings = publication_case(tmp_path)
    # CRLF 与非 UTF-8 字节必须逐字节保留，不能被换行或编码转换改写。
    files = {'src/value.py': b'value = 2\r\n', 'src/blob.bin': b'\xff\xfe\x00data'}
    publication = publish_files(config, settings.data_root, run_id='run_h4', files=files)
    assert publication['status'] == 'local'
    checkout = settings.data_root / 'publications/run_h4/checkout'
    for relative, content in files.items():
        assert run_git(checkout, 'cat-file', 'blob', publication['commit'] + ':' + relative) == content
    names = run_git(checkout, 'diff', '--name-only', config['commit'], publication['commit'], '--')
    assert sorted(names.decode().split()) == sorted(files)


@pytest.mark.parametrize('fault', ['protected', 'ci', 'oracle', 'test', 'empty', 'wrong_run', 'tampered'])
def test_publication_keeps_protection_guards(tmp_path, fault):
    path = {'protected': 'src/new.py', 'ci': '.github/workflows/new.yml',
            'oracle': 'tests/oracle/value.py', 'test': 'tests/new.py'}.get(fault, 'src/new.py')
    _, files, config, settings = publication_case(tmp_path, path=path)
    if fault == 'protected':
        config['protected_paths'] = ['src/new.py']
    if fault == 'empty':
        files = {}
    if fault == 'wrong_run':
        config['frozen_run_id'] = 'different_run'
    # 配置中的裸文件清单不构成人工审批。
    config['approved_test_files'] = [path]
    if fault == 'tampered':
        first = publish_files(config, settings.data_root, run_id='run_h4', files=files)
        assert first['status'] == 'local'
        files = {path: b'answer = 43\n'}
    publication = publish_files(config, settings.data_root, run_id='run_h4', files=files)
    assert publication['status'] == 'failed'
    assert publication['error'] == {'code': 'publication_failed'}
    if fault != 'tampered':
        assert publication['commit'] is None
        # 失败后不留半成品 checkout，修正后可以重试。
        assert not (settings.data_root / 'publications/run_h4/checkout').exists()


def test_failed_publication_can_be_retried_after_fix(tmp_path):
    _, files, config, settings = publication_case(tmp_path, path='tests/new.py')
    assert publish_files(config, settings.data_root, run_id='run_h4', files=files)['status'] == 'failed'
    retry = publish_files(config, settings.data_root, run_id='run_h4', files=files,
                          approved_test_files=('tests/new.py',))
    assert retry['status'] == 'local'


def test_publication_accepts_verified_caller_approval_only_for_exact_test_file(tmp_path):
    _, files, config, settings = publication_case(tmp_path, path='tests/new.py')
    publication = publish_files(config, settings.data_root, run_id='run_h4', files=files,
                                approved_test_files=('tests/new.py',))
    assert publication['status'] == 'local'
