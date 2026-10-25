"""事务化控制台运行记录与事件存储，并提供轻量词项提取。"""
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from tracefix.runtime.continuation import can_continue
from tracefix.storage.artifacts import redact


def timestamp():
    """返回带时区的当前 UTC 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def database_path():
    """读取控制台数据库路径配置，缺省使用项目私有目录。"""
    return Path(os.getenv('TRACEFIX_CONSOLE_DB', '.tracefix/console.sqlite3'))


def terms(text):
    """提取拉丁词和中文双字词，用于轻量级本地检索（记忆检索等复用）。"""
    result = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(phrase[index:index + 2] for index in range(max(1, len(phrase) - 1)))
    return result


class ConflictError(ValueError):
    """表示乐观并发版本检查失败。"""

    pass


class DocumentLibrary:
    """封装控制台运行记录和事件的 SQLite 事务操作。"""

    def __init__(self, path=None):
        """初始化数据库路径，并创建缺失的基础表。"""
        self.path = Path(path) if path is not None else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS console_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS console_events (run_id TEXT, seq INTEGER, event_key TEXT, data TEXT NOT NULL, PRIMARY KEY (run_id, seq), UNIQUE (run_id, event_key))')

    @contextmanager
    def connect(self):
        """打开带事务上下文的短连接，退出时始终关闭连接。"""
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def runs(self, project=None):
        """返回项目过滤后的运行记录，并按启动时间倒序排列。"""
        with self.connect() as connection:
            records = [json.loads(row[0]) for row in connection.execute('SELECT data FROM console_runs')]
        return sorted((record for record in records if project is None or record['projectId'] == project),
                      key=lambda record: record['startedAt'], reverse=True)

    def run(self, run_id):
        """读取指定运行记录。"""
        with self.connect() as connection:
            row = connection.execute('SELECT data FROM console_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError('运行记录不存在')
        return json.loads(row[0])

    def update_run(self, run_id, fields, create=False):
        """在写事务中更新运行字段；``create`` 为真时缺失记录会被新建。"""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT data FROM console_runs WHERE id=?', (run_id,)).fetchone()
            if row is None and not create:
                raise FileNotFoundError('运行记录不存在')
            record = json.loads(row[0]) if row else {'id': run_id, 'startedAt': timestamp(), 'logs': []}
            if record.get('stopRequestedAt') and fields.get('status') in {
                    'running', 'paused', 'waiting_input', 'waiting_approval'}:
                fields = {**fields, 'status': 'stopping'}
            record.update(fields, updatedAt=timestamp())
            connection.execute('INSERT OR REPLACE INTO console_runs VALUES (?,?)', (run_id, json.dumps(record, ensure_ascii=False)))
        return record

    def continue_run(self, run_id, instruction):
        """为可继续的失败运行写入恢复标记并重置运行状态。"""
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000:
            raise ValueError('请输入 1-4000 字的继续指令')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT data FROM console_runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                raise FileNotFoundError('运行记录不存在')
            record = json.loads(row[0])
            if not can_continue(record['status'], record.get('outcome')):
                raise ConflictError('只有非成功结束的任务可以继续；请刷新任务状态')
            marker = redact({'id': str(uuid.uuid4()), 'instruction': instruction.strip(), 'at': timestamp(),
                'previous_status': record['status'], 'previous_phase': record.get('phase'),
                'previous_outcome': record.get('outcome'), 'previous_error': record.get('error'),
                'previous_finished_at': record.get('finishedAt'), 'previous_report_ref': record.get('reportRef')})
            markers = [*record.get('continuationMarkers', []), marker]
            record.update(continuationMarkers=markers, continuationCount=len(markers), abnormalTermination=True,
                          continuationInstruction=marker['instruction'], continuationId=marker['id'],
                          status='running', phase='STARTING', outcome=None, error=None, reportRef=None,
                          branch=None, finishedAt=None, exitCode=None, processEndedAt=None, pid=None,
                          processControlId=None, stopRequestedAt=None, stopPreviousStatus=None, stopError=None,
                          updatedAt=timestamp())
            self._append_event(connection, run_id, 'continuation:' + marker['id'], {
                'type': 'run.continuation_requested', 'phase': 'STARTING',
                'at': datetime.fromisoformat(marker['at']).timestamp(), 'payload': marker})
            connection.execute('UPDATE console_runs SET data=? WHERE id=?', (json.dumps(record, ensure_ascii=False), run_id))
        return record

    def _append_event(self, connection, run_id, event_key, event):
        """在现有事务中以递增序号追加幂等事件。"""
        seq = connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM console_events WHERE run_id=?', (run_id,)).fetchone()[0]
        connection.execute('INSERT OR IGNORE INTO console_events VALUES (?,?,?,?)',
                           (run_id, seq, event_key, json.dumps(redact({**event, 'seq': seq}), ensure_ascii=False)))

    def append_event(self, run_id, event):
        """将代理事件包装后追加到指定运行。"""
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._append_event(connection, run_id, 'agent:' + str(event['seq']), {**event, 'agentSeq': event['seq']})

    def run_events(self, run_id, after=0):
        """读取运行在给定序号之后的事件，保持数据库顺序。"""
        self.run(run_id)
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT data FROM console_events WHERE run_id=? AND seq>? ORDER BY seq', (run_id, after))]
