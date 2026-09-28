"""项目作用域层级、路径归属和记忆可见性校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tracefix.config import Project
from tracefix.execution.platforms import safe_relative, is_link
from tracefix.execution.workspace import is_frozen_path


@dataclass(frozen=True)
class ProjectContext:
    """记录当前项目及其祖先的版本和访问时代，用于防止权限漂移。"""
    active_scope: str
    ancestors: tuple[str, ...]
    revisions: tuple[tuple[str, int], ...]
    epochs: tuple[tuple[str, int], ...]

    @property
    def readable_scopes(self):
        """返回当前项目和所有可继承读取的祖先作用域。"""
        return (self.active_scope, *self.ancestors)


class ScopeResolver:
    """解析项目树，并在每次路径或记忆访问前重新确认权限上下文。"""

    def __init__(self, projects: dict[str, Project], source_path=None):
        """校验项目 ID 和根目录唯一性后建立解析器。"""
        self.projects = projects
        self.source_path = source_path
        if len({p.casefold() for p in projects}) != len(projects):
            raise ValueError('项目 ID 忽略大小写后必须唯一，以保证证据隔离的可移植性')
        roots = [p.root.resolve() for p in projects.values()]
        if len(set(roots)) != len(roots):
            raise ValueError("项目必须绑定不同的根目录")
        for scope in projects:
            self.context(scope)

    def context(self, scope: str) -> ProjectContext:
        """沿 parent_id 链构造作用域上下文，并检测循环引用。"""
        if scope not in self.projects:
            raise PermissionError("未注册的作用域")
        chain, seen, current = [], {scope}, self.projects[scope].parent_id
        while current:
            if current in seen or current not in self.projects:
                raise ValueError("项目父级存在循环引用或未注册")
            chain.append(current)
            seen.add(current)
            current = self.projects[current].parent_id
        scopes = (scope, *chain)
        return ProjectContext(scope, tuple(chain), tuple((s, self.projects[s].memory_revision) for s in scopes),
                              tuple((s, self.projects[s].access_epoch) for s in scopes))

    def assert_current(self, ctx: ProjectContext):
        """确认配置、路径、写入策略和访问时代均未在运行中改变。"""
        if self.source_path:
            from tracefix.config import load_projects
            fresh = ScopeResolver(load_projects(self.source_path))
            new = fresh.context(ctx.active_scope)
            if new.epochs != ctx.epochs or new.ancestors != ctx.ancestors:
                raise PermissionError('项目授权已在配置中被撤销')
            for name in ctx.readable_scopes:
                old_project, new_project = self.projects[name], fresh.projects[name]
                if (old_project.root != new_project.root or
                        old_project.allowed_files != new_project.allowed_files or
                        old_project.agent_instructions != new_project.agent_instructions):
                    raise PermissionError('项目路径或写入策略已变更')
        current = self.context(ctx.active_scope)
        if current.epochs != ctx.epochs or current.ancestors != ctx.ancestors:
            raise PermissionError("作用域授权已变更；请创建新的 context")

    def owner(self, path: Path):
        """返回包含路径的最具体项目 ID；路径不属于项目时返回 None。"""
        candidates = [p for p in self.projects.values() if path.resolve().is_relative_to(p.root.resolve())]
        return max(candidates, key=lambda p: len(p.root.parts)).id if candidates else None

    def path(self, ctx: ProjectContext, relative: str, write=False) -> Path:
        """解析并授权项目内路径，拒绝越界、链接、受保护目录和未授权写入。"""
        self.assert_current(ctx)
        safe_relative(relative)
        root = self.projects[ctx.active_scope].root.resolve()
        p = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise PermissionError("路径遍历")
        if any(part in {".git", ".env", "node_modules", ".tracefix", "__pycache__"} or part.startswith('.env.') for part in p.relative_to(root).parts):
            raise PermissionError("受保护的路径")
        if any(is_link(q) for q in [p, *p.parents] if q.is_relative_to(root)):
            raise PermissionError("符号链接不可访问")
        if self.owner(p) != ctx.active_scope:
            raise PermissionError("嵌套或非本项目路径")
        if write:
            from fnmatch import fnmatchcase
            allowed = self.projects[ctx.active_scope].allowed_files
            if not any(fnmatchcase(relative, pattern) for pattern in allowed):
                raise PermissionError("写入路径不在允许列表中")
            # 与 Workspace.path(write=True) 共用同一实现，避免两处策略产生分歧。
            if is_frozen_path(relative):
                raise PermissionError("冻结的测试/判定/锁文件不可被补丁修改")
        elif relative == self.projects[ctx.active_scope].agent_instructions:
            return p
        return p

    def readable_memory(self, item: dict, ctx: ProjectContext, source_revision: str | None = None):
        """判断记忆是否同时满足可信、作用域、版本和源快照可见性。"""
        self.assert_current(ctx)
        if item["status"] != "trusted":
            return False
        if item["scope_id"] not in ctx.readable_scopes:
            return False
        if item["scope_id"] != ctx.active_scope and item["visibility"] != "DESCENDANTS":
            return False
        if item["revision"] > dict(ctx.revisions)[item["scope_id"]]:
            return False
        return source_revision is None or item["source_revision"] in {"*", source_revision}
