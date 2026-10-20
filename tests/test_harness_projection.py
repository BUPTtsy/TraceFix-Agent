import sqlite3

import pytest

from tracefix.storage.harness import HarnessConflict
from tracefix.storage.harness_projection import project_pending, read_projection
from test_harness_store import ledger, postgres_harness_pair, initialized


def test_postgres_outbox_projects_to_real_node_sqlite_and_replays(ledger, tmp_path):
    job = initialized(ledger)
    path = tmp_path / 'console.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE console_tasks (id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        connection.execute('INSERT INTO console_tasks VALUES (?,?)', ('console', '{"definition":"frozen"}'))
    first = project_pending(ledger, path, 'job', 'scope')
    assert first['projected'] > 0
    state = read_projection(path, 'job', scope_id='scope')
    assert state['console_task_id'] == 'console'
    assert state['revision'] == job.revision
    assert state['accepted_commit'] == job.accepted_commit
    assert project_pending(ledger, path, 'job', 'scope')['projected'] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT data FROM console_tasks').fetchone()[0] == '{"definition":"frozen"}'
        assert connection.execute('SELECT COUNT(*) FROM harness_event_receipts').fetchone()[0] == first['last_seq']


def test_projection_fault_rolls_back_and_restart_replays(ledger, tmp_path):
    initialized(ledger)
    path = tmp_path / 'console.sqlite3'

    def fail(event):
        if event.seq == 2:
            raise RuntimeError('simulated projection process crash')

    with pytest.raises(RuntimeError, match='simulated'):
        project_pending(ledger, path, 'job', 'scope', after_apply=fail)
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT last_seq FROM harness_job_projection').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM harness_event_receipts').fetchone()[0] == 1
    result = project_pending(ledger, path, 'job', 'scope')
    assert result['last_seq'] == len(ledger.outbox_events('job', scope_id='scope'))
    assert read_projection(path, 'job', scope_id='scope')['plan_version'] == 1


def test_projection_rejects_cross_scope_and_corrupt_stream(ledger, tmp_path, monkeypatch):
    initialized(ledger)
    path = tmp_path / 'console.sqlite3'
    with pytest.raises(PermissionError):
        project_pending(ledger, path, 'job', 'other')
    events = ledger.outbox_events('job', scope_id='scope')
    monkeypatch.setattr(ledger, 'outbox_events', lambda *args, **kwargs: events[1:])
    with pytest.raises(HarnessConflict, match='跳序'):
        project_pending(ledger, path, 'job', 'scope')


def test_projection_rejects_old_epoch_event(ledger, tmp_path, monkeypatch):
    job = initialized(ledger)
    ledger.claim_work_item('job', job.revision, 'owner', scope_id='scope')
    path = tmp_path / 'console.sqlite3'
    project_pending(ledger, path, 'job', 'scope')
    events = ledger.outbox_events('job', scope_id='scope')
    forged = events[-1].model_copy(deep=True)
    forged.seq += 1
    forged.owner_epoch = 0
    monkeypatch.setattr(ledger, 'outbox_events', lambda *args, **kwargs: [forged])
    with pytest.raises(HarnessConflict, match='epoch'):
        project_pending(ledger, path, 'job', 'scope')
