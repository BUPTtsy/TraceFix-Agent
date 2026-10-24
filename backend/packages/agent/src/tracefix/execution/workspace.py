"""工作区路径授权、补丁原子应用和冻结证据校验。"""

import difflib
import os
import re
import tempfile
from fnmatch import fnmatchcase
from pathlib import Path

from tracefix.runtime.contracts import PatchProposal, digest
from tracefix.runtime.guidance import GuidanceRejected
from tracefix.execution.platforms import safe_relative, is_link
from tracefix.execution.repository import (file_manifest, plain_root, repository_binding,
    run_git, safe_commit, safe_index_files)

# 冻结证据的词元：测试与判定(oracle)不得被 Agent 自己的补丁改写。
FROZEN_TOKENS = frozenset({'test', 'tests', 'conftest', 'spec', 'specs', 'oracle', 'oracles'})
# 锁文件只按「文件名整体形态」识别（见 is_frozen_path），它保护的是可复现构建而非判定，
# 因此不作为通用词元，以免误伤 useLock.ts / LockService.ts / lock/ 这类业务代码。
LOCKFILE_PATTERNS = ('*.lock', '*.lockb', '*lock.json', '*lock.yaml', '*lock.yml', 'npm-shrinkwrap.json')
# 快照与 CI 配置也属于不可由修复过程改写的判定/执行依据。
PROTECTED_PATTERNS = ('**/__snapshots__/**', '*.snap', '.github/**', '.gitlab-ci.yml',
                      '.gitlab-ci.yaml', '.circleci/**', 'Jenkinsfile', 'Jenkinsfile.*')


def validate_protected_paths(value):
    """校验项目追加的相对 glob；默认策略始终独立保留。"""
    if not isinstance(value, (list, tuple)):
        raise ValueError('protected_paths 必须是相对路径列表')
    for pattern in value:
        if (not isinstance(pattern, str) or not pattern or '\\' in pattern or ':' in pattern
                or pattern.startswith('/') or any(ord(char) < 32 for char in pattern)
                or any(part in {'', '.', '..'} for part in pattern.split('/'))):
            raise ValueError('protected_paths 必须是不含路径遍历的相对 glob')
    return list(dict.fromkeys(value))


def is_protected_path(relative, protected_paths=()) -> bool:
    """共用默认保护策略和项目追加策略，兼容根目录的 **/ 匹配。"""
    normalized = str(relative).replace('\\', '/').casefold()
    return any(fnmatchcase(normalized, pattern.casefold())
               or (pattern.startswith('**/') and fnmatchcase(normalized, pattern[3:].casefold()))
               for pattern in (*PROTECTED_PATTERNS, *protected_paths))


class ProtectedPathError(PermissionError):
    error_code = 'protected_path'

    def __init__(self, relative):
        normalized = str(relative).replace('\\', '/')
        super().__init__('受保护路径不可被补丁修改：' + normalized)
        self.details = {'path': normalized}


