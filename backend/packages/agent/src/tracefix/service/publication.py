from __future__ import annotations

from pathlib import Path
import re

from tracefix.execution.repository import (plain_root, repository_binding, run_git,
                                          safe_commit, safe_index_files)
from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import digest


def _validate_paths(checkout, base, allowed_files):
    workspace = Workspace(checkout, allowed_files)
    paths = run_git(checkout, 'diff', '--name-only', '-z', base, '--').split(b'\0')
    selected = []
    for encoded in paths:
        if not encoded:
            continue
        relative = encoded.decode('utf-8')
        path = workspace.path(relative, write=True)
        if not path.is_file():
            raise PermissionError('发布补丁只能修改已有普通文件')
        selected.append(relative)
    for record in run_git(checkout, 'diff', '--raw', '--no-abbrev', base, '--').splitlines():
        metadata = record.split(b'\t', 1)[0].split()
        if len(metadata) != 5 or metadata[0][1:] != metadata[1] or metadata[4] != b'M':
            raise PermissionError('发布补丁不能增删、重命名或修改文件模式')
    if not selected:
        raise ValueError('发布补丁为空')
    return selected


def prepare_publication(result, config, settings, *, run_id):
    publication = {'status': 'skipped', 'branch': None, 'commit': None, 'error': None}
    if (result.get('outcome') != 'FIX_VERIFIED' or result.get('run_status') != 'COMPLETED'
            or result.get('patch_verification') != 'verified' or not result.get('diff')):
        return publication
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id):
            raise ValueError('发布 Run 标识无效')
        if (config.get('frozen_run_id') != run_id or result.get('run_id') != run_id
                or result.get('repo_id') != config.get('repo_id')):
            raise PermissionError('发布请求必须绑定冻结 Run 和仓库')
        raw = result['diff'].encode('utf-8')
        if digest(raw) != result['report'].get('patch_hash'):
            raise ValueError('发布补丁摘要与验收证据不一致')
        repository = config['owner'] + '/' + config['repo']
        source = plain_root(Path(config['root']))
        base = config['commit']
        repository_binding(source, repository, base, require_clean=False)
        checkout = plain_root(settings.data_root / 'publications' / run_id / 'checkout')
        branch = 'codex/tracefix-' + run_id
        previous_identity = repository_binding(checkout, repository, expected_branch=branch) if checkout.exists() else None
        if previous_identity is None:
            checkout.parent.mkdir(parents=True, exist_ok=True)
            run_git(None, 'clone', '--no-hardlinks', '--', str(source), str(checkout),
                    local_clone=True, timeout=120)
            run_git(checkout, 'remote', 'set-url', 'origin', f'https://github.com/{repository}.git')
            run_git(checkout, 'checkout', '-b', branch, base)
            statistics = run_git(checkout, 'apply', '--numstat', '-z', input_data=raw)
            workspace = Workspace(checkout, config['allowed_files'])
            for record in statistics.split(b'\0'):
                if not record:
                    continue
                additions, deletions, encoded = record.split(b'\t', 2)
                if not additions.isdigit() or not deletions.isdigit() or not encoded:
                    raise PermissionError('发布补丁不支持二进制或重命名')
                if not workspace.path(encoded.decode('utf-8'), write=True).is_file():
                    raise PermissionError('发布补丁只能修改已有文件')
            run_git(checkout, 'apply', '--check', '--index', input_data=raw)
            run_git(checkout, 'apply', '--index', input_data=raw)
            paths = _validate_paths(checkout, base, config['allowed_files'])
            actual = run_git(checkout, 'diff', '--no-color', base, '--')
            if digest(actual) != digest(raw):
                raise ValueError('应用后的差异与已验证补丁不一致')
            safe_index_files(checkout, paths)
            safe_commit(checkout, '修复经验证的缺陷：' + run_id, 'TraceFix', 'tracefix@localhost')
        identity = repository_binding(checkout, repository, expected_branch=branch)
        commit = identity['head']
        parent = run_git(checkout, 'rev-parse', 'HEAD^').decode().strip()
        if parent != base:
            raise PermissionError('发布提交必须直接继承冻结源版本')
        _validate_paths(checkout, base, config['allowed_files'])
        if digest(run_git(checkout, 'diff', '--no-color', base, 'HEAD', '--')) != digest(raw):
            raise PermissionError('发布 checkout 与已验证补丁不一致')
        publication.update(status='local', branch=branch, commit=commit)
        if config.get('publish_branch', False):
            token = settings.resolve_credential(config.get('credential_ref'))['GITHUB_PERSONAL_ACCESS_TOKEN']
            reference = 'refs/heads/' + branch
            remote_url = f'https://github.com/{repository}.git'
            existing = run_git(checkout, 'ls-remote', '--heads', remote_url, reference,
                               github_token=token, timeout=120).decode().split()
            if existing:
                if existing != [commit, reference]:
                    raise PermissionError('远端修复分支已存在且提交不同')
            else:
                run_git(checkout, 'push', '--force-with-lease=' + reference + ':',
                        remote_url, commit + ':' + reference, github_token=token, timeout=120)
            publication['status'] = 'published'
    except Exception:
        publication.update(status='failed', error={'code': 'publication_failed'})
    return publication
