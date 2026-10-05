from types import SimpleNamespace

import pytest

from tracefix.runtime.contracts import Outcome, Phase, RunState, digest
from tracefix.runtime.effects import EffectBoundaryError, make_operation_executor
from tracefix.runtime.task_tools import (TaskCreateInput, TodoWriteInput,
                                        register_task_tools)
from tracefix.runtime.tools import (ToolPipeline, ToolProtocolError, ToolRegistry,
                                   ToolSpec)
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore, UnknownOperation


@pytest.fixture
def task_runtime(tmp_path):
    state = RunState(scope_id='scope', run_id='run_task_tools', goal='分析问题',
                     url='http://localhost', phase=Phase.DIAGNOSE, mode='test',
                     evidence_refs=['authorized.json'], test_spec_hash='frozen',
                     reproduction_plan_frozen=True, outcome=Outcome.INCONCLUSIVE)
    engine = SimpleNamespace(artifacts=Artifacts(tmp_path / 'artifacts'), store=MemoryStore())
    engine.store.save(state)
    return engine, state


def task_pipeline(engine, state, context=None):
    registry, handlers = ToolRegistry(), {}

    def bind(name, description, input_model, handler, *, phases,
             side_effect='read', parallel_safe=True, output_model=None,
             idempotency_key=None):
        registry.register(ToolSpec(name, description, input_model,
            phases=frozenset(phases), side_effect=side_effect,
            parallel_safe=parallel_safe, output_model=output_model,
            idempotency_key=idempotency_key or (digest if side_effect == 'write' else None)))
        handlers[name] = handler

    register_task_tools(engine, state, context or {}, bind)
    return ToolPipeline(registry, handlers, state.phase,
        operation=make_operation_executor(engine.store, state),
        resource_resolver=lambda name, args: [f'run-tasks:{state.scope_id}:{state.run_id}'])


async def create(pipeline, subject, call_id):
    result = await pipeline.execute('TaskCreate', {'subject': subject,
        'description': subject + '的验收要求'}, call_id)
    assert result.executed and not result.is_error
    return result.result['task']['id']


