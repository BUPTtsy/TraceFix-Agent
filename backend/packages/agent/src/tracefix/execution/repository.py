"""受限 Git 仓库身份、配置和文件清单操作。"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def safe_git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_')}
    environment.update({
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_OPTIONAL_LOCKS': '0',
        'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_ATTR_NOSYSTEM': '1',
    })
    return environment


def _git_prefix() -> list[str]:
    disabled = Path(tempfile.gettempdir()) / f'tracefix-disabled-{uuid.uuid4().hex}'
    return [
        'git', '-c', f'core.hooksPath={disabled / "hooks"}',
        '-c', f'init.templateDir={disabled / "template"}',
        '-c', 'diff.external=', '-c', 'core.fsmonitor=false',
        '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false',
        '-c', 'core.quotePath=false', '-c', 'commit.gpgsign=false',
        '-c', 'core.attributesFile=' + os.devnull,
        '-c', 'credential.helper=', '-c', 'core.sshCommand=false',
        '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
    ]


def run_git(root: Path | None, *args: str, input_data: bytes | None = None,
            timeout: int = 30, check: bool = True, identity: tuple[str, str] | None = None) -> bytes:
    command = _git_prefix()
    if root is not None:
        root = plain_root(root)
        for parent in (root, *root.parents):
            metadata = parent / '.git'
            if linked(metadata):
                raise PermissionError('Git 元数据入口不能是链接')
            if metadata.exists():
                if any((metadata / relative).exists() for relative in
                       ('commondir', 'config.worktree', 'objects/info/alternates')):
                    raise PermissionError('Git 元数据不能引用外部对象目录')
                for relative in ('config', 'config.worktree', 'HEAD', 'objects', 'refs', 'index'):
                    if linked(metadata / relative):
                        raise PermissionError('Git 元数据包含链接')
                break
        command.extend(['-C', str(root)])
        configuration = subprocess.run(command + ['config', '--local', '--no-includes', '--list', '-z'],
                                       capture_output=True, timeout=30, env=safe_git_environment())
        for record in configuration.stdout.decode(errors='replace').split('\0'):
            key, _, value = record.partition('\n')
            if key.lower() == 'core.worktree' or (key.lower() == 'core.bare' and value.lower() not in {'false', 'no', 'off', '0'}):
                raise PermissionError('本地 core.worktree/core.bare 重定向不受支持')
            if key.lower().startswith(('include.', 'includeif.')):
                raise PermissionError('仓库配置包含外部 include，拒绝执行')
            match = re.fullmatch(r'filter\.(.+)\.(clean|smudge|process|required)', key, re.IGNORECASE)
            if match:
                driver = match.group(1)
                for option in ('clean', 'smudge', 'process', 'required'):
                    command.extend(['-c', f'filter.{driver}.{option}=' + ('false' if option == 'required' else '')])
    arguments = list(args)
    for operation in ('diff', 'show', 'log'):
        if operation in arguments:
            position = arguments.index(operation) + 1
            arguments[position:position] = ['--no-ext-diff', '--no-textconv']
            break
    command.extend(arguments)
    environment = safe_git_environment()
    if identity:
        environment.update(GIT_AUTHOR_NAME=identity[0], GIT_AUTHOR_EMAIL=identity[1],
                           GIT_COMMITTER_NAME=identity[0], GIT_COMMITTER_EMAIL=identity[1])
    result = subprocess.run(command, input=input_data, capture_output=True,
                            timeout=timeout, env=environment, cwd=root or tempfile.gettempdir())
    if check and result.returncode:
        detail = result.stderr.decode(errors='replace').strip()
        raise RuntimeError(f'安全 Git 操作失败（退出码 {result.returncode}）：{detail[:500]}')
    return result.stdout


def repository_root(root: Path) -> Path:
    root = plain_root(root)
    metadata = root / '.git'
    if not metadata.is_dir() or linked(metadata):
        raise PermissionError('工作区根目录或 .git 入口不可信')
    top = Path(run_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if top != root:
        raise PermissionError('工作区必须绑定真实 Git 根目录')
    return top


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def plain_root(root: Path) -> Path:
    root = Path(os.path.abspath(Path(root).expanduser()))
    if any(linked(path) for path in (root, *root.parents)):
        raise PermissionError('工作区根目录不能经过链接')
    return root.resolve()


def canonical_remote(value: str) -> str:
    value = value.strip()
    if value.startswith('git@') and ':' in value:
        host, value = value.split(':', 1)
        if host.casefold() != 'git@github.com':
            return host.casefold() + ':' + value
    elif '://' in value:
        parsed = urlsplit(value)
        if parsed.hostname != 'github.com' or parsed.username or parsed.query or parsed.fragment:
            return value
        value = parsed.path.strip('/')
    value = value.removesuffix('.git').strip('/')
    return value.casefold()


def repository_binding(root: Path, expected_repo_id: str | None = None,
                      source_commit: str | None = None,
                      expected_branch: str | None = None,
                      require_clean: bool = True) -> dict:
    top = repository_root(root)
    origin = run_git(top, 'config', '--get-all', 'remote.origin.url', check=False).decode().strip()
    if '\n' in origin:
        raise PermissionError('仓库 origin 必须唯一')
    if not origin and expected_repo_id and '/' in expected_repo_id:
        raise PermissionError('仓库缺少 origin，不能建立可迁移身份')
    if origin and expected_repo_id and '/' in expected_repo_id and canonical_remote(origin) != canonical_remote(expected_repo_id):
        raise PermissionError('origin 与项目 repo_id 不一致')
    head = run_git(top, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    base = None
    if source_commit:
        base = run_git(top, 'rev-parse', '--verify', f'{source_commit}^{{commit}}').decode().strip()
    branch = run_git(top, 'branch', '--show-current').decode().strip()
    if expected_branch and branch != expected_branch:
        raise PermissionError('工作区当前分支与绑定分支不一致')
    status = run_git(top, 'status', '--porcelain=v1', '--untracked-files=all').decode()
    if require_clean and status:
        raise PermissionError('复用前 Git 工作区必须干净')
    return {
        'root': str(top), 'repo_id': expected_repo_id or canonical_remote(origin), 'origin': origin,
        'head': head, 'base': base or head, 'tree': run_git(top, 'rev-parse', f'{head}^{{tree}}').decode().strip(),
        'branch': branch, 'clean': not bool(status),
    }


def _walk_entries(root: Path):
    def visit(directory: Path, prefix: str = ''):
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            relative = f'{prefix}/{entry.name}' if prefix else entry.name
            path = Path(entry.path)
            if entry.name == '.git' and not prefix:
                continue
            if linked(path):
                yield relative, path, 'symlink'
            elif entry.is_dir(follow_symlinks=False):
                yield relative, path, 'directory'
                yield from visit(path, relative)
            elif entry.is_file(follow_symlinks=False):
                yield relative, path, 'file'
            else:
                yield relative, path, 'other'
    yield from visit(root)


def file_manifest(root: Path) -> dict[str, dict]:
    root = repository_root(root)
    tracked = {}
    for record in run_git(root, 'ls-files', '--stage', '-z').split(b'\0'):
        if record:
            metadata, name = record.split(b'\t', 1)
            mode, blob, stage = metadata.decode().split()
            if stage != '0' or mode not in {'100644', '100755'}:
                raise PermissionError('Git 索引含冲突或非普通文件')
            tracked[name.decode(errors='surrogateescape')] = mode
    manifest = {}
    for relative, path, kind in _walk_entries(root):
        if kind in {'other', 'symlink'}:
            raise PermissionError(f'不支持的文件类型：{relative}')
        file_stat = path.lstat()
        manifest[relative] = {
            'kind': kind, 'tracked': relative in tracked,
            'mode': file_stat.st_mode & 0o777,
            'git_mode': tracked.get(relative),
            'content': hashlib.sha256(path.read_bytes()).hexdigest() if kind == 'file' else '',
        }
    if set(tracked) != {relative for relative, entry in manifest.items() if entry['tracked']}:
        raise PermissionError('Git 索引存在缺失文件或文件类型变化')
    return manifest


def safe_index_files(root: Path, paths: list[str]) -> None:
    root = repository_root(root)
    for relative in paths:
        path = root / relative
        if any(linked(parent) for parent in (path, *path.parents)) or not path.is_file():
            raise PermissionError(f'不能将非普通文件写入安全索引：{relative}')
        blob = run_git(root, 'hash-object', '-w', '--no-filters', '--', relative,
                       timeout=30)
        run_git(root, 'update-index', '--add', '--cacheinfo', f'100644,{blob.decode().strip()},{relative}')


def safe_commit(root: Path, message: str, author_name: str, author_email: str) -> str:
    tree = run_git(root, 'write-tree').decode().strip()
    parent = run_git(root, 'rev-parse', '--verify', 'HEAD', check=False).decode().strip()
    args = ['commit-tree', tree, '-m', message]
    if parent:
        args[1:1] = ['-p', parent]
    commit = run_git(root, *args, identity=(author_name, author_email)).decode().strip()
    run_git(root, 'update-ref', 'HEAD', commit)
    return commit
