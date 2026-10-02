"""分层保存 Run/Job 记忆，并提供按作用域过滤的 SQLite FTS5 项目检索。

本模块提供本地可审计的分层记忆：L1 是单次 Run 的工作记忆，L2 是同一
Job 可复用的记忆，L3 是需要审核后才能成为可信知识的候选/长期记忆。
SQLite 同时作为 PostgreSQL 不可用时的本地检索回退，并沿用作用域、版本
和证据引用约束。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field

from tracefix.knowledge.documents import terms
from tracefix.runtime.contracts import Contract, digest, new_id
from tracefix.storage.artifacts import redact


# Run 工作记忆的结构化条目；证据引用由写入方按当前 Run 校验。
class MemoryNote(Contract):
    id: str | None = None
    kind: Literal['hypothesis', 'excluded', 'finding', 'todo', 'progress'] = 'finding'
    text: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict = Field(default_factory=dict)


class MemoryLibrary:
    """维护 L1/L2 记录与 L3 本地索引的 SQLite 存储。

    表结构中的 ``source_manifest``、``revision`` 和 ``scope_id`` 用来阻止
    不同源码快照或不同作用域的记忆混用；FTS5 只负责候选召回，最终可见性
    仍在 SQL 过滤中完成。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript('''
              CREATE TABLE IF NOT EXISTS run_memory (
                id TEXT NOT NULL, scope_id TEXT NOT NULL, run_id TEXT NOT NULL,
                source_manifest TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(scope_id, run_id, id));
              CREATE TABLE IF NOT EXISTS job_memory (
                id TEXT NOT NULL, scope_id TEXT NOT NULL, job_id TEXT NOT NULL,
                source_manifest TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(scope_id, job_id, id));
              CREATE TABLE IF NOT EXISTS local_memory_items (
                id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, layer TEXT NOT NULL,
                kind TEXT NOT NULL, logical_key TEXT NOT NULL, visibility TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL, source_revision TEXT NOT NULL,
                source_run_id TEXT, content TEXT NOT NULL, content_hash TEXT NOT NULL,
                embedding TEXT, expires_at REAL, search_text TEXT NOT NULL);
              CREATE VIRTUAL TABLE IF NOT EXISTS local_memory_fts USING fts5(
                search_text, content='local_memory_items', content_rowid='rowid');
              CREATE TRIGGER IF NOT EXISTS local_memory_insert AFTER INSERT ON local_memory_items BEGIN
                INSERT INTO local_memory_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
              END;
              CREATE TRIGGER IF NOT EXISTS local_memory_delete AFTER DELETE ON local_memory_items BEGIN
                INSERT INTO local_memory_fts(local_memory_fts,rowid,search_text)
                VALUES ('delete',old.rowid,old.search_text);
              END;
              CREATE TRIGGER IF NOT EXISTS local_memory_update AFTER UPDATE ON local_memory_items BEGIN
                INSERT INTO local_memory_fts(local_memory_fts,rowid,search_text)
                VALUES ('delete',old.rowid,old.search_text);
                INSERT INTO local_memory_fts(rowid,search_text) VALUES (new.rowid,new.search_text);
              END;
              CREATE INDEX IF NOT EXISTS local_memory_scope ON local_memory_items(scope_id,layer,status,revision);
            ''')

    @contextmanager
    def connect(self):
        """以短事务打开连接，确保 CLI/服务进程间写入及时提交并释放锁。"""
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def note(self, scope_id, run_id, note, *, allowed_evidence_refs=(), source_manifest=''):
        """写入 L1 工作记忆，并拒绝超出 Run 证据集合的引用。"""
        note = note if isinstance(note, MemoryNote) else MemoryNote.model_validate(note)
        if not set(note.evidence_refs) <= set(allowed_evidence_refs):
            raise PermissionError('工作记忆包含未授权的证据引用')
        record = redact(note.model_dump(mode='json'))
        record.update(id=note.id or new_id('note'), scope_id=scope_id, run_id=run_id,
                      source_run_id=run_id, source_manifest=source_manifest, updated_at=time.time())
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO run_memory VALUES (?,?,?,?,?)',
                               (record['id'], scope_id, run_id, source_manifest,
                                json.dumps(record, ensure_ascii=False)))
        return record

    def working_memory(self, scope_id, run_id):
        """按记忆类型恢复单个 Run 的 L1 分组，供上下文组装使用。"""
        with self.connect() as connection:
            notes = [json.loads(row['data']) for row in connection.execute(
                'SELECT data FROM run_memory WHERE scope_id=? AND run_id=? ORDER BY rowid',
                (scope_id, run_id))]
        return {kind: [note for note in notes if note['kind'] == kind]
                for kind in ('hypothesis', 'excluded', 'finding', 'todo', 'progress')}

    def save_job_memory(self, scope_id, job_id, content, *, source_run_id, source_manifest,
                        allowed_evidence_refs=()):
        """写入 L2 Job 记忆，并把来源 Run 和源码 manifest 一起固定下来。"""
        if not job_id:
            raise ValueError('Job 记忆需要显式 job_id')
        if not set(content.get('evidence_refs') or []) <= set(allowed_evidence_refs):
            raise PermissionError('Job 记忆包含未授权的证据引用')
        record = redact({**content, 'id': content.get('id') or new_id('job_memory'),
                         'source_run_id': source_run_id, 'source_manifest': source_manifest,
                         'scope_id': scope_id, 'job_id': job_id})
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO job_memory VALUES (?,?,?,?,?)',
                               (record['id'], scope_id, job_id, source_manifest,
                                json.dumps(record, ensure_ascii=False)))
        return record

    def job_memory(self, scope_id, job_id, source_manifest):
        """只读取同一作用域、Job 和源码 manifest 的 L2 记忆。"""
        if not job_id:
            return []
        with self.connect() as connection:
            return [json.loads(row['data']) for row in connection.execute(
                'SELECT data FROM job_memory WHERE scope_id=? AND job_id=? AND source_manifest=? ORDER BY rowid',
                (scope_id, job_id, source_manifest))]

    def upsert(self, record):
        """插入本地索引项并维护 FTS5 词法字段；结构化内容经脱敏后序列化。"""
        record = dict(record)
        content = record['content']
        content = json.dumps(redact(content), ensure_ascii=False) if not isinstance(content, str) else content
        search_text = ' '.join(sorted(terms(content)))
        values = (record['id'], record['scope_id'], record['layer'], record['kind'],
                  record.get('logical_key', record['id']), record.get('visibility', 'LOCAL'),
                  record.get('status', 'candidate'), record['revision'], record['source_revision'],
                  record.get('source_run_id'), content, digest(content),
                  json.dumps(record['embedding']) if record.get('embedding') else None,
                  record.get('expires_at'), search_text)
        with self.connect() as connection:
            connection.execute('''INSERT OR REPLACE INTO local_memory_items
              (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,
               source_run_id,content,content_hash,embedding,expires_at,search_text)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)

    def prune_index(self, scope_id, source_revision, current_ids):
        """删除当前源码版本中已不存在的卡片，保留旧版本的不可变快照。"""
        with self.connect() as connection:
            rows = connection.execute('SELECT id FROM local_memory_items WHERE scope_id=? AND layer=? AND source_revision=?',
                                      (scope_id, 'M1', source_revision)).fetchall()
            current_id_set = set(current_ids)
            connection.executemany('DELETE FROM local_memory_items WHERE id=?',
                                   [(row['id'],) for row in rows if row['id'] not in current_id_set])

    @staticmethod
    def _visible(context, source_revision, layer):
        """构造统一可见性谓词：作用域、祖先继承、可信状态、版本和过期时间。"""
        clauses = []
        parameters = []
        for scope, revision in context.revisions:
            clause = '(items.scope_id=? AND items.revision<=?'
            parameters.extend((scope, revision))
            if scope != context.active_scope:
                clause += " AND items.visibility='DESCENDANTS'"
            clauses.append(clause + ')')
        where = '(' + ' OR '.join(clauses) + ") AND items.status='trusted' AND items.layer=? AND items.source_revision IN (?, '*') AND (items.expires_at IS NULL OR items.expires_at>?)"
        return where, [*parameters, layer, source_revision, time.time()]

    def retrieve(self, query, context, source_revision, layer='M1', limit=10, vector=None):
        """在 SQLite 中合并 FTS5 词法与可选向量结果，并以 RRF 风格稳定排序。

        可见性先由 SQL 过滤，避免跨项目或未晋升候选进入 Python 排名；向量
        维度不匹配的记录会被安全跳过，以保持本地回退可用。
        """
        query_terms = sorted(terms(query[:4000]))
        where, parameters = self._visible(context, source_revision, layer)
        lexical = []
        with self.connect() as connection:
            if query_terms:
                expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in query_terms)
                lexical = connection.execute(f'''SELECT items.*, bm25(local_memory_fts) AS score
                  FROM local_memory_items items JOIN local_memory_fts ON items.rowid=local_memory_fts.rowid
                  WHERE {where} AND local_memory_fts MATCH ? ORDER BY score,items.id LIMIT ?''',
                  [*parameters, expression, max(1, min(limit, 100)) * 2]).fetchall()
            dense = []
            if vector:
                visible = connection.execute(f'SELECT items.* FROM local_memory_items items WHERE {where} AND items.embedding IS NOT NULL', parameters)
                for row in visible:
                    candidate = json.loads(row['embedding'])
                    if len(candidate) != len(vector):
                        continue
                    denominator = math.sqrt(sum(value * value for value in candidate) * sum(value * value for value in vector))
                    similarity = sum(left * right for left, right in zip(candidate, vector)) / denominator if denominator else 0
                    if similarity > .35:
                        dense.append((similarity, row))
                dense.sort(key=lambda pair: (-pair[0], pair[1]['id']))
                dense = [row for similarity, row in dense[:limit * 2]]
        scores = {}
        records = {}
        for ranking in (lexical, dense):
            for rank, row in enumerate(ranking, 1):
                scores[row['id']] = scores.get(row['id'], 0) + 1 / (60 + rank)
                records[row['id']] = {key: value for key, value in dict(row).items()
                                      if key not in {'embedding', 'search_text', 'score'}}
        return [records[key] for key in sorted(scores, key=lambda key: (-scores[key], key))[:max(1, min(limit, 100))]]

    def search(self, query, *, scope_id, run_id=None, job_id=None, source_manifest='', limit=10):
        """合并 L1/L2 直接记忆与 L3 检索结果，供 memory.search 工具调用。"""
        from tracefix.knowledge.scope import ProjectContext
        context = ProjectContext(scope_id, (), ((scope_id, 2**31 - 1),), ())
        items = []
        if run_id:
            for notes in self.working_memory(scope_id, run_id).values():
                items.extend(notes)
        items.extend(self.job_memory(scope_id, job_id, source_manifest))
        query_terms = terms(query)
        items = sorted((item for item in items if query_terms & terms(json.dumps(item, ensure_ascii=False))),
                       key=lambda item: (-len(query_terms & terms(json.dumps(item, ensure_ascii=False))), item['id']))
        items.extend(self.retrieve(query, context, source_manifest, 'M3', limit))
        return items[:max(1, min(limit, 100))]

    def candidate(self, state, content, kind, revision):
        """记录 L3 候选记忆；候选状态不会自动变成可信长期知识。"""
        key = digest([state.run_id, kind, content])
        self.upsert({'id': key, 'scope_id': state.scope_id, 'layer': 'M3', 'kind': kind,
                     'revision': revision, 'source_revision': state.source_manifest,
                     'source_run_id': state.run_id, 'content': content})
        return key

    def promote(self, item_id, scope_id, *, outcome=None, approved=False, maintainer=False):
        """只有 FIX_VERIFIED 且人工确认（或维护者明确标记）才能晋升 L3。"""
        if not maintainer and not (outcome == 'FIX_VERIFIED' and approved):
            raise PermissionError('长期记忆晋升需要 FIX_VERIFIED 和人工确认，或维护者明确标记')
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM local_memory_items WHERE id=? AND scope_id=?',
                                     (item_id, scope_id)).fetchone()
            if row is None or not row['source_run_id'] or not row['source_revision']:
                raise PermissionError('记忆来源缺失或不属于当前作用域')
            connection.execute("UPDATE local_memory_items SET status='trusted' WHERE id=? AND scope_id=?", (item_id, scope_id))
