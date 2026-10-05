import base64
import json

from tracefix.runtime.event_adapter import EventAdapter, EventCursor, CLI_CONTRACT_VERSION
from tracefix.runtime.contracts import RunState
from tracefix.storage.store import MemoryStore


def stored_run():
    store = MemoryStore()
    state = RunState(run_id='run-1', scope_id='project-1', goal='test', url='http://app')
    store.save(state)
    store.event(state, 'run.started', {'parent_run_id': None})
    store.event(state, 'subtask.completed', {'child_run_id': 'child-1', 'status': 'completed'})
    return store, state


def test_non_tty_contract_is_versioned_and_preserves_cursor_snapshot_refs():
    store, state = stored_run()
    result = EventAdapter(store).read(state.run_id, state.scope_id)
    payload = result.as_dict()

    assert payload['contract_version'] == CLI_CONTRACT_VERSION
    assert payload['cursor'] == result.cursor
    assert payload['high_watermark'] == 2
    assert [event['seq'] for event in payload['events']] == [1, 2]
    assert payload['events'][1]['payload']['child_run_id'] == 'child-1'


def test_cli_contract_marks_parent_history_slice_incomplete_on_missing_event():
    store, state = stored_run()
    store.events[state.run_id].pop(0)
    cursor = EventCursor(state.scope_id, state.run_id, 0).encode()
    result = EventAdapter(store).read(state.run_id, state.scope_id, cursor)

    assert result.snapshot['reason'] == 'cursor_expired'
    assert result.snapshot['child_history_complete'] is False
    assert result.requires_new_observation is True


def test_cli_contract_rejects_invalid_cursor_without_replaying_child():
    store, state = stored_run()
    raw = json.dumps({'scope_id': state.scope_id, 'run_id': state.run_id, 'seq': True}).encode()
    cursor = base64.urlsafe_b64encode(raw).decode().rstrip('=')
    result = EventAdapter(store).read(state.run_id, state.scope_id, cursor)

    assert result.snapshot['reason'] == 'cursor_invalid'
    assert result.snapshot['child_identities'][0]['child_run_id'] == 'child-1'
    assert result.events == []
