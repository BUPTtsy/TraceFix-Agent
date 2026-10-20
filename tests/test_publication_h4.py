from types import SimpleNamespace

import pytest

from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.runtime.contracts import digest
from tracefix.service.publication import prepare_publication


def publication_case(tmp_path, *, path='src/new.py', delete=False):
    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src/value.py').write_bytes(b'value = 1\n')
    run_git(source, 'init', '-q', '--initial-branch=main')
    safe_index_files(source, ['src/value.py'])
    safe_commit(source, '测试发布基线', 'Test', 'test@localhost')
    run_git(source, 'remote', 'add', 'origin', 'https://github.com/acme/app.git')
    base = run_git(source, 'rev-parse', 'HEAD').decode().strip()
    if delete:
        (source / 'src/value.py').unlink()
    else:
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'answer = 42\n')
        safe_index_files(source, [path])
    raw = run_git(source, 'diff', '--no-color', base, '--')
    result = {'run_id': 'run_h4', 'repo_id': 'app', 'outcome': 'FIX_VERIFIED',
              'run_status': 'COMPLETED', 'patch_verification': 'verified', 'diff': raw.decode(),
              'report': {'patch_hash': digest(raw)}}
    config = {'root': str(source), 'owner': 'acme', 'repo': 'app', 'commit': base,
              'repo_id': 'app', 'frozen_run_id': 'run_h4', 'allowed_files': ['**']}
    settings = SimpleNamespace(data_root=tmp_path / 'data')
    return source, result, config, settings


def test_publication_adds_authorized_new_file_and_retry_is_idempotent(tmp_path):
    source, result, config, settings = publication_case(tmp_path, path='src/new/helpers.py')
    first = prepare_publication(result, config, settings, run_id='run_h4')
    assert first['status'] == 'local'
    checkout = settings.data_root / 'publications/run_h4/checkout'
    assert (checkout / 'src/new/helpers.py').read_bytes() == b'answer = 42\n'
    assert run_git(checkout, 'rev-parse', 'HEAD^').decode().strip() == config['commit']
    assert run_git(source, 'rev-parse', 'HEAD').decode().strip() == config['commit']
    assert prepare_publication(result, config, settings, run_id='run_h4') == first


@pytest.mark.parametrize('fault', ['delete', 'protected', 'ci', 'oracle', 'test'])
def test_publication_keeps_deletion_and_protection_guards(tmp_path, fault):
    path = {'protected': 'src/new.py', 'ci': '.github/workflows/new.yml',
            'oracle': 'tests/oracle/value.py', 'test': 'tests/new.py'}.get(fault, 'src/new.py')
    _, result, config, settings = publication_case(tmp_path, path=path, delete=fault == 'delete')
    if fault == 'protected':
        config['protected_paths'] = ['src/new.py']
    # 配置中的裸文件清单不构成人工审批。
    config['approved_test_files'] = [path]
    publication = prepare_publication(result, config, settings, run_id='run_h4')
    assert publication['status'] == 'failed'
    assert publication['commit'] is None


def test_publication_accepts_verified_caller_approval_only_for_exact_test_file(tmp_path):
    _, result, config, settings = publication_case(tmp_path, path='tests/new.py')
    publication = prepare_publication(result, config, settings, run_id='run_h4',
                                      approved_test_files=('tests/new.py',))
    assert publication['status'] == 'local'
