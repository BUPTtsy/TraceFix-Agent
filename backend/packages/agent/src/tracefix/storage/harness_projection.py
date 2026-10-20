from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from tracefix.runtime.contracts import digest
from tracefix.runtime.harness_contracts import HarnessJob
from tracefix.storage.harness import HarnessConflict


def _setup(connection):
    connection.execute('PRAGMA busy_timeout=15000')
    connection.execute('CREATE TABLE IF NOT EXISTS harness_job_projection ('
        'job_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, console_task_id TEXT NOT NULL, '
        'last_seq INTEGER NOT NULL, owner_epoch INTEGER NOT NULL, revision INTEGER NOT NULL, '
        'data TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS harness_event_receipts ('
        'job_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, '
        'event_hash TEXT NOT NULL, PRIMARY KEY(job_id,seq))')
    connection.execute('CREATE INDEX IF NOT EXISTS harness_projection_console_task '
        'ON harness_job_projection(console_task_id,scope_id)')


def project_pending(store, sqlite_path, job_id, scope_id, *, after_apply=None):
    store.get_job(job_id, scope_id=scope_id)
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    projected = 0
    try:
        _setup(connection)
        row = connection.execute('SELECT scope_id,last_seq FROM harness_job_projection WHERE job_id=?', (job_id,)).fetchone()
        if row and row['scope_id'] != scope_id:
            raise PermissionError('投影 Job 不属于当前 scope')
        events = store.outbox_events(job_id, scope_id=scope_id, after=row['last_seq'] if row else 0)
        for event in events:
            connection.execute('BEGIN IMMEDIATE')
            try:
                receipt = connection.execute('SELECT event_id,event_hash FROM harness_event_receipts WHERE job_id=? AND seq=?',
                    (job_id, event.seq)).fetchone()
                event_hash = digest(event)
                if receipt:
                    if receipt['event_id'] != event.event_id or receipt['event_hash'] != event_hash:
                        raise HarnessConflict('投影事件序号内容冲突')
                    connection.execute('COMMIT')
                    continue
                previous = connection.execute('SELECT * FROM harness_job_projection WHERE job_id=?', (job_id,)).fetchone()
                last_seq = previous['last_seq'] if previous else 0
                owner_epoch = previous['owner_epoch'] if previous else 0
                if event.job_id != job_id or event.scope_id != scope_id:
                    raise PermissionError('outbox 事件不属于当前 Job/scope')
                if event.seq != last_seq + 1 or event.owner_epoch < owner_epoch:
                    raise HarnessConflict('投影事件跳序或 owner epoch 倒退')
                job = HarnessJob.model_validate(event.payload['job'])
                if (job.job_id, job.scope_id, job.owner_epoch, job.revision) != (
                        job_id, scope_id, event.owner_epoch, event.revision):
                    raise HarnessConflict('投影快照与权威事件身份不一致')
                if previous and (previous['scope_id'] != scope_id or previous['console_task_id'] != job.console_task_id
                                 or previous['revision'] > job.revision):
                    raise HarnessConflict('投影身份变化或 revision 倒退')
                connection.execute('INSERT INTO harness_event_receipts VALUES (?,?,?,?)',
                    (job_id, event.seq, event.event_id, event_hash))
                connection.execute('INSERT INTO harness_job_projection VALUES (?,?,?,?,?,?,?) '
                    'ON CONFLICT(job_id) DO UPDATE SET last_seq=excluded.last_seq, '
                    'owner_epoch=excluded.owner_epoch,revision=excluded.revision,data=excluded.data',
                    (job_id, scope_id, job.console_task_id, event.seq, job.owner_epoch, job.revision,
                     json.dumps(job.model_dump(mode='json'), ensure_ascii=False)))
                if after_apply:
                    after_apply(event)
                connection.execute('COMMIT')
                projected += 1
            except BaseException:
                connection.execute('ROLLBACK')
                raise
        row = connection.execute('SELECT last_seq FROM harness_job_projection WHERE job_id=?', (job_id,)).fetchone()
        return {'job_id': job_id, 'projected': projected, 'last_seq': row['last_seq'] if row else 0}
    finally:
        connection.close()


def read_projection(sqlite_path, job_id, *, scope_id):
    connection = sqlite3.connect(f'{Path(sqlite_path).resolve().as_uri()}?mode=ro', uri=True)
    try:
        row = connection.execute('SELECT scope_id,data FROM harness_job_projection WHERE job_id=?', (job_id,)).fetchone()
        if not row or row[0] != scope_id:
            raise PermissionError('当前 scope 中不存在 Job 投影')
        return json.loads(row[1])
    finally:
        connection.close()
