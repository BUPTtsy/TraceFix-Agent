from pydantic import Field

from tracefix.connectors.context7_mcp import (
    CONTEXT7_INSTRUCTIONS, Context7MCPConnector, Context7MCPError, context7_enabled,
)
from tracefix.runtime.contracts import Contract, Phase
from tracefix.tools.core import ToolRejected


class Context7ResolveInput(Contract):
    libraryName: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2000)


class Context7QueryInput(Contract):
    libraryId: str = Field(pattern=r'^/[^/\s]+/[^/\s]+(?:/[^/\s]+)?$', max_length=300)
    query: str = Field(min_length=1, max_length=2000)
    language: str | None = Field(default=None, min_length=1, max_length=80)


def register_context7_tools(engine, state, context, bind):
    if not context7_enabled():
        return
    connector = Context7MCPConnector()

    async def invoke(name, arguments):
        try:
            return await connector.call(name, arguments.model_dump(exclude_none=True))
        except Context7MCPError as error:
            raise ToolRejected(str(error)) from None

    async def resolve(arguments, call_id):
        return await invoke('resolve-library-id', arguments)

    async def query(arguments, call_id):
        return await invoke('query-docs', arguments)

    bind('context7.resolve-library-id', CONTEXT7_INSTRUCTIONS, Context7ResolveInput, resolve,
         phases=set(Phase), side_effect='read', parallel_safe=True, timeout_s=35,
         aliases=('mcp__context7__resolve-library-id',),
         search_hint='Context7 第三方库、框架、API 文档，按名称和版本解析 libraryId。')
    bind('context7.query-docs', '查询已解析 libraryId 的版本文档与示例。' + CONTEXT7_INSTRUCTIONS,
         Context7QueryInput, query, phases=set(Phase), side_effect='read', parallel_safe=True,
         timeout_s=35, aliases=('mcp__context7__query-docs',),
         search_hint='Context7 读取官方库文档、API、代码示例及安装配置步骤。')
