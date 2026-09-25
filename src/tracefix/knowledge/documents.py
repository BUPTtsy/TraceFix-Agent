"""Transactional console records and bounded, project-scoped document retrieval."""
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
    return datetime.now(timezone.utc).isoformat()


def database_path():
    return Path(os.getenv('TRACEFIX_CONSOLE_DB', '.tracefix/console.sqlite3'))


def terms(text):
    result = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(phrase[index:index + 2] for index in range(max(1, len(phrase) - 1)))
    return result


class ConflictError(ValueError):
    pass


class DocumentLibrary:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS console_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS console_events (run_id TEXT, seq INTEGER, event_key TEXT, data TEXT NOT NULL, PRIMARY KEY (run_id, seq), UNIQUE (run_id, event_key))')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def documents(self, project=None, include_disabled=True):
        with self.connect() as connection:
            records = [json.loads(row[0]) for row in connection.execute('SELECT data FROM documents')]
        return sorted((record for record in records
                       if (project is None or record['projectId'] in {None, project})
                       and (include_disabled or record['enabled'])), key=lambda record: record['updatedAt'], reverse=True)

    def document(self, document_id):
        with self.connect() as connection:
            row = connection.execute('SELECT data FROM documents WHERE id=?', (document_id,)).fetchone()
        if row is None:
            raise FileNotFoundError('文档不存在')
        return json.loads(row[0])

    def save_document(self, fields, document_id=None):
        title, content = fields.get('title', ''), fields.get('content', '')
        tags = fields.get('tags', [])
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValueError('文档标题须为 1-120 字')
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 262144 or '\x00' in content:
            raise ValueError('文档须为非空 UTF-8 文本，最大 256 KiB')
        if not isinstance(tags, list) or len(tags) > 12 or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 30 for tag in tags):
            raise ValueError('最多 12 个标签，每个标签 1-30 字')
        if fields.get('kind') not in {'repair', 'testing', 'experience'} or not isinstance(fields.get('enabled'), bool):
            raise ValueError('文档类型或启用状态无效')
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = None
            if document_id:
                row = connection.execute('SELECT data FROM documents WHERE id=?', (document_id,)).fetchone()
                if row is None:
                    raise FileNotFoundError('文档不存在')
                previous = json.loads(row[0])
                if fields.get('version') != previous['version']:
                    raise ConflictError('文档已被其他窗口更新，请重新打开后编辑')
            now = timestamp()
            record = {'id': document_id or str(uuid.uuid4()), 'title': title.strip(), 'content': content,
                      'tags': list(dict.fromkeys(tag.strip() for tag in tags)), 'kind': fields['kind'],
                      'enabled': fields['enabled'], 'projectId': fields.get('projectId') or None,
                      'sourceRunId': previous['sourceRunId'] if previous else fields.get('sourceRunId') or None,
                      'version': previous['version'] + 1 if previous else 1,
                      'createdAt': previous['createdAt'] if previous else now, 'updatedAt': now}
            connection.execute('INSERT OR REPLACE INTO documents VALUES (?,?)', (record['id'], json.dumps(record, ensure_ascii=False)))
        return record

    def search(self, query, project, limit=8):
        query_terms = terms(query[:2000])
        if not query_terms:
            return []
        candidates = []
        for record in self.documents(project, include_disabled=False):
            title_terms = terms(record['title'] + ' ' + ' '.join(record['tags']))
            title_score = len(query_terms & title_terms) * 3
            chunks = [record['content'][offset:offset + 1400] for offset in range(0, len(record['content']), 1200)]
            ranked = [(title_score + len(query_terms & terms(chunk)), index, chunk) for index, chunk in enumerate(chunks)]
            score, chunk_index, excerpt = max(ranked, key=lambda item: (item[0], -item[1]))
            if score:
                candidates.append({key: value for key, value in record.items() if key != 'content'} |
                                  {'excerpt': excerpt, 'chunk': chunk_index, 'score': score})
        return sorted(candidates, key=lambda item: (-item['score'], item['id']))[:max(1, min(limit, 20))]

    def runs(self, project=None):
        with self.connect() as connection:
            records = [json.loads(row[0]) for row in connection.execute('SELECT data FROM console_runs')]
        return sorted((record for record in records if project is None or record['projectId'] == project),
                      key=lambda record: record['startedAt'], reverse=True)

    def run(self, run_id):
        with self.connect() as connection:
            row = connection.execute('SELECT data FROM console_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError('运行记录不存在')
        return json.loads(row[0])

    def update_run(self, run_id, fields, create=False, knowledge=None):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT data FROM console_runs WHERE id=?', (run_id,)).fetchone()
            if row is None and not create:
                raise FileNotFoundError('运行记录不存在')
            record = json.loads(row[0]) if row else {'id': run_id, 'startedAt': timestamp(), 'knowledge': [], 'logs': []}
            record.update(fields, updatedAt=timestamp())
            if knowledge is not None:
                record['knowledge'] = [*record.get('knowledge', []), knowledge][-30:]
            connection.execute('INSERT OR REPLACE INTO console_runs VALUES (?,?)', (run_id, json.dumps(record, ensure_ascii=False)))
        return record

    def continue_run(self, run_id, instruction):
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
                          branch=None, finishedAt=None, exitCode=None, processEndedAt=None, pid=None, updatedAt=timestamp())
            self._append_event(connection, run_id, 'continuation:' + marker['id'], {
                'type': 'run.continuation_requested', 'phase': 'STARTING',
                'at': datetime.fromisoformat(marker['at']).timestamp(), 'payload': marker})
            connection.execute('UPDATE console_runs SET data=? WHERE id=?', (json.dumps(record, ensure_ascii=False), run_id))
        return record

    def _append_event(self, connection, run_id, event_key, event):
        seq = connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM console_events WHERE run_id=?', (run_id,)).fetchone()[0]
        connection.execute('INSERT OR IGNORE INTO console_events VALUES (?,?,?,?)',
                           (run_id, seq, event_key, json.dumps(redact({**event, 'seq': seq}), ensure_ascii=False)))

    def append_event(self, run_id, event):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            self._append_event(connection, run_id, 'agent:' + str(event['seq']), {**event, 'agentSeq': event['seq']})

    def run_events(self, run_id, after=0):
        self.run(run_id)
        with self.connect() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT data FROM console_events WHERE run_id=? AND seq>? ORDER BY seq', (run_id, after))]
