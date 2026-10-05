from types import SimpleNamespace

import pytest

from tracefix.knowledge.context import SkillCatalog
from tracefix.runtime.contracts import Contract, Phase, RunState
from tracefix.runtime.discovery_tools import (SkillOutput, ToolSearchInput,
    register_discovery_tools, search_tools)
from tracefix.runtime.effects import make_operation_executor
from tracefix.runtime.skills import SkillStore
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolSpec, build_tool
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore


class Empty(Contract):
    pass


def test_search_is_bounded_and_filters_phase_disabled_and_aliases():
    registry = ToolRegistry([
        ToolSpec('file.read', '读取源码文件', Empty, aliases=('Read',), search_hint='source code'),
        ToolSpec('file.search', '搜索源码文件', Empty, search_hint='source code'),
        ToolSpec('patch.only', 'source code', Empty, phases=frozenset({Phase.PATCH})),
        ToolSpec('hidden', 'source code', Empty, enabled=False),
    ])
    result = search_tools(registry, Phase.DIAGNOSE,
                          ToolSearchInput(query='source code', max_results=1))
    assert result.total_matches == 2 and result.truncated
    assert result.tools[0].name == 'FileRead'
    assert registry.get_wire(result.tools[0].name, Phase.DIAGNOSE).name == 'file.read'
    exact = search_tools(registry, Phase.DIAGNOSE, ToolSearchInput(query='select:Read'))
    assert exact.tools[0].canonical_name == 'file.read'
    assert search_tools(registry, Phase.DIAGNOSE,
                        ToolSearchInput(query='select:patch.only,hidden')).tools == []


@pytest.mark.asyncio
async def test_skill_load_uses_frozen_content_without_changing_run_authority(tmp_path):
    root = tmp_path / 'skills'
    document = root / 'test-plan' / 'SKILL.md'
    document.parent.mkdir(parents=True)
    document.write_text('---\nname: test-plan\ndescription: 测试计划\nphases: [DIAGNOSE]\n---\n'
                        '先检查证据，再定位源码。\n', encoding='utf-8')
    artifacts = Artifacts(tmp_path / 'artifacts')
    store = MemoryStore()
    state = RunState(scope_id='scope', url='https://example.com', goal='验证', phase=Phase.DIAGNOSE)
    store.save(state)
    catalog, snapshots = SkillCatalog(root), SkillStore(artifacts)

    def load_skill(current, name):
        result = snapshots.load(current, catalog, name, str(current.phase))
        store.save(current)
        return result

    engine = SimpleNamespace(load_skill=load_skill)
    registry, handlers = ToolRegistry(), {}

    def bind(name, description, model, handler, **options):
        definition = build_tool(name, description, model, handler, **options)
        registry.register(definition.spec)
        handlers[name] = definition.handler

    register_discovery_tools(engine, state, {}, registry, bind)
    pipeline = ToolPipeline(registry, handlers, state.phase,
                            operation=make_operation_executor(store, state))
    authority = (state.phase, state.evidence_refs[:], state.outcome, state.test_spec_ref)
    first = await pipeline.execute('Skill', {'skill': 'test-plan'}, 'load')
    assert not first.is_error
    loaded = SkillOutput.model_validate(first.result)
    assert '先检查证据' in loaded.content
    assert loaded.snapshot_ref == state.skills_loaded[0]['snapshot_ref']
    document.write_text('---\nname: test-plan\ndescription: 新内容\n---\n修改后的指导', encoding='utf-8')
    state = store.load(state.run_id, state.scope_id)
    second = snapshots.load(state, catalog, 'test-plan', str(state.phase))
    assert second['content'] == loaded.content
    assert (state.phase, state.evidence_refs, state.outcome, state.test_spec_ref) == authority
    state.phase = Phase.VERIFY
    rejected = await pipeline.execute('Skill', {'skill': 'missing'}, 'bad-load')
    assert rejected.is_error


def test_discovery_is_not_offered_to_ungranted_worker():
    registry = ToolRegistry()
    register_discovery_tools(SimpleNamespace(), SimpleNamespace(),
                             {'worker_depth': 1}, registry, lambda *args, **kwargs: pytest.fail('越权注册'))
    assert registry.specs == ()


def test_derived_run_does_not_inherit_planning_artifacts():
    from tracefix.runtime.continuation import derive_run

    parent = RunState(scope_id='scope', url='https://example.com', goal='目标',
                      task_board_ref='0001_任务板.json', todo_list_ref='0002_待办.json')
    child = derive_run(parent)
    assert child.task_board_ref is None and child.todo_list_ref is None
    assert parent.task_board_ref == '0001_任务板.json'


@pytest.mark.asyncio
async def test_unified_runtime_exposes_executable_tools_and_keeps_worker_scope(tmp_path, monkeypatch):
    from tracefix.runtime.smoke import make_engine
    from tracefix.runtime.tool_handlers import build_runtime_tools

    monkeypatch.delenv('TRACEFIX_WEB_SEARCH_API_KEY', raising=False)
    engine, state = make_engine(tmp_path / 'runtime')
    state.phase = Phase.DIAGNOSE
    engine.store.save(state)
    runtime = build_runtime_tools(engine, state, None)
    expected = {'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate', 'TodoWrite',
                'WebFetch', 'ToolSearch', 'Skill'}
    assert all(runtime.registry.contains(name, state.phase) for name in expected)
    assert not runtime.registry.contains('WebSearch')
    assert all(spec.name in runtime.handlers for spec in runtime.registry.visible(state.phase))
    pipeline = runtime.pipeline()
    created = await pipeline.execute('TaskCreate', {'subject': '集成验证',
        'description': '确认统一 runtime 已绑定处理器'}, 'create')
    assert not created.is_error
    assert (await pipeline.execute('TaskList', {}, 'list')).result['tasks'][0]['id'] == created.result['task']['id']
    found = await pipeline.execute('ToolSearch', {'query': 'select:TaskCreate,WebFetch'}, 'discover')
    assert {tool['name'] for tool in found.result['tools']} == {'TaskCreate', 'WebFetch'}
    assert runtime.resource_resolver('TaskCreate', {}) == [f'run-tasks:{state.scope_id}:{state.run_id}']
    worker = build_runtime_tools(engine, state, None, {'worker_depth': 1,
        'worker_allowed_files': [], 'worker_allowed_tools': ['Read']})
    assert not any(worker.registry.contains(name) for name in expected)


@pytest.mark.asyncio
async def test_skill_failure_after_loading_is_unknown_instead_of_unexecuted(tmp_path):
    from tracefix.runtime.tools import ToolOperationUnknown

    store = MemoryStore()
    state = RunState(scope_id='scope', url='https://example.com', goal='验证', phase=Phase.DIAGNOSE)
    store.save(state)

    def load_skill(current, name):
        current.skills_loaded.append({'name': name})
        raise ValueError('保存快照后失败')

    registry, handlers = ToolRegistry(), {}

    def bind(name, description, model, handler, **options):
        definition = build_tool(name, description, model, handler, **options)
        registry.register(definition.spec)
        handlers[name] = definition.handler

    register_discovery_tools(SimpleNamespace(load_skill=load_skill), state, {}, registry, bind)
    pipeline = ToolPipeline(registry, handlers, state.phase,
                            operation=make_operation_executor(store, state))
    with pytest.raises(ToolOperationUnknown):
        await pipeline.execute('Skill', {'skill': 'test-plan'}, 'load')
    assert next(iter(store.operations.values()))['status'] == 'UNKNOWN'
