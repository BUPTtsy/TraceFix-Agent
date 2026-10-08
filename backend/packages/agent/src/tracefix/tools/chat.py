"""复用运行时只读工具与项目作用域，为 Chat 提供有界的辅助查询。"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace

from pydantic import Field

from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import Contract, Phase
from tracefix.tools.local import GlobInput, GrepInput, LocalTools, ReadInput
from tracefix.tools.core import ToolPipeline, ToolRegistry, ToolRejected, build_tool
from tracefix.runtime.validation_feedback import _public
from tracefix.storage.artifacts import redact


MAX_FILE_BYTES = 2_000_000
MAX_SCAN_BYTES = 8_000_000
MAX_SCAN_FILES = 1000
PRIVATE_PATH = re.compile(
    r'(^|[._-])(oracle|oracles|held_out|held-out|heldout|hidden|private|final_scoring|final-scoring)([._-]|$)',
    re.I,
)


class DocumentSearchInput(Contract):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)


class ChatWorkspace(Workspace):
    def __init__(self, scopes, ctx):
        scopes.assert_current(ctx)
        project = scopes.projects[ctx.active_scope]
        super().__init__(project.root, project.allowed_files)
        self.scopes, self.ctx = scopes, ctx

    def path(self, relative, write=False):
        if write:
            raise PermissionError('Chat 不允许修改工作区')
        if any(PRIVATE_PATH.search(part) for part in Path(relative).parts):
            raise PermissionError('Chat 不允许读取独立判定或 held-out 路径')
        scoped = self.scopes.path(self.ctx, relative)
        path = super().path(relative)
        if scoped != path:
            raise PermissionError('Chat 项目根目录绑定不一致')
        if path.is_file() and path.stat().st_size > MAX_FILE_BYTES:
            raise ToolRejected('Chat 单文件读取超过 2 MB；请缩小输入文件')
        return path


class ChatLocalTools(LocalTools):
    def files(self, directory=None):
        base = Path(directory) if directory else self.root
        if not base.is_absolute() or not base.resolve().is_relative_to(self.root):
            raise ToolRejected('检索目录必须是工作区内的绝对路径')
        if base != self.root:
            base = self.workspace.path(base.relative_to(self.root).as_posix())
        self.workspace.scopes.assert_current(self.workspace.ctx)
        count, total_bytes = 0, 0
        if base.is_file():
            candidates = [base]
        else:
            def walk_files():
                for current, directories, filenames in os.walk(base, followlinks=False):
                    allowed = []
                    for name in directories:
                        try:
                            self.workspace.path((Path(current) / name).relative_to(self.root).as_posix())
                        except (PermissionError, ToolRejected):
                            continue
                        allowed.append(name)
                    directories[:] = allowed
                    for name in filenames:
                        yield Path(current) / name
            candidates = walk_files()
        for candidate in candidates:
            try:
                path = self.path(str(candidate))
            except PermissionError:
                continue
            size = path.stat().st_size
            count += 1
            total_bytes += size
            if count > MAX_SCAN_FILES or total_bytes > MAX_SCAN_BYTES:
                raise ToolRejected('Chat 检索超过 1000 个文件或 8 MB；请指定更小的 path')
            yield path


def build_chat_tools(scopes, ctx, library, artifacts, session_id, *, emit=None, use_knowledge=True):
    workspace = ChatWorkspace(scopes, ctx)
    local = ChatLocalTools(SimpleNamespace(workspace=workspace), {})
    registry, handlers = ToolRegistry(), {}
    phase = Phase.DIAGNOSE

    def bind(name, description, input_model, callback):
        async def handler(arguments, call_id):
            result = await asyncio.to_thread(callback, arguments)
            if not _public(result):
                raise ToolRejected('Chat 查询结果包含非公开判定数据')
            return redact(result)

        definition = build_tool(name, description, input_model, handler,
            side_effect='read', parallel_safe=True, phases=frozenset({phase}),
            output_limit_tokens=4000, category='chat')
        registry.register(definition.spec)
        handlers[name] = definition.handler

    bind('Read', '读取当前项目内的绝对路径，支持 offset/limit 分页；最大 2 MB，不修改文件。',
         ReadInput, local.read)
    bind('Glob', '在当前项目内按 glob 列出文件；path 是绝对目录，扫描有文件数和字节上限。',
         GlobInput, local.glob)
    bind('Grep', '在当前项目内进行正则内容搜索；path 是绝对路径，可用 glob/type 缩小范围。',
         GrepInput, local.grep)
    if use_knowledge:
        def search(arguments):
            return {'documents': [record for record in library.search(arguments.query, ctx.active_scope, arguments.limit)
                                  if _public(record)]}

        bind('DocumentSearch', '检索当前项目和公开全局知识文档，返回正文摘录及真实版本来源。',
             DocumentSearchInput, search)

    def store_result(result):
        return artifacts.put(ctx.active_scope, session_id, result, label='Chat工具完整结果')

    return ToolPipeline(registry, handlers, phase, emit=emit, store_artifact=store_result,
                        scope_check=lambda: scopes.assert_current(ctx))
