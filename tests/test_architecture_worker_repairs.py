import asyncio
import threading
from concurrent.futures import Future
from unittest.mock import Mock

import pytest

from tracefix.workers import WorkerScheduler, WorkerStatus, WorkerTask


def make_task(task_id, **overrides):
    values = {
        "task_id": task_id,
        "run_id": "architecture-r03",
        "phase": "DIAGNOSE",
        "role": "dependency investigator",
        "goal": "Verify dependency scheduling terminates safely.",
        "prompt": "Inspect the task dependency outcome and return a structured result.",
        "retry_limit": 0,
    }
    values.update(overrides)
    return WorkerTask(**values)


def test_missing_dependency_expires_and_releases_waiting_index():
    executed = []
    scheduler = WorkerScheduler(wait_timeout_seconds=0.05)
    try:
        handle = scheduler.submit(make_task("dependent", depends_on=["missing"]),
                                  lambda task, _context: executed.append(task.task_id) or "done")
        result = handle.result(timeout=2)
        assert result.status is WorkerStatus.EXPIRED
        assert "missing dependencies: missing" in result.error
        assert handle.status is WorkerStatus.EXPIRED
        assert scheduler.get("dependent") == result
        assert scheduler._waiting_for == {}
        assert executed == []
        assert [event.event_type.value for event in scheduler.events()] == [
            "worker.created", "worker.queued", "worker.expired",
        ]
        assert scheduler.submit(make_task("missing"), lambda *_args: "done").result(timeout=2).status is WorkerStatus.SUCCEEDED
        assert handle.status is WorkerStatus.EXPIRED
    finally:
        scheduler.close(cancel_pending=True)


def test_self_dependency_is_rejected_without_creating_a_record():
    scheduler = WorkerScheduler(runner=lambda *_args: "done")
    try:
        with pytest.raises(ValueError, match="cannot depend on itself"):
            scheduler.submit(make_task("self", depends_on=["self"]))
        assert scheduler.snapshots() == []
        assert scheduler.events() == ()
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.parametrize("length", [2, 4])
def test_forward_dependency_cycle_is_rejected_before_registration(length):
    scheduler = WorkerScheduler(runner=lambda *_args: "done")
    try:
        for index in range(length - 1):
            scheduler.submit(make_task(f"task-{index}", depends_on=[f"task-{index + 1}"]))
        with pytest.raises(ValueError, match="dependency cycle"):
            scheduler.submit(make_task(f"task-{length - 1}", depends_on=["task-0"]))
        with pytest.raises(KeyError, match="unknown worker task"):
            scheduler.handle(f"task-{length - 1}")
        assert len(scheduler.snapshots()) == length - 1
        assert scheduler._waiting_for[f"task-{length - 1}"] == {f"task-{length - 2}"}
        scheduler.submit(make_task(f"task-{length - 1}"))
        results = scheduler.wait_blocking([scheduler.handle(f"task-{index}") for index in range(length)],
                                          timeout=2)
        assert all(result.status is WorkerStatus.SUCCEEDED for result in results)
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.parametrize("forward", [False, True])
def test_valid_diamond_dag_preserves_both_submission_orders(forward):
    executed = []

    def runner(task, _context):
        executed.append(task.task_id)
        return "done"

    tasks = [
        make_task("base"),
        make_task("left", depends_on=["base"]),
        make_task("right", depends_on=["base"]),
        make_task("join", depends_on=["left", "right"]),
    ]
    scheduler = WorkerScheduler(runner=runner, max_concurrency=1)
    try:
        handles = [scheduler.submit(task) for task in (reversed(tasks) if forward else tasks)]
        results = scheduler.wait_blocking(handles, timeout=2)
        assert all(result.status is WorkerStatus.SUCCEEDED for result in results)
        assert executed[0] == "base"
        assert set(executed[1:3]) == {"left", "right"}
        assert executed[-1] == "join"
        assert scheduler._waiting_for == {}
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.parametrize("outcome", ["exception", "failed", "expired", "cancelled", "partial-error"])
def test_failed_dependency_cancels_transitive_dependents_even_with_missing_input(outcome):
    executed = []

    def runner(task, _context):
        executed.append(task.task_id)
        if outcome == "exception":
            raise RuntimeError("base failed")
        status = {
            "failed": WorkerStatus.FAILED,
            "expired": WorkerStatus.EXPIRED,
            "cancelled": WorkerStatus.CANCELLED,
            "partial-error": WorkerStatus.PARTIAL,
        }[outcome]
        return {"status": status, "summary": "base failed", "error": "base failed"}

    scheduler = WorkerScheduler(max_concurrency=1)
    try:
        leaf = scheduler.submit(make_task("leaf", depends_on=["middle"]), runner)
        middle = scheduler.submit(make_task("middle", depends_on=["base", "never-submitted"]), runner)
        base = scheduler.submit(make_task("base"), runner)
        assert base.result(timeout=2).status in {WorkerStatus.PARTIAL, WorkerStatus.CANCELLED}
        assert middle.result(timeout=2).status is WorkerStatus.CANCELLED
        assert leaf.result(timeout=2).status is WorkerStatus.CANCELLED
        assert "base" in scheduler.get("middle").error
        assert "middle" in scheduler.get("leaf").error
        assert scheduler._waiting_for == {}
        assert executed == ["base"]
    finally:
        scheduler.close(cancel_pending=True)


