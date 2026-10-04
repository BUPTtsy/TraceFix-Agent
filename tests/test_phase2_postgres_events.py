import os
import threading
from uuid import uuid4

import pytest

from tracefix.runtime.contracts import RunState
from tracefix.runtime.event_adapter import EventAdapter
from tracefix.storage.store import PostgresStore


@pytest.fixture
def isolated_postgres():
    database = os.getenv('PHASE2_DATABASE_URL')
    if not database:
        pytest.skip('需要显式 PHASE2_DATABASE_URL 隔离数据库')
    stores = [PostgresStore(database), PostgresStore(database)]
    stores[0].setup()
    try:
        yield stores
    finally:
        for store in stores:
            store.close()


def test_postgres_cursor_reconnect_and_snapshot_keep_pending_operation(isolated_postgres):
    writer, reader = isolated_postgres
    state = RunState(run_id='phase2_' + uuid4().hex, scope_id='phase2-events',
                     goal='独立 PostgreSQL 事件续传', url='http://app:3000')
    writer.save(state)
    writer.event(state, 'subtask.started', {'child_run_id': 'same-child', 'status': 'RUNNING'})
    first = EventAdapter(reader).read(state.run_id, state.scope_id)
    writer.begin(state, 'phase2-op-' + uuid4().hex, {'kind': 'observe', 'resources': ['browser']})

    resumed = EventAdapter(reader).read(state.run_id, state.scope_id, first.cursor)
    assert [event['seq'] for event in resumed.events] == [2]
    snapshot = EventAdapter(reader).read(state.run_id, state.scope_id, 'invalid!')
    assert snapshot.high_watermark == snapshot.snapshot['high_watermark'] == 2
    assert snapshot.snapshot['pending_operations'][0]['status'] == 'STARTED'
    assert snapshot.snapshot['child_identities'][0]['child_run_id'] == 'same-child'
    assert snapshot.requires_new_observation


def test_postgres_snapshot_is_coherent_while_other_connection_updates(isolated_postgres):
    writer, reader = isolated_postgres
    state = RunState(run_id='phase2_' + uuid4().hex, scope_id='phase2-events',
                     goal='独立 PostgreSQL 权威水位', url='http://app:3000')
    writer.save(state)
    writer.event(state, 'state.changed')
    original_load = reader.load
    started, finished = threading.Event(), threading.Event()
    errors = []
    writer_thread = None

    def update():
        started.set()
        try:
            with writer.conn.transaction():
                next_state = state.model_copy(update={'revision': 1})
                writer.save(next_state)
                writer.event(next_state, 'state.changed')
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()

    def load_then_update(run_id, scope):
        nonlocal writer_thread
        loaded = original_load(run_id, scope)
        writer_thread = threading.Thread(target=update)
        writer_thread.start()
        assert started.wait(1)
        assert not finished.is_set()
        return loaded

    reader.load = load_then_update
    cut = EventAdapter(reader).read(state.run_id, state.scope_id, 'invalid!')
    writer_thread.join(5)
    assert finished.is_set() and errors == []
    assert cut.snapshot['state']['revision'] == 0
    assert cut.high_watermark == 1
    reader.load = original_load
    resumed = EventAdapter(reader).read(state.run_id, state.scope_id, cut.cursor)
    assert [event['seq'] for event in resumed.events] == [2]
