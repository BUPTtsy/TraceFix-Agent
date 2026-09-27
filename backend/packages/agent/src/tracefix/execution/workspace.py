import difflib
import os
import re
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from tracefix.runtime.contracts import PatchProposal, digest
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
    """补丁路径是否命中冻结的测试/判定/锁文件策略。

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


def git(root: Path, *args):
    p = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                        "-c", "core.quotePath=false", "-C", str(root), *args], capture_output=True, timeout=30)
    if p.returncode:
        raise RuntimeError(f'Git 命令执行失败（退出码 {p.returncode}）：' + p.stderr.decode(errors="replace")[:500])
    return p.stdout


class Workspace:
    def __init__(self, root: Path, allowed_files: list[str]):
        self.root = root.resolve()
        self.allowed_files = allowed_files

    @classmethod
    def export(cls, scopes, ctx, commit: str, target: Path):
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
        git(target, "-c", "user.name=TraceFix", "-c", "user.email=tracefix@localhost", "commit", "-qm", "Frozen authorized source export")
        return workspace, {"commit": resolved, "files": manifest, "subdir": prefix}

    def path(self, relative, write=False):
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
        return p

    def read(self, relative):
        p = self.path(relative)
        if p.stat().st_size > 200_000:
            raise ValueError("文件过大")
        # read_text's universal-newline conversion would invalidate CRLF hashes.
        return p.read_bytes().decode('utf-8')

    def files(self):
        for p in sorted(self.root.rglob('*')):
            if p.is_file() and p.suffix in {'.ts', '.tsx', '.js', '.mjs', '.css'}:
                rel = p.relative_to(self.root).as_posix()
                try:
                    self.path(rel)
                except PermissionError:
                    continue
                yield rel

    def cards(self, limit_chars=60_000, preferred_paths=()):
        result, total = [], 0
        ordered = sorted(self.files(), key=lambda p: (p not in preferred_paths, p))
        for path in ordered:
            body = self.read(path)
            if total + len(body) > limit_chars:
                continue
            total += len(body)
            result.append({"path": path, "content": body, "before_hash": digest(body.encode())})
        return result

    def apply(self, patch: PatchProposal):
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
        # All validation occurs before any write. Recovery reconciles the complete
        # before/after set; mixed states remain UNKNOWN and require manual review.
        for p, before, after in changes:
            tmp = p.with_name(p.name + '.tracefix-tmp')
            tmp.write_bytes(after)
            os.replace(tmp, p)
        return {"patch_hash": digest(self.diff().encode()), "files": [e.path for e in patch.edits]}

    def reconcile(self, patch: PatchProposal):
        hashes = [digest(self.path(e.path).read_bytes()) for e in patch.edits]
        if all(h == digest(e.content.encode()) for h, e in zip(hashes, patch.edits)):
            return {"patch_hash": digest(self.diff().encode()), "files": [e.path for e in patch.edits]}
        raise RuntimeError("补丁状态未知；请先检查工作区再恢复")

    def diff(self):
        return git(self.root, "diff", "--no-ext-diff", "--no-color", "HEAD").decode()

    def check_frozen(self, source_manifest):
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
        if not name.startswith('tracefix/run_'):
            raise PermissionError("本地分支名称无效")
        existing = git(self.root, "branch", "--list", name).decode().strip()
        if not existing:
            git(self.root, "checkout", "-qb", name)
            git(self.root, "add", ".")
            git(self.root, "-c", "user.name=TraceFix", "-c", "user.email=tracefix@localhost", "commit", "-qm", "TraceFix verified candidate")
        return name