def test_partial_dependency_without_error_remains_usable():
    scheduler = WorkerScheduler(max_concurrency=1)
    try:
        dependent = scheduler.submit(make_task("dependent", depends_on=["base"]), lambda *_args: "done")
        base = scheduler.submit(make_task("base"), lambda *_args: {
            "status": WorkerStatus.PARTIAL, "summary": "usable partial evidence",
        })
        assert base.result(timeout=2).status is WorkerStatus.PARTIAL
        assert dependent.result(timeout=2).status is WorkerStatus.SUCCEEDED
    finally:
        scheduler.close(cancel_pending=True)


def test_already_failed_dependency_does_not_leave_later_missing_refs_registered():
    scheduler = WorkerScheduler(max_concurrency=1)
    try:
        base = scheduler.submit(make_task("base"), lambda *_args: {
            "status": WorkerStatus.CANCELLED, "summary": "cancelled dependency",
        })
        assert base.result(timeout=2).status is WorkerStatus.CANCELLED
        dependent = scheduler.submit(make_task("dependent", depends_on=["base", "never-submitted"]),
                                     lambda *_args: "must not execute")
        assert dependent.result(timeout=0).status is WorkerStatus.CANCELLED
        assert scheduler._waiting_for == {}
    finally:
        scheduler.close(cancel_pending=True)


def test_long_missing_dependency_diagnostic_still_resolves_the_future():
    scheduler = WorkerScheduler(wait_timeout_seconds=0.03)
    try:
        handle = scheduler.submit(make_task("dependent", depends_on=["missing-" + "x" * 4_000]),
                                  lambda *_args: "must not execute")
        result = handle.result(timeout=2)
        assert result.status is WorkerStatus.EXPIRED
        assert len(result.error) == 4_000
        assert scheduler._waiting_for == {}
    finally:
        scheduler.close(cancel_pending=True)


def test_early_queue_timer_wakeup_rearms_instead_of_orphaning_the_task():
    scheduler = WorkerScheduler(wait_timeout_seconds=2)
    try:
        handle = scheduler.submit(make_task("dependent", depends_on=["missing"]),
                                  lambda *_args: "must not execute")
        record = scheduler._records["dependent"]
        early_timer = record.queue_timer
        early_timer.cancel()
        scheduler._expire_queued("dependent")
        assert record.queue_timer is not early_timer
        assert not handle.done()
        assert handle.cancel()
        assert handle.result(timeout=0).status is WorkerStatus.CANCELLED
    finally:
        scheduler.close(cancel_pending=True)


def test_cancel_waiting_dependency_propagates_without_waiting_for_missing_task():
    scheduler = WorkerScheduler(runner=lambda *_args: "must not execute")
    try:
        leaf = scheduler.submit(make_task("leaf", depends_on=["base"]))
        base = scheduler.submit(make_task("base", depends_on=["missing"]))
        assert base.cancel()
        assert base.result(timeout=0).status is WorkerStatus.CANCELLED
        assert leaf.result(timeout=0).status is WorkerStatus.CANCELLED
        assert not base.cancel()
        assert scheduler._waiting_for == {}
    finally:
        scheduler.close(cancel_pending=True)


def test_queue_deadline_cancels_dependents_of_an_expired_missing_dependency():
    scheduler = WorkerScheduler(runner=lambda *_args: "must not execute")
    try:
        leaf = scheduler.submit(make_task("leaf", depends_on=["base"]))
        base = scheduler.submit(make_task("base", depends_on=["missing"], timeout_seconds=0.03))
        assert base.result(timeout=2).status is WorkerStatus.EXPIRED
        assert leaf.result(timeout=2).status is WorkerStatus.CANCELLED
        assert scheduler._waiting_for == {}
    finally:
        scheduler.close(cancel_pending=True)