def test_registration_permissions_and_output_models(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    assert {spec.name for spec in pipeline.registry.specs} == {
        'TaskCreate', 'TaskGet', 'TaskList', 'TaskUpdate', 'TodoWrite'}
    for name in ('TaskGet', 'TaskList'):
        spec = pipeline.registry.get(name)
        assert spec.side_effect == 'read' and spec.parallel_safe
    for name in ('TaskCreate', 'TaskUpdate', 'TodoWrite'):
        spec = pipeline.registry.get(name)
        assert spec.side_effect == 'write' and not spec.parallel_safe
        assert spec.idempotency_key and spec.output_model
    assert not task_pipeline(engine, state, {'worker_depth': 1,
        'worker_write_enabled': True}).registry.specs
    engine.subagent_depth = 1
    assert not task_pipeline(engine, state).registry.specs


@pytest.mark.asyncio
async def test_create_restore_and_preserve_evidence_gates(task_runtime):
    engine, state = task_runtime
    before = state.model_dump(mode='json')
    pipeline = task_pipeline(engine, state)
    task_id = await create(pipeline, '检查接口', 'create-interface')
    assert task_id == 'task_' + digest([state.scope_id, state.run_id, 'create-interface'])[:24]
    assert state.task_board_ref and state.task_board_ref not in state.evidence_refs
    restored = engine.store.load(state.run_id, state.scope_id)
    recovered = task_pipeline(engine, restored)
    found = await recovered.execute('TaskGet', {'taskId': task_id}, 'get-restored')
    assert found.result['task']['subject'] == '检查接口'
    after = state.model_dump(mode='json')
    assert {key: value for key, value in after.items() if key != 'task_board_ref'} == {
        key: value for key, value in before.items() if key != 'task_board_ref'}
    operations = list(engine.store.operations.values())
    assert len(operations) == 1 and operations[0]['status'] == 'DONE'
    assert operations[0]['resources'] == ['run-tasks:scope:run_task_tools']


@pytest.mark.asyncio
async def test_create_idempotency_after_restore_and_conflicting_call_identity(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    arguments = {'subject': '定位原因', 'description': '保留证据'}
    first = await pipeline.execute('TaskCreate', arguments, 'create-once')
    task_id = first.result['task']['id']
    restored = engine.store.load(state.run_id, state.scope_id)
    recovered = task_pipeline(engine, restored)
    repeated = await recovered.execute('TaskCreate', arguments, 'create-retry')
    assert repeated.result['task']['id'] == task_id
    assert len((await recovered.execute('TaskList', {}, 'list-once')).result['tasks']) == 1
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())
    conflict = await task_pipeline(engine, restored).execute('TaskCreate', {
        'subject': '不同任务', 'description': '其他要求'}, 'create-once')
    assert conflict.is_error and not conflict.executed
    assert len((await recovered.execute('TaskList', {}, 'list-after-conflict')).result['tasks']) == 1


@pytest.mark.asyncio
async def test_completed_task_can_be_created_again_with_new_call(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    first = await create(pipeline, '复核', 'first')
    completed = await pipeline.execute('TaskUpdate', {'taskId': first,
        'status': 'completed'}, 'finish')
    assert not completed.is_error
    second = await create(pipeline, '复核', 'second')
    assert first != second
    tasks = (await pipeline.execute('TaskList', {}, 'list')).result['tasks']
    assert [task['status'] for task in tasks] == ['completed', 'pending']


@pytest.mark.asyncio
async def test_task_count_limit_and_duplicate_pending_deduplication(task_runtime, monkeypatch):
    from tracefix.runtime import task_tools

    monkeypatch.setattr(task_tools, 'MAX_TASKS', 2)
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    first = await create(pipeline, '第一项', 'first')
    assert await create(pipeline, '第一项', 'retry') == first
    await create(pipeline, '第二项', 'second')
    reference = state.task_board_ref
    rejected = await pipeline.execute('TaskCreate', {'subject': '第三项',
        'description': '超限'}, 'third')
    assert rejected.is_error and '上限' in rejected.error['message']
    assert state.task_board_ref == reference


@pytest.mark.asyncio
async def test_metadata_merge_cannot_exceed_stored_limit(task_runtime, monkeypatch):
    from tracefix.runtime import task_tools

    monkeypatch.setattr(task_tools, 'MAX_METADATA_BYTES', 70)
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    result = await pipeline.execute('TaskCreate', {'subject': '任务', 'description': '描述',
        'metadata': {'first': 'value' * 7}}, 'create')
    task_id = result.result['task']['id']
    reference = state.task_board_ref
    updated = await pipeline.execute('TaskUpdate', {'taskId': task_id,
        'metadata': {'second': 'value' * 7}}, 'update')
    assert updated.is_error and '上限' in updated.error['message']
    assert state.task_board_ref == reference


@pytest.mark.asyncio
async def test_dependency_readiness_retry_and_bidirectional_edges(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    prerequisite = await create(pipeline, '收集证据', 'create-first')
    dependent = await create(pipeline, '提交结论', 'create-second')
    linked = await pipeline.execute('TaskUpdate', {'taskId': dependent,
        'addBlockedBy': [prerequisite]}, 'link')
    assert not linked.is_error
    blocked = await pipeline.execute('TaskUpdate', {'taskId': dependent,
        'status': 'in_progress'}, 'blocked')
    assert blocked.is_error and not blocked.executed
    first = await pipeline.execute('TaskGet', {'taskId': prerequisite}, 'read-first')
    second = await pipeline.execute('TaskGet', {'taskId': dependent}, 'read-second')
    assert first.result['task']['blocks'] == [dependent]
    assert second.result['task']['blockedBy'] == [prerequisite]
    assert second.result['task']['status'] == 'pending'
    completed = await pipeline.execute('TaskUpdate', {'taskId': prerequisite,
        'status': 'completed'}, 'finish-first')
    assert not completed.is_error
    ready = await pipeline.execute('TaskUpdate', {'taskId': dependent,
        'status': 'in_progress'}, 'ready')
    assert not ready.is_error and ready.result['statusChange'] == {
        'from': 'pending', 'to': 'in_progress'}


@pytest.mark.asyncio
@pytest.mark.parametrize('change, message', [
    ({'addBlockedBy': ['missing']}, '不存在'),
    ({'addBlockedBy': ['self']}, '自身'),
    ({'addBlocks': ['self']}, '自身'),
])
async def test_invalid_dependencies_reject_without_persisting(task_runtime, change, message):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    task_id = await create(pipeline, '任务', 'create')
    values = {key: [task_id if item == 'self' else item for item in items]
              for key, items in change.items()}
    reference = state.task_board_ref
    result = await pipeline.execute('TaskUpdate', {'taskId': task_id, **values}, 'invalid')
    assert result.is_error and message in result.error['message']
    assert state.task_board_ref == reference
    task = (await pipeline.execute('TaskGet', {'taskId': task_id}, 'get')).result['task']
    assert task['blocks'] == [] and task['blockedBy'] == []


@pytest.mark.asyncio
async def test_dependency_cycle_and_running_task_regression_are_atomic(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    first = await create(pipeline, '前置', 'create-first')
    second = await create(pipeline, '后置', 'create-second')
    assert not (await pipeline.execute('TaskUpdate', {'taskId': first,
        'addBlocks': [second]}, 'link')).is_error
    reference = state.task_board_ref
    cycle = await pipeline.execute('TaskUpdate', {'taskId': second,
        'addBlocks': [first]}, 'cycle')
    assert cycle.is_error and '环' in cycle.error['message']
    assert state.task_board_ref == reference
    await pipeline.execute('TaskUpdate', {'taskId': first, 'status': 'completed'}, 'complete-first')
    await pipeline.execute('TaskUpdate', {'taskId': second, 'status': 'in_progress'}, 'start-second')
    reference = state.task_board_ref
    regression = await pipeline.execute('TaskUpdate', {'taskId': first,
        'status': 'pending'}, 'regress')
    assert regression.is_error and state.task_board_ref == reference


@pytest.mark.asyncio
async def test_task_update_metadata_and_missing_task(task_runtime):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    first = await pipeline.execute('TaskCreate', {'subject': '分析', 'description': '描述',
        'metadata': {'retained': 1, 'remove': 2}}, 'create')
    task_id = first.result['task']['id']
    update = await pipeline.execute('TaskUpdate', {'taskId': task_id, 'owner': 'reviewer',
        'subject': '复核', 'activeForm': '正在复核', 'metadata': {'remove': None, 'added': 3}}, 'update')
    assert not update.is_error
    task = (await pipeline.execute('TaskGet', {'taskId': task_id}, 'get')).result['task']
    assert task['metadata'] == {'retained': 1, 'added': 3}
    assert task['owner'] == 'reviewer' and task['activeForm'] == '正在复核'
    assert (await pipeline.execute('TaskGet', {'taskId': 'missing'}, 'get-missing')).result['task'] is None
    missing = await pipeline.execute('TaskUpdate', {'taskId': 'missing',
        'status': 'completed'}, 'update-missing')
    assert not missing.result['success']
    with pytest.raises(ToolProtocolError):
        await pipeline.execute('TaskUpdate', {'taskId': task_id, 'status': 'deleted'}, 'delete')


@pytest.mark.asyncio
async def test_todo_restore_repeated_content_and_clear(task_runtime):
    engine, state = task_runtime
    before = state.model_dump(mode='json')
    pipeline = task_pipeline(engine, state)
    todo = {'content': '复核结果', 'status': 'in_progress', 'activeForm': '正在复核结果'}
    result = await pipeline.execute('TodoWrite', {'todos': [todo]}, 'first')
    assert result.result == {'oldTodos': [], 'newTodos': [todo]}
    restored = engine.store.load(state.run_id, state.scope_id)
    recovered = task_pipeline(engine, restored)
    clear = await recovered.execute('TodoWrite', {'todos': []}, 'clear')
    assert clear.result == {'oldTodos': [todo], 'newTodos': []}
    written = await recovered.execute('TodoWrite', {'todos': [todo]}, 'again')
    assert written.result == {'oldTodos': [], 'newTodos': [todo]}
    cleared_again = await recovered.execute('TodoWrite', {'todos': []}, 'clear-again')
    assert cleared_again.result == {'oldTodos': [todo], 'newTodos': []}
    assert engine.artifacts.json(state.scope_id, state.run_id, restored.todo_list_ref)['items'] == []
    after = state.model_dump(mode='json')
    assert {key: value for key, value in after.items() if key != 'todo_list_ref'} == {
        key: value for key, value in before.items() if key != 'todo_list_ref'}
    assert restored.todo_list_ref not in restored.evidence_refs
    with pytest.raises(ToolProtocolError, match='校验'):
        await recovered.execute('TodoWrite', {'todos': [todo, todo]}, 'invalid')


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['task_board_ref', 'todo_list_ref'])
async def test_foreign_artifact_body_is_rejected(task_runtime, field):
    engine, state = task_runtime
    payload = {'version': 1, 'scope_id': state.scope_id, 'run_id': 'other_run',
               'tasks': {}, 'create_calls': {}, 'items': []}
    reference = engine.artifacts.put(state.scope_id, state.run_id, payload)
    setattr(state, field, reference)
    engine.store.save(state)
    pipeline = task_pipeline(engine, state)
    name, arguments = ('TaskList', {}) if field == 'task_board_ref' else ('TodoWrite', {'todos': []})
    result = await pipeline.execute(name, arguments, 'foreign')
    assert result.is_error and '当前 Run' in result.error['message']
    assert getattr(state, field) == reference


@pytest.mark.asyncio
async def test_worker_cannot_access_supervisor_board(task_runtime):
    engine, state = task_runtime
    task_id = await create(task_pipeline(engine, state), 'Supervisor 任务', 'create')
    worker = task_pipeline(engine, state, {'worker_depth': 1,
        'worker_allowed_tools': ['TaskGet', 'TaskList', 'TaskUpdate', 'TodoWrite'],
        'worker_write_enabled': True})
    with pytest.raises(ToolProtocolError, match='未知'):
        await worker.execute('TaskGet', {'taskId': task_id}, 'worker-read')


@pytest.mark.asyncio
async def test_direct_write_requires_receipt_and_save_failure_keeps_fence(task_runtime, monkeypatch):
    engine, state = task_runtime
    pipeline = task_pipeline(engine, state)
    arguments = TaskCreateInput(subject='检查', description='检查要求')
    with pytest.raises(EffectBoundaryError):
        await pipeline.handlers['TaskCreate'](arguments, 'direct')
    assert state.task_board_ref is None

    def broken_save(updated):
        raise OSError('持久化失败')

    monkeypatch.setattr(engine.store, 'save', broken_save)
    with pytest.raises(Exception) as failure:
        await pipeline.execute('TaskCreate', arguments.model_dump(mode='json'), 'failing')
    assert getattr(failure.value, 'status', None) == 'UNKNOWN_OPERATION'
    assert state.task_board_ref is None
    assert next(iter(engine.store.operations.values()))['status'] == 'UNKNOWN'
    with pytest.raises(UnknownOperation):
        await pipeline.execute('TaskCreate', {'subject': '后续', 'description': '后续任务'}, 'blocked')


def test_task_and_todo_inputs_are_strict():
    with pytest.raises(ValueError):
        TaskCreateInput.model_validate({'subject': '任务', 'description': '描述',
                                       'evidence_refs': ['fake.json']})
    with pytest.raises(ValueError):
        TodoWriteInput.model_validate({'todos': [{'content': '任务',
            'status': 'completed', 'activeForm': '完成任务', 'phase': 'FINALIZE'}]})
    with pytest.raises(ValueError, match='metadata'):
        TaskCreateInput(subject='任务', description='描述', metadata={'value': float('inf')})
    with pytest.raises(ValueError, match='上限'):
        TaskCreateInput(subject='任务', description='描述', metadata={'text': '大' * 6000})
