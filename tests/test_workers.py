import threading
import time

import pytest

from tracefix.workers import (
    WorkerDelegationError,
    WorkerScheduler,
    WorkerStatus,
    WorkerTask,
    WorkspaceMutex,
)


def make_task(task_id="task-1", **overrides):
    values = {
        "task_id": task_id,
        "run_id": "run-1",
        "phase": "DIAGNOSE",
        "role": "dynamic investigator",
        "goal": "trace the failure and return concrete evidence",
        "prompt": "Read the authorized file, record observations, and report unresolved questions.",
        "allowed_files": ["src/app.ts"],
        "tools": ["code.read"],
    }
    values.update(overrides)
    return WorkerTask(**values)


def test_delegate_contract_keeps_dynamic_role_and_rejects_spawn():
    task = WorkerTask.from_delegate_args(
        {
            "task_id": "worker-1",
            "role": "code explorer 1",
            "objective": "trace the state transition and return concrete evidence",
            "prompt": "Read the authorized source and follow the state transition step by step.",
            "phase": "DIAGNOSE",
            "allowed_tools": ["code.read"],
            "expected_output": "A concise evidence-backed conclusion.",
            "completion_criteria": ["cite the relevant file"],
            "constraints": ["do not expand the file scope"],
        },
        run_id="run-1",
    )
    assert task.role == "code explorer 1"
    assert "完成标准" in task.prompt

    scheduler = WorkerScheduler()
    handle = scheduler.submit(task, lambda _task, context: context.spawn_worker())
    result = handle.result(timeout=5)
    scheduler.close()
    assert result.status is WorkerStatus.PARTIAL
    assert any("spawn" in value for value in result.unresolved)


def test_delegate_contract_runtime_fields_and_tools_are_strict():
    task = WorkerTask.from_delegate_args(
        {
            "task_id": "worker-2",
            "run_id": "other-run",
            "parent_task_id": "other-task",
            "source_revision": 999,
            "role": "code explorer",
            "objective": "trace the state transition and return concrete evidence",
            "prompt": "Read the authorized source and follow the state transition step by step.",
            "phase": "DIAGNOSE",
            "allowed_tools": ["code.read"],
            "expected_output": "A concise evidence-backed conclusion.",
            "completion_criteria": ["cite the relevant file"],
            "constraints": ["do not expand the file scope"],
        },
        run_id="run-1",
        parent_task_id=None,
        source_revision=3,
    )
    assert task.run_id == "run-1"
    assert task.parent_task_id is None
    assert task.source_revision == 3
    with pytest.raises(ValueError, match="not supported"):
        make_task(tools=["worker.spawn"])


def test_scheduler_caps_running_threads_at_four():
    lock = threading.Lock()
    active = 0
    peak = 0

    def runner(_task, _context):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"summary": "done"}

    scheduler = WorkerScheduler()
    handles = [scheduler.submit(make_task(f"task-{index}"), runner) for index in range(9)]
    results = scheduler.wait_blocking(handles, timeout=10)
    scheduler.close()
    assert peak == 4
    assert all(result.status is WorkerStatus.SUCCEEDED for result in results)


def test_scheduler_releases_dependency_without_consuming_worker_slot():
    order = []

    def runner(task, _context):
        order.append(task.task_id)
        return {"summary": task.task_id}

    scheduler = WorkerScheduler(max_concurrency=1)
    dependent = make_task("dependent", depends_on=["base"])
    dependent_handle = scheduler.submit(dependent, runner)
    base_handle = scheduler.submit(make_task("base"), runner)
    result = dependent_handle.result(timeout=5)
    assert result.status is WorkerStatus.SUCCEEDED
    assert base_handle.result(timeout=5).status is WorkerStatus.SUCCEEDED
    scheduler.close()
    assert order == ["base", "dependent"]


def test_scheduler_retries_three_times_then_returns_partial():
    attempts = 0

    def runner(_task, _context):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary failure")

    events = []
    scheduler = WorkerScheduler(event_sink=events.append)
    result = scheduler.submit(make_task(), runner).result(timeout=5)
    scheduler.close()
    assert attempts == 4
    assert result.status is WorkerStatus.PARTIAL
    assert result.attempt == 4
    assert any(event.event_type.value == "worker.partial" for event in events)


def test_workspace_mutex_serializes_writes_for_one_path():
    mutex = WorkspaceMutex()
    active = 0
    peak = 0
    guard = threading.Lock()

    def write_once():
        nonlocal active, peak
        with mutex.write_lock("src/app.ts"):
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with guard:
                active -= 1

    threads = [threading.Thread(target=write_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1
