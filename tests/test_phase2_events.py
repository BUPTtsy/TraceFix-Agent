import base64
import json
import threading
from types import SimpleNamespace

import pytest

from tracefix.cli.main import Session
from tracefix.runtime.contracts import RunState
from tracefix.runtime.engine import Engine
from tracefix.runtime.event_adapter import CursorError, EventAdapter, EventCursor
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore


def stored_run():
    store = MemoryStore()
    state = RunState(run_id="run-1", scope_id="project-1", goal="test", url="http://app")
    store.save(state)
    store.event(state, "run.started", {"child_run_id": "child-1", "role": "scout"})
    store.event(state, "subtask.completed", {"child_run_id": "child-1", "status": "completed"})
    return store, state


def test_event_adapter_replays_scoped_history_and_high_watermark():
    store, state = stored_run()
    adapter = EventAdapter(store)
    first = adapter.read(state.run_id, state.scope_id)
    assert [event["seq"] for event in first.events] == [1, 2]
    assert EventCursor.decode(first.cursor, state.scope_id, state.run_id).seq == 2

    second = adapter.read(state.run_id, state.scope_id, first.cursor)
    assert second.events == []
    assert second.snapshot is None
    assert second.requires_new_observation is False


def test_event_adapter_resyncs_ahead_cursor_with_child_snapshot():
    store, state = stored_run()
    adapter = EventAdapter(store)
    ahead = EventCursor(state.scope_id, state.run_id, 99).encode()
    result = adapter.read(state.run_id, state.scope_id, ahead)
    assert result.events == []
    assert result.snapshot["reason"] == "cursor_ahead"
    assert result.snapshot["high_watermark"] == 2
    assert result.snapshot["child_identities"][0]["child_run_id"] == "child-1"
    assert result.requires_new_observation is True


def test_event_adapter_detects_durable_gap_and_lists_pending_operations():
    store, state = stored_run()
    store.events[state.run_id].pop(0)
    store.operations["op-1"] = {
        "run_id": state.run_id, "scope_id": state.scope_id, "status": "UNKNOWN",
        "resources": ["browser"], "epoch": "epoch-1", "resolution": None,
    }
    result = EventAdapter(store).read(state.run_id, state.scope_id)
    assert result.snapshot["reason"] == "cursor_expired"
    assert result.snapshot["pending_operations"][0]["operation_id"] == "op-1"


def test_event_cursor_rejects_cross_run_reuse():
    store, state = stored_run()
    store.save(RunState(run_id="run-2", scope_id=state.scope_id, goal="other", url="http://app"))
    cursor = EventAdapter(store).read(state.run_id, state.scope_id).cursor
    with pytest.raises(CursorError, match="作用域"):
        EventAdapter(store).read("run-2", state.scope_id, cursor)


@pytest.mark.parametrize("fields", [
    {"scope_id": "project-1", "run_id": "run-1", "seq": True},
    {"scope_id": "project-1", "run_id": "run-1", "seq": "1"},
    {"scope_id": "project-1", "run_id": "run-1", "seq": -1},
    {"scope_id": "project-1", "run_id": "run-1", "seq": 1, "extra": "ignored"},
])
def test_invalid_cursor_requires_snapshot(fields):
    store, state = stored_run()
    raw = json.dumps(fields, separators=(",", ":")).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    result = EventAdapter(store).read(state.run_id, state.scope_id, cursor)
    assert result.snapshot["reason"] == "cursor_invalid"
    assert result.high_watermark == result.snapshot["high_watermark"] == 2


@pytest.mark.parametrize("cursor", ["", "!!!!", "e30=", "a"])
def test_noncanonical_cursor_requires_snapshot(cursor):
    store, state = stored_run()
    result = EventAdapter(store).read(state.run_id, state.scope_id, cursor)
    assert result.snapshot["reason"] == "cursor_invalid"


def engine_consumer(store, artifacts, notify=None):
    engine = Engine.__new__(Engine)
    engine.store = store
    engine.artifacts = artifacts
    engine.event_adapter = EventAdapter(store)
    engine._trace_sequences = {}
    engine.notify = notify
    return engine


