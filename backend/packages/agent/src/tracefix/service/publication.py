from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat

from tracefix.execution.repository import (plain_root, repository_binding, run_git,
                                          safe_commit, safe_index_files)
from tracefix.execution.workspace import Workspace


def _verify_commit(checkout, base, files, workspace):
    """复核交付提交恰好包含授权改动集合，且只含普通 100644 文件。"""
    commit = run_git(checkout, 'rev-parse', 'HEAD').decode().strip()
    if run_git(checkout, 'rev-list', '--parents', '-n', '1', commit).decode().split() != [commit, base]:
        raise PermissionError('发布提交必须直接继承冻结源版本')
    paths = run_git(checkout, 'diff', '--no-renames', '--name-only', '-z', base, commit, '--').split(b'\0')
    selected = sorted(encoded.decode('utf-8') for encoded in paths if encoded)
    if selected != sorted(files):
        raise PermissionError('发布提交的改动集合与已验收内容不一致')
    for relative in selected:
        workspace.path(relative, write=True)
        if run_git(checkout, 'cat-file', 'blob', commit + ':' + relative) != files[relative]:
            raise PermissionError('发布提交内容与已验收内容不一致')
    records = run_git(checkout, 'diff', '--raw', '--no-abbrev', '--no-renames', '-z', base, commit, '--').split(b'\0')
    for record in records[0:-1:2]:
        metadata = record.split()
        if (len(metadata) != 5 or metadata[1] != b'100644'
                or (metadata[4] == b'M' and metadata[0] != b':100644')
                or (metadata[4] == b'A' and metadata[0] != b':000000')
                or metadata[4] not in {b'M', b'A'}):
            raise PermissionError('发布补丁不能删除、重命名或修改文件模式')
    return commit


def _discard(checkout):
    def writable(function, path, _error):
        os.chmod(path, stat.S_IWRITE)
        function(path)

    shutil.rmtree(checkout, onexc=writable)


def publish_files(config, data_root, *, run_id, files, resolve_credential=None, approved_test_files=()):
    """在注册仓库的独立 clone 中按文件写入已验收内容，生成交付提交并按配置推送。"""
    publication = {'status': 'failed', 'branch': None, 'commit': None, 'error': None}
    created = None
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id):
            raise ValueError('发布 Run 标识无效')
        if config.get('frozen_run_id') != run_id:
            raise PermissionError('发布请求必须绑定冻结 Run 和仓库')
        if not files or any(not isinstance(content, bytes) for content in files.values()):
            raise ValueError('发布改动为空或内容无效')
        repository = config['owner'] + '/' + config['repo']
        source = plain_root(Path(config['root']))
        base = config['commit']
        repository_binding(source, repository, base, require_clean=False)
        checkout = plain_root(Path(data_root) / 'publications' / run_id / 'checkout')
        branch = 'codex/tracefix-' + run_id
        workspace = Workspace(checkout, config['allowed_files'],
            protected_paths=config.get('protected_paths', []), approved_test_files=approved_test_files)
        previous_identity = repository_binding(checkout, repository, expected_branch=branch) if checkout.exists() else None
        if previous_identity is None:
            created = checkout
            checkout.parent.mkdir(parents=True, exist_ok=True)
            run_git(None, 'clone', '--no-hardlinks', '--', str(source), str(checkout),
                    local_clone=True, timeout=120)
            run_git(checkout, 'remote', 'set-url', 'origin', f'https://github.com/{repository}.git')
            run_git(checkout, 'checkout', '-b', branch, base)
            for relative in sorted(files):
                path = workspace.path(relative, write=True)
                if path.exists() and not path.is_file():
                    raise PermissionError('发布补丁只能写入普通文件')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(files[relative])
            safe_index_files(checkout, sorted(files))
            safe_commit(checkout, '修复经验证的缺陷：' + run_id, 'TraceFix', 'tracefix@localhost')
            created = None
        identity = repository_binding(checkout, repository, expected_branch=branch)
        commit = _verify_commit(checkout, base, files, workspace)
        if identity['head'] != commit:
            raise PermissionError('发布 checkout 身份不一致')
        publication.update(status='local', branch=branch, commit=commit)
        if config.get('publish_branch', False):
            token = resolve_credential(config.get('credential_ref'))['GITHUB_PERSONAL_ACCESS_TOKEN']
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
        if created is not None:
            # 本次新建且尚未提交的半成品 checkout 会阻断重试，清理后可重新交付。
            try:
                _discard(created)
            except OSError:
                pass
    return publication