def tokens(part: str):
    """把一个路径段切成小写整词；camelCase 边界与非字母字符同样是词边界。"""
    # 必须先按 camelCase 补分隔符再转小写，否则 'UserServiceTest' 会退化成单个词元。
    return set(re.split(r'[^a-z]+', re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', part).lower()))


def is_frozen_path(relative, protected_paths=()) -> bool:
    """判断补丁路径是否属于测试、判定或锁文件等冻结证据。

    唯一的策略实现；knowledge.scope.ScopeResolver.path 复用本函数，两处判定必须一致。
    每个路径段（目录与文件名同规则）切词后做整词匹配，而不是子串匹配：
    'foo.test.ts'、'test_foo.py'、'UserServiceTest.java'、'MyComponentSpec.js'、
    'conftest.py'、'tests/'、'__tests__/' 命中；锁文件另按文件名形态单独判定。
    'latest-run/'、'blockchain/'、'Testimonial.tsx'、'useLock.ts' 不再被误伤。
    """
    if is_protected_path(relative, protected_paths):
        return True
    parts = Path(relative).parts
    if parts and any(fnmatchcase(parts[-1].lower(), pattern) for pattern in LOCKFILE_PATTERNS):
        return True
    return any(FROZEN_TOKENS & tokens(part) for part in parts)


def test_write_is_approved(relative, approved_test_files=(), protected_paths=()):
    """人工精确文件审批只能放开测试代码，不能覆盖判定或执行依据。"""
    safe_relative(relative)
    parts = Path(relative).parts
    if (is_protected_path(relative, protected_paths)
            or any({'oracle', 'oracles'} & tokens(part) for part in parts)
            or any(fnmatchcase(parts[-1].lower(), pattern) for pattern in LOCKFILE_PATTERNS)):
        return False
    if not any({'test', 'tests', 'conftest', 'spec', 'specs'} & tokens(part) for part in parts):
        return False
    return relative in approved_test_files


def git(root: Path, *args, env=None):
    if env is not None:
        raise ValueError('Git 环境只能由安全仓库 helper 构造')
    return run_git(root, *args)


def commit_workspace(root: Path, message: str):
    name = os.getenv('TRACEFIX_GIT_AUTHOR_NAME', '').strip() or 'TraceFix'
    email = os.getenv('TRACEFIX_GIT_AUTHOR_EMAIL', '').strip() or 'tracefix@localhost'
    return safe_commit(root, message, name, email)


class Workspace:
    """在项目根目录内提供经过路径和文件白名单校验的读写接口。"""

    def __init__(self, root: Path, allowed_files: list[str], *, protected_paths=(), approved_test_files=()):
        """绑定根目录和允许写入的 glob 模式。"""
        self.root = plain_root(root)
        self.allowed_files = allowed_files
        self.protected_paths = validate_protected_paths(protected_paths)
        # 仅可信人工审批恢复入口设置；工具参数不得传入此授权。
        self.approved_test_files = tuple(approved_test_files)
        self.guidance_constraints = []
        self._created_entries = {}
        self.repository_snapshot = None

    @classmethod
    def export(cls, scopes, ctx, commit: str, target: Path):
        """将指定提交导出到临时目录，并构造对应的工作区对象。"""
        scopes.assert_current(ctx)
        project = scopes.projects[ctx.active_scope]
        source = plain_root(project.root)
        top = Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        identity = repository_binding(top, project.repo_id, commit, require_clean=False)
        resolved = git(source, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
        target = plain_root(target)
        if target.exists():
            raise FileExistsError("该 Run 的工作区已存在")
        target.mkdir(parents=True)
        prefix = source.relative_to(top).as_posix()
        manifest = {}
        for record in git(top, "ls-tree", "-rz", resolved).split(b'\0'):
            if not record:
                continue
            meta, raw_name = record.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            name = raw_name.decode()
            path = top / name
            if not path.is_relative_to(source):
                continue
            relative = path.relative_to(source).as_posix()
            try:
                scopes.path(ctx, relative)
            except PermissionError:
                continue
            if mode == "120000" or kind != "blob":
                raise PermissionError("授权导出中存在符号链接/子模块；请先展平已批准的依赖")
            if mode != '100644':
                raise PermissionError('授权导出暂不支持可执行文件模式；必须使用保留执行位的显式迁移')
            data = git(top, "cat-file", "blob", blob)
            if len(data) > 2_000_000:
                raise ValueError("源文件超过 2 MB；请排除生成的资源文件")
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            manifest[relative] = digest(data)
        if not manifest:
            raise ValueError("源码导出为空")
        workspace = cls(target, project.allowed_files,
                        protected_paths=(project.repository_config or {}).get('protected_paths', []))
        git(target, "init", "-q", '--initial-branch=tracefix/export')
        safe_index_files(target, sorted(manifest))
        commit_workspace(target, "Frozen authorized source export")
        workspace.prepare_mountpoints(create=True)
        snapshot = {"commit": resolved, "files": manifest, "subdir": prefix,
            'repository_version': 1, 'scope_id': ctx.active_scope, 'repo_id': project.repo_id,
            'source_root': str(source), 'source_repository': identity,
            'source_tree': git(top, 'rev-parse', f'{resolved}^{{tree}}').decode().strip(),
            'workspace_root': str(workspace.root), 'workspace_repository': repository_binding(target),
            'entries': file_manifest(target)}
        if project.repository_config:
            from copy import deepcopy
            snapshot['repository_config'] = deepcopy(project.repository_config)
        workspace.repository_snapshot = snapshot
        return workspace, snapshot

    def prepare_mountpoints(self, *, create=False):
        paths = [self.root / name for name in ('dist', 'node_modules')]
        for path in paths:
            if is_link(path) or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                raise PermissionError('运行期挂载点必须是空的普通目录：' + path.name)
            if not create and not path.is_dir():
                raise PermissionError('绑定快照的挂载目录已删除：' + path.name)
        if create:
            for path in paths:
                path.mkdir(exist_ok=True)

    def require_repository_snapshot(self, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.repository_snapshot
        required = {'repository_version', 'scope_id', 'repo_id', 'source_root', 'source_repository',
                    'source_tree', 'workspace_root', 'workspace_repository', 'commit', 'files', 'entries', 'subdir'}
        if (not isinstance(snapshot, dict) or not required <= snapshot.keys()
                or snapshot['repository_version'] != 1):
            raise PermissionError('历史源码快照缺少仓库身份；须人工迁移或创建新 Run，不能直接恢复')
        for name in ('source_repository', 'workspace_repository'):
            binding = snapshot[name]
            if not isinstance(binding, dict) or not {'root', 'repo_id', 'origin', 'head', 'base', 'tree', 'branch', 'clean'} <= binding.keys():
                raise PermissionError('仓库身份快照不完整')
        if not isinstance(snapshot['entries'], dict) or not snapshot['files']:
            raise PermissionError('仓库文件 manifest 不完整')
        if snapshot['workspace_root'] != str(self.root) or snapshot['workspace_repository']['root'] != str(self.root):
            raise PermissionError('沙箱工作区真实 root 与快照绑定不一致')
        if not isinstance(snapshot['files'], dict) or any(
                not isinstance(entry, dict) or not {'kind', 'tracked', 'mode', 'git_mode', 'content'} <= entry.keys()
                for entry in snapshot['entries'].values()):
            raise PermissionError('文件 manifest 类型记录不完整')
        for relative, content in snapshot['files'].items():
            entry = snapshot['entries'].get(relative, {})
            if entry.get('kind') != 'file' or entry.get('tracked') is not True or entry.get('content') != content:
                raise PermissionError('源码摘要与 tracked 文件 manifest 不一致')
        for name in ('dist', 'node_modules'):
            entry = snapshot['entries'].get(name, {})
            if entry.get('kind') != 'directory' or entry.get('tracked') is not False or entry.get('content') != '':
                raise PermissionError('快照未固定空的运行期挂载目录')
        return snapshot

    def validate_repository(self, scopes, ctx, snapshot, *, base_commit=None,
                            patch_hash=None, branch=None):
        snapshot = self.require_repository_snapshot(snapshot)
        scopes.assert_current(ctx)
        project = scopes.projects[ctx.active_scope]
        if project.repository_config:
            from tracefix.repository_registry import validate_stored_config
            validate_stored_config(project)
            if snapshot.get('repository_config') != project.repository_config:
                raise PermissionError('源码快照与冻结仓库配置不一致')
            if snapshot['commit'] != project.repository_config['commit']:
                raise PermissionError('源码快照 commit 与仓库配置不一致')
        self.protected_paths = validate_protected_paths(
            (project.repository_config or {}).get('protected_paths', []))
        source = plain_root(project.root)
        if (snapshot['scope_id'] != ctx.active_scope or snapshot['repo_id'] != project.repo_id
                or snapshot['source_root'] != str(source)
                or snapshot['workspace_root'] != str(self.root)):
            raise PermissionError('源码快照与 scope/repo/root 绑定不一致')
        expected_source = snapshot['source_repository']
        current_source = repository_binding(Path(expected_source['root']), project.repo_id,
                                            snapshot['commit'], require_clean=False)
        top = Path(git(source, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        if str(top) != expected_source['root'] or current_source != expected_source:
            raise PermissionError('源仓库 origin/HEAD/branch/commit 身份已变化')
        if git(top, 'rev-parse', f"{snapshot['commit']}^{{tree}}").decode().strip() != snapshot['source_tree']:
            raise PermissionError('源仓库树与快照不一致')
        expected_workspace = snapshot['workspace_repository']
        current = repository_binding(self.root, require_clean=False)
        baseline = base_commit or expected_workspace['head']
        if baseline != expected_workspace['head']:
            raise PermissionError('补丁 baseline 与导出仓库绑定不一致')
        if (current['origin'] != expected_workspace['origin']
                or current['branch'] not in {branch, expected_workspace['branch']}
                or git(self.root, 'rev-parse', f'{baseline}^{{tree}}').decode().strip() != expected_workspace['tree']):
            raise PermissionError('工作区 origin/branch/baseline 身份不一致')
        if current['head'] != baseline:
            if not branch or current['branch'] != branch:
                raise PermissionError('工作区 HEAD 未绑定候选分支')
            try:
                git(self.root, 'merge-base', '--is-ancestor', baseline, current['head'])
            except RuntimeError as error:
                raise PermissionError('工作区 baseline 不是当前 HEAD 的祖先') from error
            commits = git(self.root, 'rev-list', f'{baseline}..{current["head"]}', '--').decode().splitlines()
            for commit in commits:
                tree_files = {}
                for record in git(self.root, 'ls-tree', '-rz', commit).split(b'\0'):
                    if record:
                        metadata, name = record.split(b'\t', 1)
                        mode, kind, blob = metadata.decode().split()
                        relative = name.decode()
                        if mode != '100644' or kind != 'blob':
                            raise PermissionError('候选提交存在链接/类型变化')
                        tree_files[relative] = digest(git(self.root, 'cat-file', 'blob', blob))
                if not snapshot['files'].keys() <= tree_files.keys():
                    raise PermissionError('候选提交存在文件删除')
                for relative, content in tree_files.items():
                    if content != snapshot['files'].get(relative):
                        try:
                            self.path(relative, write=True)
                        except PermissionError as error:
                            raise PermissionError(f'候选提交包含未授权路径：{relative}（{error}）') from error
        diff = self.diff(base=baseline)
        if (patch_hash and digest(diff.encode()) != patch_hash) or (not patch_hash and diff):
            raise PermissionError('原任务补丁已变化，请核对工作区后继续')
        previous_entries = self._created_entries
        try:
            # 重启后只能从已绑定 patch_hash 的索引差异恢复新增 manifest。
            if patch_hash:
                self._created_entries = self._new_entries(snapshot, file_manifest(self.root))
            self.check_frozen(snapshot)
        except Exception:
            self._created_entries = previous_entries
            raise
        self.repository_snapshot = snapshot
        return snapshot

    def path(self, relative, write=False):
        """解析相对路径，同时检查符号链接、冻结文件和写入白名单。"""
        safe_relative(relative)
        p = self.root / relative
        parts = Path(relative).parts
        if Path(relative).is_absolute() or '..' in parts or not p.resolve().is_relative_to(self.root):
            raise PermissionError("工作区路径逃逸")
        if any(is_link(q) for q in [p, *p.parents] if q.is_relative_to(self.root)):
            raise PermissionError("符号链接")
        if any(v in {'.git', '.env', 'node_modules', '.tracefix'} or v.startswith('.env.') for v in parts):
            raise PermissionError("受保护的文件")
        if write and is_protected_path(relative, self.protected_paths):
            raise ProtectedPathError(relative)
        if write and (not any(fnmatchcase(relative, x) for x in self.allowed_files)
                      or (is_frozen_path(relative) and not test_write_is_approved(
                          relative, self.approved_test_files, self.protected_paths))):
            raise PermissionError("补丁路径被拒绝")
        if write:
            normalized = relative.replace('\\', '/')
            # 用户约束叠加在项目写入白名单上，取交集而不扩大原有授权范围。
            for constraint in self.guidance_constraints:
                if ((constraint.include_paths and not any(fnmatchcase(normalized, pattern) for pattern in constraint.include_paths))
                        or any(fnmatchcase(normalized, pattern) for pattern in constraint.exclude_paths)):
                    raise GuidanceRejected('补丁路径违反用户约束：' + normalized,
                                           details={'path': normalized})
        return p

    def read(self, relative):
        """读取已授权文件的 UTF-8 文本内容。"""
        # read_text's universal-newline conversion would invalidate CRLF hashes.
        data = self.path(relative).read_bytes()
        if len(data) > 200_000:
            raise ValueError("文件过大")
        return data.decode('utf-8')

    def baseline_bytes(self, relative):
        """不存在的新文件以空字节计算哈希，存在性另由 operation 绑定。"""
        path = self.path(relative)
        if any(parent.exists() and not parent.is_dir() for parent in path.parents if parent.is_relative_to(self.root)):
            raise PermissionError('补丁目标的父路径必须是目录')
        if path.exists() and not path.is_file():
            raise PermissionError('补丁目标必须是普通文件')
        return path.read_bytes() if path.exists() else b''

    def files(self):
        """枚举根目录下可读的普通文件，排除链接和受保护目录。"""
        from tracefix.rules.codegraph import CODEGRAPH_SUPPORTED_SUFFIXES
        for p in sorted(self.root.rglob('*')):
            if p.is_file() and p.suffix in CODEGRAPH_SUPPORTED_SUFFIXES | {'.css', '.json', '.html', '.htm'}:
                rel = p.relative_to(self.root).as_posix()
                try:
                    self.path(rel)
                except PermissionError:
                    continue
                yield rel

    def validate_guidance_patch(self, patch: PatchProposal):
        # 相对当前 HEAD 计算已有改动与候选补丁的累计变化，执行用户指定的总量限制。
        if not self.guidance_constraints:
            return
        replacements = {edit.path: edit.content.encode('utf-8') for edit in patch.edits}
        paths = set(git(self.root, 'diff', '--name-only', 'HEAD').decode().splitlines()) | set(replacements)
        changed_paths, changed_lines = [], 0
        for relative in sorted(paths):
            current = replacements.get(relative)
            if current is None:
                current = self.path(relative).read_bytes()
            baseline = self._commit_bytes('HEAD', relative)
            if current == baseline:
                continue
            self.path(relative, write=True)
            changed_paths.append(relative)
            before = baseline.decode('utf-8').splitlines(keepends=True)
            after = current.decode('utf-8').splitlines(keepends=True)
            changed_lines += sum(before_end - before_start + after_end - after_start
                                 for kind, before_start, before_end, after_start, after_end
                                 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes()
                                 if kind != 'equal')
        for constraint in self.guidance_constraints:
            if constraint.max_files_changed is not None and len(changed_paths) > constraint.max_files_changed:
                raise GuidanceRejected('补丁超过用户约束的文件数', details={
                    'actual_files': len(changed_paths), 'max_files_changed': constraint.max_files_changed})
            if constraint.max_lines_changed is not None and changed_lines > constraint.max_lines_changed:
                raise GuidanceRejected('补丁超过用户约束的改动行数', details={
                    'actual_lines': changed_lines, 'max_lines_changed': constraint.max_lines_changed})

    def apply(self, patch: PatchProposal, base='HEAD'):
        """先完整校验补丁，再逐文件替换，返回可恢复的补丁摘要。"""
        if len({e.path for e in patch.edits}) != len(patch.edits):
            raise ValueError("存在重复的编辑路径")
        if self.repository_snapshot:
            self.check_frozen(self.repository_snapshot)
        changes = []
        for edit in patch.edits:
            p = self.path(edit.path, write=True)
            exists = p.exists()
            operation = getattr(edit, 'operation', 'modify')
            if (operation not in {'modify', 'create'} or exists != (operation == 'modify')):
                raise ValueError('补丁操作与目标存在性不匹配')
            old = self.baseline_bytes(edit.path)
            if digest(old) != edit.before_hash:
                raise ValueError("基准哈希不匹配；请重新读取文件")
            if not edit.content.strip() or old == edit.content.encode():
                raise ValueError("空补丁或无实际改动")
            changes.append((p, old if exists else None, edit.content.encode()))
        self.validate_guidance_patch(patch)
        new_paths = [p.relative_to(self.root).as_posix() for p, before, _ in changes if before is None]
        created_dirs, written = [], []
        index = self.root / '.git' / 'index'
        index_before = index.read_bytes() if index.exists() else None
        entries_before = dict(self._created_entries)
        try:
            for p, before, after in changes:
                self.path(p.relative_to(self.root).as_posix(), write=True)
                if p.exists() != (before is not None) or self.baseline_bytes(p.relative_to(self.root).as_posix()) != (before or b''):
                    raise ValueError('补丁物化前磁盘已漂移')
                missing = []
                parent = p.parent
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    created_dirs.append(directory)
                self._replace_bytes(p, after)
                written.append((p, before))
            # Git diff 必须包含新文件，供验证与发布使用同一标准补丁字节。
            if new_paths:
                safe_index_files(self.root, new_paths)
            if self.repository_snapshot:
                self._created_entries = self._new_entries(self.repository_snapshot, file_manifest(self.root))
                self.check_frozen(self.repository_snapshot)
            result = {"patch_hash": digest(self.diff(base).encode()), "files": [e.path for e in patch.edits],
                      "base_commit": base}
        except Exception:
            for p, before in reversed(written):
                if before is None:
                    p.unlink()
                else:
                    self._replace_bytes(p, before)
            for directory in reversed(created_dirs):
                directory.rmdir()
            if index_before is None:
                index.unlink(missing_ok=True)
            else:
                self._replace_bytes(index, index_before)
            self._created_entries = entries_before
            raise
        return result

    @staticmethod
    def _replace_bytes(path, data):
        # 随机临时文件不能覆盖仓库中同名的用户文件。
        descriptor, temporary = tempfile.mkstemp(prefix='.tracefix-', dir=path.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit_bytes(self, base, relative):
        self.path(relative)
        if not git(self.root, 'ls-tree', '-z', base, '--', relative):
            return b''
        return git(self.root, 'show', base + ':' + relative)

    def commit_bytes(self, base, relative):
        """只读取得指定提交的授权文件，新路径以空字节为修复基线。"""
        return self._commit_bytes(base, relative)

    def reconcile(self, patch: PatchProposal, base='HEAD'):
        """确认磁盘状态是否与补丁的全部目标内容一致。"""
        hashes = [digest(self.path(e.path, write=True).read_bytes()) for e in patch.edits]
        if all(h == digest(e.content.encode()) for h, e in zip(hashes, patch.edits)):
            new_paths = [e.path for e in patch.edits if getattr(e, 'operation', 'modify') == 'create']
            for relative in new_paths:
                if ((self.repository_snapshot and relative in self.repository_snapshot['files'])
                        or git(self.root, 'ls-tree', '-z', base, '--', relative)):
                    raise RuntimeError('新增补丁目标已存在于冻结基线')
            if new_paths:
                safe_index_files(self.root, new_paths)
            if self.repository_snapshot:
                entries = self._new_entries(self.repository_snapshot, file_manifest(self.root))
                permitted = set(new_paths) | {relative for relative, entry in self._created_entries.items()
                                              if entry['kind'] == 'file'}
                if any(entry['kind'] == 'file' and relative not in permitted
                       for relative, entry in entries.items()):
                    raise RuntimeError('补丁状态含未登记新增文件')
                self._created_entries = entries
                self.check_frozen(self.repository_snapshot)
            return {"patch_hash": digest(self.diff(base).encode()), "files": [e.path for e in patch.edits],
                    "base_commit": base}
        raise RuntimeError("补丁状态未知；请先检查工作区再恢复")

    def head(self):
        return git(self.root, 'rev-parse', '--verify', 'HEAD').decode().strip()

    def diff(self, base='HEAD'):
        """返回工作区相对指定基线的纯文本差异。"""
        return git(self.root, "diff", "--no-ext-diff", '--no-textconv', "--no-color", base, '--').decode()

    def check_frozen(self, source_manifest):
        """验证文件集合和冻结文件摘要未被运行过程悄然改变。"""
        source_manifest = self.require_repository_snapshot(source_manifest)
        if source_manifest.get('repository_version') == 1:
            self.require_repository_snapshot(source_manifest)
            self.prepare_mountpoints()
            actual_entries = file_manifest(self.root)
            # 登记新增项只能扩展基线，不能覆盖或删改冻结 manifest。
            if source_manifest['entries'].keys() & self._created_entries.keys():
                raise PermissionError('新增 manifest 覆盖冻结条目')
            valid_created = self._new_entries(source_manifest, actual_entries)
            if valid_created.keys() != self._created_entries.keys():
                raise PermissionError('工作区 manifest 存在未登记新增或删除')
            expected_entries = {**source_manifest['entries'], **self._created_entries}
            if actual_entries.keys() != expected_entries.keys():
                raise PermissionError('工作区 manifest 文件/目录集合变化')
            for relative, expected in expected_entries.items():
                actual_entry = actual_entries[relative]
                if any(actual_entry[key] != expected[key] for key in ('kind', 'tracked', 'mode', 'git_mode')):
                    raise PermissionError('工作区 manifest 文件类型/链接/索引变化：' + relative)
                if actual_entry['content'] != expected['content']:
                    self.path(relative, write=True)
        tracked = set(source_manifest['files']) | {relative for relative, entry in self._created_entries.items()
                                                  if entry['kind'] == 'file'}
        actual = set()
        for p in self.root.rglob('*'):
            rel = p.relative_to(self.root).as_posix()
            if '.git' in p.relative_to(self.root).parts:
                continue
            if p.is_file():
                self.path(rel)
                actual.add(rel)
        if actual != tracked:
            raise PermissionError("补丁约定之外发生了文件新增/删除")
        for rel, expected in source_manifest['files'].items():
            if digest(self.path(rel).read_bytes()) != expected:
                self.path(rel, write=True)

    def _new_entries(self, snapshot, actual_entries):
        """验证新增项均为授权索引普通文件及其必要父目录。"""
        extra = {relative: entry for relative, entry in actual_entries.items()
                 if relative not in snapshot['entries']}
        parents = set()
        for relative, entry in extra.items():
            if not isinstance(entry, dict):
                raise PermissionError('新增 manifest 记录无效')
            if entry.get('kind') != 'file':
                continue
            self.path(relative, write=True)
            if entry.get('tracked') is not True or entry.get('git_mode') != '100644' or entry.get('mode', 0) & 0o111:
                raise PermissionError('新增文件必须是已登记的普通索引文件')
            parents.update(parent.as_posix() for parent in Path(relative).parents if parent != Path('.'))
        for relative, entry in extra.items():
            if entry.get('kind') == 'directory':
                if relative not in parents or entry.get('tracked') is not False or entry.get('git_mode') is not None:
                    raise PermissionError('新增目录不是补丁文件的必要父目录')
            elif entry.get('kind') != 'file':
                raise PermissionError('新增 manifest 含非普通文件')
        return extra

    def branch(self, name):
        """创建并提交验证分支，确保分支名称只能属于本次运行命名空间。"""
        if not isinstance(name, str) or not re.fullmatch(r'tracefix/[A-Za-z0-9_-]{1,80}', name):
            raise PermissionError("本地分支名称无效")
        existing = git(self.root, "branch", "--list", name).decode().strip()
        if existing:
            if git(self.root, 'branch', '--show-current').decode().strip() != name:
                raise PermissionError('候选分支与当前工作区不匹配')
        else:
            git(self.root, "checkout", "-qb", name)
        if git(self.root, 'status', '--porcelain').strip():
            if self.repository_snapshot:
                self.check_frozen(self.repository_snapshot)
            paths = set(self.repository_snapshot['files'] if self.repository_snapshot else
                        git(self.root, 'ls-files', '-z').decode().split('\0')[:-1])
            paths.update(relative for relative, entry in self._created_entries.items() if entry['kind'] == 'file')
            safe_index_files(self.root, sorted(paths))
            commit_workspace(self.root, "提交通过验证的修复候选")
        return name
