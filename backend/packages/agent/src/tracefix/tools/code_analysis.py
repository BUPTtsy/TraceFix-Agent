"""授权前端源码的分析工具契约、证据采集与注册。"""
from __future__ import annotations

import asyncio
import fnmatch
from typing import Literal

from pydantic import Field

from tracefix.rules.analyzers import analyze_source
from tracefix.rules.codegraph import CODEGRAPH_SUPPORTED_SUFFIXES, RepositoryCodeGraph, is_graph_file
from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.tools.local import LocalTools


class CodeAnalyze(Contract):
    paths: list[str] = Field(default_factory=list, max_length=100)
    path_globs: list[str] = Field(default_factory=lambda: ['**'], max_length=30)
    check: Literal['symbols', 'context', 'callers', 'callees', 'impact',
                   'syntax', 'elements', 'a11y_name', 'event_binding', 'regex'] = 'symbols'
    config: dict = Field(default_factory=dict)


def register_code_analysis_tool(engine, state, context, bind):
    async def code_analyze(arguments, call_id):
        local = LocalTools(engine, context, state)
        requested = {local.path(value).relative_to(local.root).as_posix() for value in arguments.paths}
        files = []
        graph_files = []
        versions = {}
        with local.mutex.workspace_read():
            for path in local.files():
                relative = path.relative_to(local.root).as_posix()
                if not is_graph_file(relative) and path.suffix not in {'.html', '.htm'}:
                    continue
                data = path.read_bytes()
                graph_files.append((relative, data.decode('utf-8')))
                if path.suffix not in CODEGRAPH_SUPPORTED_SUFFIXES | {'.html', '.htm'}:
                    continue
                if requested and relative not in requested:
                    continue
                if arguments.path_globs and not any(fnmatch.fnmatchcase(relative, pattern)
                                                    for pattern in arguments.path_globs):
                    continue
                files.append((relative, data.decode('utf-8')))
                versions[relative] = digest(data)
        engine.scopes.assert_current(engine.context)
        project = engine.scopes.projects[engine.context.active_scope]
        index = RepositoryCodeGraph(project, engine.artifacts.root.parent)
        binding = {'scope_id': state.scope_id, 'run_id': state.run_id,
                   'source_manifest': state.source_manifest, 'patch_hash': state.patch_hash, 'view': 'run'}
        result = await asyncio.to_thread(analyze_source, files,
                                        {**arguments.config, 'analyzer': 'codegraph',
                                         'check': arguments.check}, index=index, graph_files=graph_files,
                                        binding=binding)
        stale = any(getattr(state, key) != binding[key] for key in ('source_manifest', 'patch_hash'))
        with local.mutex.workspace_read():
            for relative, content in graph_files:
                path = local.path(str(local.root / relative))
                current = path.read_bytes()
                stale = stale or digest(current) != digest(content.encode('utf-8'))
        if stale:
            result['errors'].append({'message': 'CodeGraph 查询期间源码或 Run 版本已变化，请重新分析'})
            result.update(status='error', fallback_required=True)
        result.update(source_manifest=binding['source_manifest'], content_versions=versions,
                      graph_content_versions={path: digest(content.encode('utf-8')) for path, content in graph_files})
        if callable(getattr(engine, 'put', None)):
            result['artifact_ref'] = engine.put(state, result, name='代码分析证据')
        return result

    bind('code.analyze', '使用 CodeGraph 分析授权源码的符号、依赖和调用结构；模板语义检查保留在受限适配器中，支持 JavaScript/TypeScript、React JSX/TSX、Vue3 SFC 和 HTML。'
         '每个配置仓库维护一个持久图数据库，按当前 Run 完整授权源码同步。默认 symbols；'
         'context、callers、callees、impact 使用 config.query 指定任务或符号，关系查询范围为完整授权源码；'
         '保留 syntax、elements、a11y_name、event_binding、regex 检查；paths 使用工作区绝对路径，省略时按 path_globs 选择。'
         '结果含实际位置、源码哈希和证据引用；status=error 时应结合页面截图、语义观察和 Read/Grep 回退检测，不能视为通过。',
         CodeAnalyze, code_analyze, phases=set(Phase), output_limit_tokens=6000,
         search_hint='AST DOM frontend React Vue JavaScript TypeScript HTML accessibility event handler')
