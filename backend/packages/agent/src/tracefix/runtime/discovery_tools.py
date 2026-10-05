"""当前授权工具目录和阶段化 Skill 的显式加载入口。"""
from __future__ import annotations

import re

from pydantic import Field

from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.runtime.tools import ToolRejected


class ToolSearchInput(Contract):
    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=5, ge=1, le=20)


class ToolSummary(Contract):
    name: str
    canonical_name: str
    aliases: list[str]
    description: str
    category: str
    search_hint: str
    parameters: dict
    side_effect: str
    parallel_safe: bool


class ToolSearchOutput(Contract):
    tools: list[ToolSummary]
    total_matches: int
    truncated: bool


class SkillInput(Contract):
    skill: str = Field(min_length=1, max_length=200)


class SkillOutput(Contract):
    name: str
    version: str
    content_hash: str
    description: str
    source: str
    content: str
    references: list[dict]
    snapshot_ref: str | None = None


def search_tools(registry, phase, arguments: ToolSearchInput) -> ToolSearchOutput:
    query = arguments.query.strip()
    if not query:
        raise ToolRejected('工具搜索关键词不能为空')
    visible = registry.visible(phase)
    if query.startswith('select:'):
        names = {name.strip() for name in query[7:].split(',') if name.strip()}
        matches = [(0, spec) for spec in visible if names & {
            spec.name, spec.wire_name, spec.name.replace('.', '_'), *spec.aliases}]
    else:
        keywords = re.findall(r'\S+', query.casefold())
        matches = []
        for spec in visible:
            names = ' '.join((spec.name, spec.wire_name, *spec.aliases)).casefold()
            text = ' '.join((names, spec.description, spec.category, spec.search_hint)).casefold()
            if all(keyword in text for keyword in keywords):
                matches.append((-sum(keyword in names for keyword in keywords), spec))
    matches.sort(key=lambda item: (item[0], item[1].wire_name))
    tools = [ToolSummary(name=spec.wire_name, canonical_name=spec.name,
        aliases=list(spec.aliases), description=spec.description, category=spec.category,
        search_hint=spec.search_hint, parameters=spec.parameters,
        side_effect=spec.side_effect, parallel_safe=spec.parallel_safe)
        for score, spec in matches[:arguments.max_results]]
    return ToolSearchOutput(tools=tools, total_matches=len(matches),
                            truncated=len(matches) > len(tools))


def register_discovery_tools(engine, state, context, registry, bind):
    if context.get('worker_depth') == 1:
        return

    def tool_search(arguments, call_id):
        return search_tools(registry, state.phase, arguments)

    bind('ToolSearch', '查询当前阶段已授权且可执行的工具。支持关键词或 select:工具名，'
         '返回模型接口名称和输入 schema；查询不会扩大权限或延迟加载工具。',
         ToolSearchInput, tool_search, phases=set(Phase), side_effect='read',
         parallel_safe=True, output_model=ToolSearchOutput,
         search_hint='discover available capabilities schemas 工具目录 能力发现')

    if not callable(getattr(engine, 'load_skill', None)):
        return

    def skill(arguments, call_id):
        prior = [dict(entry) for entry in state.skills_loaded]
        try:
            return engine.load_skill(state, arguments.skill)
        except (OSError, PermissionError, ValueError) as error:
            if state.skills_loaded != prior:
                raise
            raise ToolRejected(str(error)) from error

    bind('Skill', '加载当前阶段允许的 Skill 正文及声明引用并冻结为 Run 快照；'
         'Skill 只是指导，不会增加工具、文件、浏览器权限或改变验证门禁。',
         SkillInput, skill, phases=set(Phase), side_effect='write', parallel_safe=False,
         output_model=SkillOutput, output_limit_tokens=10000,
         idempotency_key=lambda arguments: digest([str(state.phase), arguments.model_dump(mode='json')]),
         search_hint='load staged workflow instructions 技能 工作流指导')
