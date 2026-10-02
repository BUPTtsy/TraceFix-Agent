"""工作区路径授权、补丁原子应用和冻结证据校验。"""

import difflib
import os
import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from tracefix.runtime.contracts import PatchProposal, digest
from tracefix.runtime.guidance import GuidanceRejected
from tracefix.execution.platforms import safe_relative, is_link

# 冻结证据的词元：测试与判定(oracle)不得被 Agent 自己的补丁改写。
FROZEN_TOKENS = frozenset({'test', 'tests', 'conftest', 'spec', 'specs', 'oracle', 'oracles'})
# 锁文件只按「文件名整体形态」识别（见 is_frozen_path），它保护的是可复现构建而非判定，
# 因此不作为通用词元，以免误伤 useLock.ts / LockService.ts / lock/ 这类业务代码。
LOCKFILE_PATTERNS = ('*.lock', '*.lockb', '*lock.json', '*lock.yaml', '*lock.yml', 'npm-shrinkwrap.json')


def tokens(part: str):
    """把一个路径段切成小写整词；camelCase 边界与非字母字符同样是词边界。"""
    # 必须先按 camelCase 补分隔符再转小写，否则 'UserServiceTest' 会退化成单个词元。
    return set(re.split(r'[^a-z]+', re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', part).lower()))


def is_frozen_path(relative) -> bool:
    """判断补丁路径是否属于测试、判定或锁文件等冻结证据。

    唯一的策略实现；knowledge.scope.ScopeResolver.path 复用本函数，两处判定必须一致。
    每个路径段（目录与文件名同规则）切词后做整词匹配，而不是子串匹配：
    'foo.test.ts'、'test_foo.py'、'UserServiceTest.java'、'MyComponentSpec.js'、
    'conftest.py'、'tests/'、'__tests__/' 命中；锁文件另按文件名形态单独判定。
    'latest-run/'、'blockchain/'、'Testimonial.tsx'、'useLock.ts' 不再被误伤。
    """
    parts = Path(relative).parts
    if parts and any(fnmatchcase(parts[-1].lower(), pattern) for pattern in LOCKFILE_PATTERNS):
        return True
    return any(FROZEN_TOKENS & tokens(part) for part in parts)


def git(root: Path, *args, env=None):
    p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                        "-c", "core.quotePath=false", "-C", str(root), *args], capture_output=True, timeout=30, env=env)
    if p.returncode:
        raise RuntimeError(f'Git 命令执行失败（退出码 {p.returncode}）：' + p.stderr.decode(errors="replace")[:500])
    return p.stdout


def commit_workspace(root: Path, message: str):
    name = os.getenv('TRACEFIX_GIT_AUTHOR_NAME', '').strip() or 'TraceFix'
    email = os.getenv('TRACEFIX_GIT_AUTHOR_EMAIL', '').strip() or 'tracefix@localhost'
    environment = dict(os.environ)
    environment.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                       GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return git(root, 'commit', '-qm', message, env=environment)


