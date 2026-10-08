"""授权前端源码的分析工具契约、证据采集与注册。"""
from __future__ import annotations

import asyncio
import fnmatch
from typing import Literal

from pydantic import Field

from tracefix.rules.analyzers import analyze_source
from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.tools.local import LocalTools


class CodeAnalyze(Contract):
    paths: list[str] = Field(default_factory=list, max_length=100)
    path_globs: list[str] = Field(default_factory=lambda: ['**'], max_length=30)
    check: Literal['syntax', 'elements', 'a11y_name', 'event_binding', 'regex'] = 'elements'
    config: dict = Field(default_factory=dict)


def register_code_analysis_tool(engine, state, context, bind):
    async def code_analyze(arguments, call_id):
        local = LocalTools(engine, context, state)
        requested = {local.path(value).relative_to(local.root).as_posix() for value in arguments.paths}
        files = []
        versions = {}
        with local.mutex.workspace_read():
            for path in local.files():
                relative = path.relative_to(local.root).as_posix()
                if requested and relative not in requested:
                    continue
                if arguments.path_globs and not any(fnmatch.fnmatchcase(relative, pattern)
                                                    for pattern in arguments.path_globs):
                    continue
                if path.suffix.lower() not in {
                    '.js', '.mjs', '.cjs', '.jsx', '.ts', '.mts', '.cts', '.tsx', '.vue', '.html', '.htm',
                }:
                    continue
                staged = local.staged_content(relative)
                data = staged if staged is not None else path.read_bytes()
                files.append((relative, data.decode('utf-8')))
                versions[relative] = digest(data)
        result = await asyncio.to_thread(analyze_source, files,
                                        {**arguments.config, 'check': arguments.check})
        result.update(source_manifest=state.source_manifest, content_versions=versions)
        if callable(getattr(engine, 'put', None)):
            result['artifact_ref'] = engine.put(state, result, name='代码分析证据')
        return result

    bind('code.analyze', '分析授权源码的 AST 和模板结构，支持 JavaScript/TypeScript、React JSX/TSX、Vue3 SFC 和 HTML。'
         '按需检查 syntax、elements、a11y_name、event_binding 或 regex；paths 使用工作区绝对路径，省略时按 path_globs 选择。'
         '结果含实际位置、源码哈希和证据引用；status=error 时应结合页面截图、语义观察和 Read/Grep 回退检测，不能视为通过。',
         CodeAnalyze, code_analyze, phases=set(Phase), output_limit_tokens=6000,
         search_hint='AST DOM frontend React Vue JavaScript TypeScript HTML accessibility event handler')
