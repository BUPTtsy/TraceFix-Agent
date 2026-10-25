import asyncio
import threading

import pytest
from pydantic import ValidationError

from tracefix.workers.contracts import WorkerStatus, WorkerTask
from tracefix.workers.runtime import HierarchyTrace, SubAgentRejected, SubAgentRuntime


def task(task_id, role='code-explorer', phase='DIAGNOSE', tools=None, source_revision=4):
    return WorkerTask(task_id=task_id, run_id='run-parent', phase=phase, role=role,
                      goal='trace the authorized state transition and report evidence',
                      prompt='Read the authorized source and return a structured evidence report.',
                      tools=tools or ['code.read'], allowed_files=['src/app.ts'],
                      source_revision=source_revision)


@pytest.mark.asyncio
async def test_read_only_roles_dispatch_in_parallel_and_keep_parent_trace():
    trace = HierarchyTrace()
    runtime = SubAgentRuntime(max_concurrency=3, trace=trace)
    active = 0
    peak = 0
    lock = threading.Lock()

    async def runner(worker_task, _context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(.02)
        with lock:
            active -= 1
        return {'status': WorkerStatus.SUCCEEDED, 'summary': worker_task.task_id,
                'files_touched': ['src/app.ts']}

    results = await runtime.group([task('a'), task('b'), task('c')], parent_run_id='run-parent',
                                  parent_step_id='step-3', generation=4,
                                  runners={'a': runner, 'b': runner, 'c': runner})
    runtime.close()
    assert peak >= 2
    assert [item.summary for item in results] == ['a', 'b', 'c']
    children = trace.children('run-parent')
    assert {item['task_id'] for item in children if item['type'] == 'subtask.completed'} == {'a', 'b', 'c'}


def test_runtime_rejects_wrong_phase_tool_and_recursive_depth():
    runtime = SubAgentRuntime()
    with pytest.raises(SubAgentRejected):
        runtime.validate(task('bad', role='gui-scout', phase='DIAGNOSE', tools=['browser']),
                         parent_run_id='run-parent', parent_step_id='step', generation=4)
    with pytest.raises(SubAgentRejected):
        runtime.validate(task('bad-tool', tools=['code.read', 'browser']), parent_run_id='run-parent',
                         parent_step_id='step', generation=4)
    with pytest.raises(SubAgentRejected):
        runtime.validate(task('recursive'), parent_run_id='run-parent', parent_step_id='step',
                         generation=4, depth=2)
    runtime.close()


def test_supervisor_cannot_grant_workers_removed_write_access():
    runtime = SubAgentRuntime()
    with pytest.raises(ValidationError, match='file.write'):
        task('patch', role='patch-worker', phase='PATCH', tools=['file.write', 'code.read'])
    with pytest.raises(SubAgentRejected):
        runtime.validate(task('patch', role='patch-worker', phase='PATCH'), parent_run_id='run-parent',
                         parent_step_id='step', generation=4)
    for field, value in [('writable_files', ['src/app.ts']), ('write_enabled', True)]:
        with pytest.raises(ValidationError, match='Extra inputs'):
            WorkerTask.model_validate({**task('unsafe').model_dump(), field: value})
    runtime.close()


@pytest.mark.asyncio
async def test_parent_cancel_propagates_to_pending_workers():
    runtime = SubAgentRuntime(max_concurrency=1)
    started = threading.Event()

    async def runner(_task, context):
        started.set()
        while not context.cancelled:
            await asyncio.sleep(.005)
        context.check_cancelled()

    pending = asyncio.create_task(runtime.dispatch(task('cancel-me'), parent_run_id='run-parent',
                                                    parent_step_id='step', generation=4, runner=runner))
    assert await asyncio.to_thread(started.wait, 5)
    runtime.cancel('run-parent')
    result = await pending
    runtime.close()
    assert result.status in {WorkerStatus.CANCELLED, WorkerStatus.PARTIAL}


def test_runtime_rejects_stale_generation():
    runtime = SubAgentRuntime()
    with pytest.raises(SubAgentRejected, match='过期'):
        runtime.validate(task('stale', source_revision=3), parent_run_id='run-parent',
                         parent_step_id='step', generation=4)
    runtime.close()
