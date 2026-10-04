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
from tracefix.knowledge.experience import (
    applicable, checked_probe, conditions, cross_run_enabled, experience_record,
    public_content, source_applicability, verified_public_evidence,
)
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

    def __init__(self, path, *, cross_run=None):
        self.path = Path(path)
        self.cross_run = cross_run
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
                embedding TEXT, expires_at REAL, search_text TEXT NOT NULL,
                applicability TEXT NOT NULL DEFAULT '{}', revoked_reason TEXT,
                revoked_at REAL, evidence_refs TEXT NOT NULL DEFAULT '[]',
                patch_hash TEXT, environment_digest TEXT, test_spec_hash TEXT,
                verification_refs TEXT NOT NULL DEFAULT '[]');
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
            columns = {row[1] for row in connection.execute('PRAGMA table_info(local_memory_items)')}
            additions = {
                'applicability': "TEXT NOT NULL DEFAULT '{}'",
                'revoked_reason': 'TEXT', 'revoked_at': 'REAL',
                'evidence_refs': "TEXT NOT NULL DEFAULT '[]'",
                'patch_hash': 'TEXT', 'environment_digest': 'TEXT',
                'test_spec_hash': 'TEXT', 'verification_refs': "TEXT NOT NULL DEFAULT '[]'",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f'ALTER TABLE local_memory_items ADD COLUMN {name} {definition}')

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

    def note(self, scope_id, run_id, note, *, allowed_evidence_refs=(), source_manifest='',
             phase=None, patch_hash=None, page_generation=None):
        """写入 L1 工作记忆，并拒绝超出 Run 证据集合的引用。"""
        note = note if isinstance(note, MemoryNote) else MemoryNote.model_validate(note)
        if not set(note.evidence_refs) <= set(allowed_evidence_refs):
            raise PermissionError('工作记忆包含未授权的证据引用')
        record = redact(note.model_dump(mode='json'))
        if not public_content(record):
            raise PermissionError('held-out 内容不能写入工作记忆')
        record['metadata'].update({key: value for key, value in {
            'phase': str(phase) if phase is not None else None, 'patch_hash': patch_hash,
            'page_generation': page_generation}.items() if value is not None})
        record.update(id=note.id or new_id('note'), scope_id=scope_id, run_id=run_id,
                      source_run_id=run_id, source_manifest=source_manifest, updated_at=time.time())
        with self.connect() as connection:
            connection.execute('INSERT OR REPLACE INTO run_memory VALUES (?,?,?,?,?)',
                               (record['id'], scope_id, run_id, source_manifest,
                                json.dumps(record, ensure_ascii=False)))
        return record

    def working_memory(self, scope_id, run_id, *, query='', phase=None, source_manifest=None,
                       patch_hash=None, page_generation=None, limit=12):
        """按记忆类型恢复单个 Run 的 L1 分组，供上下文组装使用。"""
        with self.connect() as connection:
            notes = [json.loads(row['data']) for row in connection.execute(
                'SELECT data FROM run_memory WHERE scope_id=? AND run_id=? ORDER BY rowid',
                (scope_id, run_id))]
        selected = []
        query_terms = terms(query)
        for note in notes:
            metadata = note.get('metadata') or {}
            if metadata.get('status') in {'revoked', 'superseded'}:
                continue
            if metadata.get('expires_at') and metadata['expires_at'] <= time.time():
                continue
            if phase and metadata.get('phases') and str(phase) not in metadata['phases']:
                continue
            if page_generation is not None and metadata.get('page_generation') not in {None, page_generation}:
                continue
            stale = ((source_manifest is not None and note.get('source_manifest') not in {'', source_manifest})
                     or (patch_hash is not None and metadata.get('patch_hash') not in {None, patch_hash}))
            if stale and note['kind'] in {'excluded', 'hypothesis'}:
                note = {**note, 'kind': 'finding', 'applicability': 'stale',
                        'requires_probe': True, 'previous_kind': note['kind']}
            score = len(query_terms & terms(note['text']))
            priority = {'todo': 3, 'finding': 2, 'excluded': 2, 'hypothesis': 1, 'progress': 0}[note['kind']]
            selected.append((score, priority, note.get('updated_at', 0), note))
        selected.sort(key=lambda value: (-value[0], -value[1], -value[2], value[3]['id']))
        notes = [value[3] for value in selected[:max(0, limit)]]
        return {kind: [note for note in notes if note['kind'] == kind]
                for kind in ('hypothesis', 'excluded', 'finding', 'todo', 'progress')}

    def save_job_memory(self, scope_id, job_id, content, *, source_run_id, source_manifest,
                        allowed_evidence_refs=(), cross_run=None):
        """写入 L2 Job 记忆，并把来源 Run 和源码 manifest 一起固定下来。"""
        if not cross_run_enabled(self.cross_run) or cross_run is False:
            return None
        if not public_content(content):
            raise PermissionError('held-out 内容不能写入跨 Run 记忆')
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

    def job_memory(self, scope_id, job_id, source_manifest, *, cross_run=None, query='',
                   phase=None, limit=6, include_stale=False):
        """只读取同一作用域、Job 和源码 manifest 的 L2 记忆。"""
        if not job_id or not cross_run_enabled(self.cross_run) or cross_run is False:
            return []
        with self.connect() as connection:
            rows = connection.execute('SELECT data FROM job_memory WHERE scope_id=? AND job_id=? ORDER BY rowid',
                                      (scope_id, job_id))
            records = [json.loads(row['data']) for row in rows]
        selected = []
        for record in records:
            if record.get('status') in {'revoked', 'superseded'}:
                continue
            if record.get('expires_at') and record['expires_at'] <= time.time():
                continue
            if phase and record.get('phases') and str(phase) not in record['phases']:
                continue
            exact = record['source_manifest'] == source_manifest
            compatible = (record.get('compatibility') or {}).get(source_manifest)
            if not exact and not compatible and not include_stale:
                continue
            record = {**record, 'applicability': 'exact' if exact else 'compatible' if compatible else 'stale',
                      'requires_probe': not exact and not compatible}
            selected.append(record)
        selected.sort(key=lambda item: (-len(terms(query) & terms(json.dumps(item, ensure_ascii=False))), item['id']))
        return selected[:max(0, limit)]

    def upsert(self, record):
        """插入本地索引项并维护 FTS5 词法字段；结构化内容经脱敏后序列化。"""
        record = dict(record)
        if record['layer'] in {'M3', 'L2', 'L3'}:
            if not cross_run_enabled(self.cross_run):
                return
            if not public_content(record):
                raise PermissionError('held-out 内容不能写入跨 Run 记忆')
        content = record['content']
        content = redact(content)
        content = json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content
        search_text = ' '.join(sorted(terms(content)))
        values = (record['id'], record['scope_id'], record['layer'], record['kind'],
                  record.get('logical_key', record['id']), record.get('visibility', 'LOCAL'),
                  record.get('status', 'candidate'), record['revision'], record['source_revision'],
                  record.get('source_run_id'), content, digest(content),
                  json.dumps(record['embedding']) if record.get('embedding') else None,
                  record.get('expires_at'), search_text,
                  json.dumps(record.get('applicability') or {}, ensure_ascii=False),
                  record.get('revoked_reason'), record.get('revoked_at'),
                  json.dumps(record.get('evidence_refs') or [], ensure_ascii=False),
                  record.get('patch_hash'), record.get('environment_digest'),
                  record.get('test_spec_hash'),
                  json.dumps(record.get('verification_refs') or [], ensure_ascii=False))
        with self.connect() as connection:
            connection.execute('''INSERT OR REPLACE INTO local_memory_items
              (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,
               source_run_id,content,content_hash,embedding,expires_at,search_text,
               applicability,revoked_reason,revoked_at,evidence_refs,patch_hash,
               environment_digest,test_spec_hash,verification_refs)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', values)

    def prune_index(self, scope_id, source_revision, current_ids):
        """删除当前源码版本中已不存在的卡片，保留旧版本的不可变快照。"""
        with self.connect() as connection:
            rows = connection.execute('SELECT id FROM local_memory_items WHERE scope_id=? AND layer=? AND source_revision=?',
                                      (scope_id, 'M1', source_revision)).fetchall()
            current_id_set = set(current_ids)
            connection.executemany('DELETE FROM local_memory_items WHERE id=?',
                                   [(row['id'],) for row in rows if row['id'] not in current_id_set])

    @staticmethod
    def _visible(context, source_revision, layer, *, cross_run=True, allow_compatible=False):
        """构造统一可见性谓词：作用域、祖先继承、可信状态、版本和过期时间。"""
        if not cross_run and layer in {'M3', 'L2', 'L3'}:
            return '0', []
        clauses = []
        parameters = []
        for scope, revision in context.revisions:
            clause = '(items.scope_id=? AND items.revision<=?'
            parameters.extend((scope, revision))
            if scope != context.active_scope:
                clause += " AND items.visibility='DESCENDANTS'"
            clauses.append(clause + ')')
        source_filter = ('1' if allow_compatible else 'items.source_revision=?') if layer == 'M3' else "items.source_revision IN (?, '*')"
        where = '(' + ' OR '.join(clauses) + ") AND items.status='trusted' AND items.layer=? AND " + source_filter + " AND (items.expires_at IS NULL OR items.expires_at>?)"
        source_parameters = [] if layer == 'M3' and allow_compatible else [source_revision]
        return where, [*parameters, layer, *source_parameters, time.time()]

    def retrieve(self, query, context, source_revision, layer='M1', limit=10, vector=None,
                 *, cross_run=True, allow_compatible=False, state=None, phase=None,
                 artifact_exists=None, artifact_read=None):
        """在 SQLite 中合并 FTS5 词法与可选向量结果，并以 RRF 风格稳定排序。

        可见性先由 SQL 过滤，避免跨项目或未晋升候选进入 Python 排名；向量
        维度不匹配的记录会被安全跳过，以保持本地回退可用。
        """
        if layer in {'M3', 'L2', 'L3'} and (not cross_run_enabled(self.cross_run) or not cross_run):
            return []
        query_terms = sorted(terms(query[:4000]))
        where, parameters = self._visible(context, source_revision, layer,
                                          cross_run=cross_run, allow_compatible=allow_compatible)
        lexical = []
        with self.connect() as connection:
            if layer == 'M3':
                metadata = connection.execute(
                    f'SELECT items.id,items.source_revision,items.applicability FROM local_memory_items items WHERE {where}',
                    parameters).fetchall()
                eligible = [row['id'] for row in metadata
                            if applicable(dict(row), state, phase)
                            and source_applicability(dict(row), source_revision, state,
                                artifact_exists=artifact_exists, artifact_read=artifact_read) != 'stale']
                if not eligible:
                    return []
                where += ' AND items.id IN (' + ','.join('?' for _ in eligible) + ')'
                parameters += eligible
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
        selected = [records[key] for key in sorted(scores, key=lambda key: (-scores[key], key))
                    ][:max(1, min(limit, 100))]
        if layer == 'M3':
            for record in selected:
                record['applicability_status'] = source_applicability(record, source_revision, state,
                    artifact_exists=artifact_exists, artifact_read=artifact_read)
                record['why_retrieved'] = '当前源码和条件匹配的已验证经验'
        return selected

    def search(self, query, *, scope_id, run_id=None, job_id=None, source_manifest='', limit=10,
               cross_run=True, allow_compatible=False):
        """合并 L1/L2 直接记忆与 L3 检索结果，供 memory.search 工具调用。"""
        from tracefix.knowledge.scope import ProjectContext
        context = ProjectContext(scope_id, (), ((scope_id, 2**31 - 1),), ())
        cross_run = cross_run_enabled(self.cross_run) and cross_run
        items = []
        if run_id:
            for notes in self.working_memory(scope_id, run_id).values():
                items.extend(notes)
        if cross_run:
            items.extend(self.job_memory(scope_id, job_id, source_manifest))
        query_terms = terms(query)
        items = sorted((item for item in items if query_terms & terms(json.dumps(item, ensure_ascii=False))),
                       key=lambda item: (-len(query_terms & terms(json.dumps(item, ensure_ascii=False))), item['id']))
        if cross_run:
            items.extend(self.retrieve(query, context, source_manifest, 'M3', limit,
                                       cross_run=True, allow_compatible=allow_compatible))
        return items[:max(1, min(limit, 100))]

    def candidate(self, state, content, kind, revision, *, evidence_refs=(), patch_hash='',
                  environment_digest='', test_spec_hash='', applicability=None,
                  verification_refs=(), expires_at=None):
        """记录 L3 候选记忆；候选状态不会自动变成可信长期知识。"""
        if not cross_run_enabled(self.cross_run):
            return None
        record = experience_record(state, content, kind, revision, expires_at=expires_at)
        if evidence_refs:
            record['evidence_refs'] = list(dict.fromkeys(evidence_refs))
        for field, value in (('patch_hash', patch_hash), ('environment_digest', environment_digest),
                             ('test_spec_hash', test_spec_hash)):
            if value:
                record[field] = value
        if applicability:
            record['applicability'].update(applicability)
        if verification_refs:
            record['verification_refs'] = list(dict.fromkeys(verification_refs))
        with self.connect() as connection:
            existing = connection.execute('SELECT id FROM local_memory_items WHERE id=?',
                                          (record['id'],)).fetchone()
        if existing:
            return record['id']
        self.upsert(record)
        return record['id']

    def promote(self, item_id, scope_id, *, outcome=None, approved=False, maintainer=False,
                verification_refs=(), public_verification=True, state=None,
                artifact_exists=None, artifact_read=None, artifact_read_bytes=None):
        """只有 FIX_VERIFIED 且人工确认（或维护者明确标记）才能晋升 L3。"""
        if not cross_run_enabled(self.cross_run):
            return None
        if state is None or state.scope_id != scope_id or not public_verification:
            raise PermissionError('晋升需要当前作用域的公开开发验证状态')
        verified_refs = verified_public_evidence(state, artifact_exists, artifact_read, artifact_read_bytes)
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM local_memory_items WHERE id=? AND scope_id=?',
                                     (item_id, scope_id)).fetchone()
            if row is None or not row['source_run_id'] or not row['source_revision']:
                raise PermissionError('记忆来源缺失或不属于当前作用域')
            if row['status'] != 'candidate' or row['expires_at'] and row['expires_at'] <= time.time():
                raise PermissionError('撤回或过期经验不能晋升')
            for field, state_field in (('source_revision', 'source_manifest'), ('source_run_id', 'run_id'),
                                       ('patch_hash', 'patch_hash'), ('environment_digest', 'environment_digest'),
                                       ('test_spec_hash', 'test_spec_hash')):
                if row[field] != getattr(state, state_field, None):
                    raise PermissionError('经验与当前 patch/env/spec 不匹配')
            stored_refs = set(json.loads(row['verification_refs'] or '[]'))
            if not set(verified_refs) <= stored_refs:
                raise PermissionError('晋升引用未绑定公开验证 artifact')
            connection.execute("UPDATE local_memory_items SET status='trusted' WHERE id=? AND scope_id=?", (item_id, scope_id))
        return item_id

    def revoke(self, item_id, scope_id, reason):
        """撤回经验但保留历史内容；后续过滤不会激活 revoked 条目。"""
        reason = str(reason).strip()
        if not reason:
            raise ValueError('撤回必须记录原因')
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE local_memory_items SET status='revoked', revoked_reason=?, revoked_at=? "
                "WHERE id=? AND scope_id=?", (reason, time.time(), item_id, scope_id)).rowcount
        if not updated:
            raise KeyError('记忆条目不存在')

    def clues(self, query, context, source_manifest, *, limit=3):
        if not cross_run_enabled(self.cross_run):
            return []
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM local_memory_items WHERE scope_id=? AND layer='M3' "
                "AND status IN ('candidate','trusted') AND (expires_at IS NULL OR expires_at>?)",
                (context.active_scope, time.time())).fetchall()
        query_terms = terms(query)
        records = [dict(row) for row in rows if query_terms & terms(row['content'])
                   and row['revision'] <= dict(context.revisions)[context.active_scope]]
        records.sort(key=lambda row: (-len(query_terms & terms(row['content'])), row['id']))
        return [{**{key: value for key, value in record.items() if key not in {'embedding', 'search_text'}},
                 'applicability_status': 'stale' if record['source_revision'] != source_manifest else 'candidate',
                 'requires_probe': True, 'actionable': False} for record in records[:max(0, limit)]]

    def mark_compatible(self, item_id, state, probe_ref, *, artifact_exists, artifact_read):
        if not cross_run_enabled(self.cross_run):
            return None
        probe = checked_probe(state, probe_ref, artifact_exists, artifact_read)
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM local_memory_items WHERE id=? AND scope_id=?',
                                     (item_id, state.scope_id)).fetchone()
            if row is None or row['status'] != 'trusted' or row['expires_at'] and row['expires_at'] <= time.time():
                raise PermissionError('仅未撤回且未过期的已验证经验可激活兼容版本')
            declared = conditions(dict(row))
            declared.setdefault('compatibility', {})[state.source_manifest] = probe
            connection.execute('UPDATE local_memory_items SET applicability=? WHERE id=? AND scope_id=?',
                               (json.dumps(declared, ensure_ascii=False), item_id, state.scope_id))
        return item_id