def test_executor_backlog_expires_without_running_the_expired_task():
    started = threading.Event()
    release = threading.Event()
    executed = []

    def runner(task, _context):
        executed.append(task.task_id)
        if task.task_id == "base":
            started.set()
            assert release.wait(2)
        return "done"

    scheduler = WorkerScheduler(runner=runner, max_concurrency=1)
    try:
        base = scheduler.submit(make_task("base"))
        assert started.wait(2)
        queued = scheduler.submit(make_task("queued", timeout_seconds=0.03))
        assert queued.result(timeout=2).status is WorkerStatus.EXPIRED
        release.set()
        assert base.result(timeout=2).status is WorkerStatus.SUCCEEDED
    finally:
        release.set()
        scheduler.close(cancel_pending=True)
    assert executed == ["base"]
    assert queued.status is WorkerStatus.EXPIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 0.01])
async def test_async_wait_timeout_does_not_cancel_scheduler_owned_future(timeout):
    scheduler = WorkerScheduler(max_concurrency=1)
    try:
        dependent = scheduler.submit(make_task("dependent", depends_on=["base"]), lambda *_args: "done")
        with pytest.raises(TimeoutError):
            await scheduler.wait(dependent, timeout=timeout)
        assert not dependent.future.cancelled()
        assert not dependent.done()
        base = scheduler.submit(make_task("base"), lambda *_args: "done")
        results = await scheduler.wait([dependent, base], timeout=2)
        assert all(result.status is WorkerStatus.SUCCEEDED for result in results)
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.asyncio
async def test_async_wait_cancellation_does_not_cancel_shared_worker_future():
    scheduler = WorkerScheduler()
    try:
        dependent = scheduler.submit(make_task("dependent", depends_on=["base"]), lambda *_args: "done")
        waiting = asyncio.create_task(scheduler.wait(dependent))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not dependent.future.cancelled()
        scheduler.submit(make_task("base"), lambda *_args: "done")
        assert (await scheduler.wait(dependent, timeout=2)).status is WorkerStatus.SUCCEEDED
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.asyncio
async def test_async_wait_without_timeout_has_finite_default_deadline():
    scheduler = WorkerScheduler(wait_timeout_seconds=0.03)
    future = Future()
    try:
        with pytest.raises(TimeoutError):
            await scheduler.wait(future)
        assert not future.cancelled()
    finally:
        future.set_result("done")
        scheduler.close(cancel_pending=True)


def test_blocking_wait_without_timeout_has_finite_default_deadline():
    scheduler = WorkerScheduler(wait_timeout_seconds=0.03)
    future = Future()
    try:
        with pytest.raises(TimeoutError):
            scheduler.wait_blocking(future)
        assert not future.cancelled()
    finally:
        future.set_result("done")
        scheduler.close(cancel_pending=True)


def test_handle_result_without_timeout_has_finite_default_deadline():
    started = threading.Event()
    release = threading.Event()

    def runner(_task, _context):
        started.set()
        assert release.wait(2)
        return "done"

    scheduler = WorkerScheduler(wait_timeout_seconds=0.03)
    try:
        handle = scheduler.submit(make_task("running"), runner)
        assert started.wait(2)
        with pytest.raises(TimeoutError):
            handle.result()
        assert handle.status is WorkerStatus.RUNNING
        release.set()
        assert handle.result(timeout=2).status is WorkerStatus.SUCCEEDED
    finally:
        release.set()
        scheduler.close(cancel_pending=True)


def test_blocking_wait_uses_one_deadline_for_the_whole_group(monkeypatch):
    first = Mock()
    second = Mock()
    first.result.return_value = "done"
    second.result.side_effect = TimeoutError
    scheduler = WorkerScheduler()
    try:
        readings = iter([100.0, 100.0, 100.2])
        monkeypatch.setattr("tracefix.workers.scheduler.time.monotonic", lambda: next(readings))
        with pytest.raises(TimeoutError):
            scheduler.wait_blocking([first, second], timeout=0.3)
        assert first.result.call_args.kwargs["timeout"] == pytest.approx(0.3)
        assert second.result.call_args.kwargs["timeout"] == pytest.approx(0.1)
    finally:
        scheduler.close(cancel_pending=True)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), True])
def test_scheduler_rejects_unbounded_default_deadline(timeout):
    with pytest.raises(ValueError, match="finite and positive"):
        WorkerScheduler(wait_timeout_seconds=timeout)