class Workspace:
    """在项目根目录内提供经过路径和文件白名单校验的读写接口。"""

    def __init__(self, root: Path, allowed_files: list[str]):
        """绑定根目录和允许写入的 glob 模式。"""
        self.root = root.resolve()
        self.allowed_files = allowed_files
        self.guidance_constraints = []
        # Local edit tools write into this overlay until a PatchProposal is
        # accepted by the normal patch transaction.  Keeping the overlay out
        # of the working tree preserves the frozen reproduction baseline.
        self._staged_files = {}

    @classmethod
    def export(cls, scopes, ctx, commit: str, target: Path):
        """将指定提交导出到临时目录，并构造对应的工作区对象。"""
        source = scopes.projects[ctx.active_scope].root
        top = Path(git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        resolved = git(source, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
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
            data = git(top, "cat-file", "blob", blob)
            if len(data) > 2_000_000:
                raise ValueError("源文件超过 2 MB；请排除生成的资源文件")
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            manifest[relative] = digest(data)
        if not manifest:
            raise ValueError("源码导出为空")
        workspace = cls(target, scopes.projects[ctx.active_scope].allowed_files)
        git(target, "init", "-q")
        git(target, "add", ".")
        commit_workspace(target, "Frozen authorized source export")
        return workspace, {"commit": resolved, "files": manifest, "subdir": prefix}

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
        if write and (not any(fnmatchcase(relative, x) for x in self.allowed_files)
                      or is_frozen_path(relative)):
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
        p = self.path(relative)
        if p.stat().st_size > 200_000:
            raise ValueError("文件过大")
        # read_text's universal-newline conversion would invalidate CRLF hashes.
        staged = self._staged_files.get(relative)
        return (staged if staged is not None else p.read_bytes()).decode('utf-8')

    def staged_content(self, relative):
        return self._staged_files.get(relative)

    def stage(self, relative, content):
        """Stage an existing authorized file and enforce guidance limits."""
        p = self.path(relative, write=True)
        if not p.exists() or not p.is_file():
            raise PermissionError('本地编辑工具不支持创建新文件')
        data = content.encode('utf-8')
        if not data.strip():
            raise ValueError('不能暂存空文件')
        previous = self._staged_files.get(relative)
        self._staged_files[relative] = data
        try:
            self._validate_staged_constraints()
        except Exception:
            if previous is None:
                self._staged_files.pop(relative, None)
            else:
                self._staged_files[relative] = previous
            raise
        return {'path': str(p), 'after_hash': digest(data), 'staged': True}

    def stage_many(self, changes):
        previous = dict(self._staged_files)
        try:
            for relative, content in changes.items():
                p = self.path(relative, write=True)
                if not p.exists() or not p.is_file():
                    raise PermissionError('本地编辑工具不支持创建新文件')
                data = content.encode('utf-8') if isinstance(content, str) else content
                if not data.strip():
                    raise ValueError('不能暂存空文件')
                self._staged_files[relative] = data
            self._validate_staged_constraints()
        except Exception:
            self._staged_files = previous
            raise
        return {'files_changed': list(changes), 'staged': True}

    def clear_staged(self):
        self._staged_files.clear()

    def _validate_staged_constraints(self):
        if not self.guidance_constraints:
            return
        changed_paths = set()
        changed_lines = 0
        for relative, data in self._staged_files.items():
            baseline = self.path(relative).read_bytes()
            if baseline == data:
                continue
            self.path(relative, write=True)
            changed_paths.add(relative)
            before = baseline.decode('utf-8').splitlines(keepends=True)
            after = data.decode('utf-8').splitlines(keepends=True)
            changed_lines += sum(end_before - start_before + end_after - start_after
                                 for kind, start_before, end_before, start_after, end_after
                                 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes()
                                 if kind != 'equal')
        # Include committed working-tree edits as well as staged changes.
        for relative in git(self.root, 'diff', '--name-only', 'HEAD').decode().splitlines():
            if relative in self._staged_files:
                continue
            current = self.path(relative).read_bytes()
            baseline = git(self.root, 'show', 'HEAD:' + relative)
            if current == baseline:
                continue
            changed_paths.add(relative)
            before = baseline.decode('utf-8').splitlines(keepends=True)
            after = current.decode('utf-8').splitlines(keepends=True)
            changed_lines += sum(end_before - start_before + end_after - start_after
                                 for kind, start_before, end_before, start_after, end_after
                                 in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes()
                                 if kind != 'equal')
        for constraint in self.guidance_constraints:
            if constraint.max_files_changed is not None and len(changed_paths) > constraint.max_files_changed:
                raise GuidanceRejected('暂存修改超过用户约束的文件数', details={
                    'actual_files': len(changed_paths), 'max_files_changed': constraint.max_files_changed})
            if constraint.max_lines_changed is not None and changed_lines > constraint.max_lines_changed:
                raise GuidanceRejected('暂存修改超过用户约束的行数', details={
                    'actual_lines': changed_lines, 'max_lines_changed': constraint.max_lines_changed})

    def files(self):
        """枚举根目录下可读的普通文件，排除链接和受保护目录。"""
        for p in sorted(self.root.rglob('*')):
            if p.is_file() and p.suffix in {'.ts', '.tsx', '.js', '.mjs', '.css'}:
                rel = p.relative_to(self.root).as_posix()
                try:
                    self.path(rel)
                except PermissionError:
                    continue
                yield rel

    def cards(self, limit_chars=60_000, preferred_paths=()):
        """按偏好顺序收集受字符预算限制的文件上下文卡片。"""
        result, total = [], 0
        ordered = sorted(self.files(), key=lambda p: (p not in preferred_paths, p))
        for path in ordered:
            body = self.read(path)
            if total + len(body) > limit_chars:
                continue
            total += len(body)
            baseline = self.path(path).read_bytes()
            result.append({"path": path, "content": body, "before_hash": digest(baseline)})
        return result

    def validate_guidance_patch(self, patch: PatchProposal):
        # 相对当前 HEAD 计算已有改动与候选补丁的累计变化，执行用户指定的总量限制。
        if not self.guidance_constraints:
            return
        replacements = dict(self._staged_files)
        replacements.update({edit.path: edit.content.encode('utf-8') for edit in patch.edits})
        paths = set(git(self.root, 'diff', '--name-only', 'HEAD').decode().splitlines()) | set(replacements)
        changed_paths, changed_lines = [], 0
        for relative in sorted(paths):
            current = replacements.get(relative)
            if current is None:
                current = self.path(relative).read_bytes()
            baseline = git(self.root, 'show', 'HEAD:' + relative)
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

    def apply(self, patch: PatchProposal):
        """先完整校验补丁，再逐文件替换，返回可恢复的补丁摘要。"""
        if len({e.path for e in patch.edits}) != len(patch.edits):
            raise ValueError("存在重复的编辑路径")
        changes = []
        for edit in patch.edits:
            p = self.path(edit.path, write=True)
            old = p.read_bytes()
            if digest(old) != edit.before_hash:
                raise ValueError("基准哈希不匹配；请重新读取文件")
            if not edit.content.strip() or old == edit.content.encode():
                raise ValueError("空补丁或无实际改动")
            changes.append((p, old, edit.content.encode()))
        self.validate_guidance_patch(patch)
        # 所有校验在首次写入前完成；恢复时核对完整 before/after 集合。
        # 文件只部分替换时保持 UNKNOWN，由人工检查后再恢复。
        for p, before, after in changes:
            tmp = p.with_name(p.name + '.tracefix-tmp')
            tmp.write_bytes(after)
            os.replace(tmp, p)
        self.clear_staged()
        return {"patch_hash": digest(self.diff().encode()), "files": [e.path for e in patch.edits]}

    def reconcile(self, patch: PatchProposal):
        """确认磁盘状态是否与补丁的全部目标内容一致。"""
        hashes = [digest(self.path(e.path).read_bytes()) for e in patch.edits]
        if all(h == digest(e.content.encode()) for h, e in zip(hashes, patch.edits)):
            return {"patch_hash": digest(self.diff().encode()), "files": [e.path for e in patch.edits]}
        raise RuntimeError("补丁状态未知；请先检查工作区再恢复")

    def diff(self):
        """返回工作区相对 HEAD 的纯文本差异。"""
        return git(self.root, "diff", "--no-ext-diff", "--no-color", "HEAD").decode()

    def check_frozen(self, source_manifest):
        """验证文件集合和冻结文件摘要未被运行过程悄然改变。"""
        tracked = set(source_manifest['files'])
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

    def branch(self, name):
        """创建并提交验证分支，确保分支名称只能属于本次运行命名空间。"""
        if not name.startswith('tracefix/run_'):
            raise PermissionError("本地分支名称无效")
        existing = git(self.root, "branch", "--list", name).decode().strip()
        if not existing:
            git(self.root, "checkout", "-qb", name)
            git(self.root, "add", ".")
            commit_workspace(self.root, "TraceFix verified candidate")
        return name