def test_engine_notify_recovers_missed_live_events_and_deduplicates(tmp_path):
    store, state = stored_run()
    third = store.event(state, "state.changed", {"status": "RUNNING"})
    received = []
    engine = engine_consumer(store, Artifacts(tmp_path), received.append)
    batch = engine._notify(third)
    assert [event["seq"] for event in received] == [1, 2, 3]
    assert batch.high_watermark == engine.artifacts.trace_position(state.scope_id, state.run_id) == 3
    assert engine._notify(third).events == []
    assert engine._notify(store.events[state.run_id][0]).events == []
    assert len(received) == 3


def test_engine_notify_durable_gap_returns_snapshot_without_jump_or_recursion(tmp_path):
    store, state = stored_run()
    artifacts = Artifacts(tmp_path)
    artifacts.append_trace(store.events[state.run_id][0])
    third = store.event(state, "state.changed", {"status": "RUNNING"})
    second = store.events[state.run_id].pop(1)
    received = []
    engine = engine_consumer(store, artifacts, received.append)
    batch = engine._notify(third)
    assert batch.snapshot["reason"] == "durable_gap"
    assert batch.high_watermark == 3
    assert artifacts.trace_position(state.scope_id, state.run_id) == 1
    assert received == [] and len(store.events[state.run_id]) == 2
    assert engine.read_events(state, batch.cursor).events == []
    store.events[state.run_id].insert(1, second)
    assert [event["seq"] for event in engine._notify(third).events] == [2, 3]
    assert artifacts.trace_position(state.scope_id, state.run_id) == 3


async def test_cli_trace_uses_adapter_and_keeps_default_history(tmp_path):
    store, state = stored_run()
    engine = engine_consumer(store, Artifacts(tmp_path))
    events, texts = [], []
    session = Session.__new__(Session)
    session.run_id, session.scope = state.run_id, state.scope_id
    session.store, session.engine = store, engine
    session.render = SimpleNamespace(event=lambda event, replay=False: events.append((event, replay)), text=texts.append)
    assert await session.dispatch("/trace") is None
    assert [event["seq"] for event, replay in events if replay] == [1, 2]
    cursor = EventCursor(state.scope_id, state.run_id, 1).encode()
    result = await session.dispatch("/trace " + cursor)
    assert [event["seq"] for event in result["events"]] == [2]
    assert json.loads(texts[-1])["high_watermark"] == 2


def test_snapshot_reconnect_preserves_child_for_wait_without_dispatch(tmp_path):
    store, state = stored_run()
    store.event(state, "subtask.started", {"child_run_id": "pending-child", "task_id": "task-1", "status": "RUNNING"})
    store.event(state, "subtask.wait_timeout", {"child_run_id": "pending-child", "task_id": "task-1"})
    engine = engine_consumer(store, Artifacts(tmp_path))
    result = engine.read_events(state, EventCursor(state.scope_id, state.run_id, 100).encode())
    waits = []
    children = SimpleNamespace(wait=waits.append, dispatch=lambda: pytest.fail("must not dispatch replacement child"))
    pending = next(child for child in result.snapshot["child_identities"] if child.get("task_id") == "task-1")
    children.wait(pending["child_run_id"])
    assert waits == ["pending-child"]
    assert pending["status"] == "RUNNING"
    assert result.requires_new_observation


def test_memory_snapshot_uses_one_lock_for_state_operations_and_watermark():
    store, state = stored_run()
    original_load = store.load
    writer_started, writer_done = threading.Event(), threading.Event()
    writer = None

    def save_after_snapshot():
        writer_started.set()
        with store._operation_lock:
            changed = state.model_copy(update={"revision": 1})
            store.save(changed)
            store.event(changed, "state.changed")
        writer_done.set()

    def load_with_writer(run_id, scope):
        nonlocal writer
        loaded = original_load(run_id, scope)
        writer = threading.Thread(target=save_after_snapshot)
        writer.start()
        assert writer_started.wait(1)
        assert not writer_done.is_set()
        return loaded

    store.load = load_with_writer
    result = EventAdapter(store).read(state.run_id, state.scope_id, "invalid!")
    writer.join(2)
    assert writer_done.is_set()
    assert result.snapshot["state"]["revision"] == 0
    assert result.high_watermark == result.snapshot["high_watermark"] == 2
    assert store.events[state.run_id][-1]["seq"] == 3
