"""按作用域过滤的跨 Run 经验检索与候选写入；RRF 分数不是概率。

检索层先依据冻结的作用域和源码版本过滤 M3 经验候选，再合并 PostgreSQL 的
词法/向量结果；数据库不可用时切换到 MemoryLibrary 的 SQLite FTS5 回退，并在
调用方留下 degraded 事件所需的能力说明。代码结构理解由 CodeGraph 承担，本
模块不再对源码切块建索引。
"""
import json
import os
import time

import httpx
import psycopg

from tracefix.runtime.contracts import digest
from tracefix.knowledge.experience import (
    applicable, checked_probe, conditions, cross_run_enabled, experience_record,
    public_content, source_applicability, verified_public_evidence,
)


def rrf(*rankings, k=60):
    """合并多个有序结果集，使用 reciprocal rank fusion 计算稳定排序。

    RRF 只表达候选相对顺序，不是置信概率；同分时按 id 排序确保回放稳定。
    """
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return sorted(scores, key=lambda x: (-scores[x], x))


class EmbeddingAdapter:
    """调用固定版本的 BGE-M3 服务，并校验返回向量维度。

    固定 revision 使向量语义与索引快照可追踪；响应维度错误会直接失败，
    避免把不兼容向量写入检索索引。
    """

    def __init__(self, base_url: str, revision: str, key: str = ""):
        """保存服务地址和模型版本；拒绝可变的 latest/main 版本。"""
        if not revision or revision in {"main", "latest"}:
            raise ValueError("请固定 BGE-M3 的版本号")
        self.url, self.revision, self.key = base_url.rstrip('/'), revision, key

    async def encode(self, text: str):
        """把文本编码成 1024 维向量，并传播 HTTP 或响应格式错误。"""
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(self.url + "/embeddings", headers={"Authorization": "Bearer " + self.key},
                                  json={"model": "BAAI/bge-m3", "input": text})
            r.raise_for_status()
            vector = r.json()["data"][0]["embedding"]
            if len(vector) != 1024:
                raise ValueError("BGE-M3 稠密向量维度不匹配")
            return vector


class Retriever:
    """维护作用域隔离的跨 Run 经验检索，并合并词法和向量候选。

    ``fallback`` 是 PostgreSQL 失败后的显式 SQLite 后端；``postgres_failed``
    一旦置位，本次运行后续操作固定走回退，避免在半失败连接上反复重试。
    """

    def __init__(self, store, scopes, ctx, embedding=None, fallback=None, *, cross_run=None):
        """绑定存储、作用域解析器、当前上下文和可选嵌入适配器。"""
        self.store, self.scopes, self.ctx, self.embedding = store, scopes, ctx, embedding
        self.fallback = fallback
        self.cross_run = cross_run
        if self.fallback is None and os.getenv('TRACEFIX_MEMORY_DB'):
            from tracefix.knowledge.memory import MemoryLibrary
            self.fallback = MemoryLibrary(os.environ['TRACEFIX_MEMORY_DB'])
        self.postgres_failed = False

    def postgres_available(self):
        """判断当前连接是否可用；异常后保持本次 Run 的回退选择稳定。"""
        return getattr(self.store, 'conn', None) is not None and not self.postgres_failed

    def degradation(self):
        """返回可展示的降级原因和禁用能力，不把回退当作完全等价的后端。"""
        if self.postgres_available():
            return None
        if self.fallback is not None:
            return {
                'reason': 'sqlite_fallback', 'backend': 'sqlite',
                'disabled_capabilities': [] if self.embedding else ['vector_retrieval'],
                'message': '项目记忆已回退到 SQLite FTS5 本地检索。' +
                           ('向量检索仍可用。' if self.embedding else '未配置嵌入服务，向量检索未启用。'),
            }
        return {
            'reason': 'postgres_unavailable',
            'disabled_capabilities': ['repair_memory_read', 'repair_memory_write'],
            'message': '修复记忆的读写未启用：当前运行未连接 PostgreSQL。配置 PostgreSQL 后重新运行以恢复该能力。',
        }

    async def retrieve(self, query, source_revision, layer="M3", limit=10, *, state=None, phase=None,
                       allow_compatible=False, artifact_exists=None, artifact_read=None):
        """执行检索并在数据库错误后固定切换到本地回退。"""
        if layer in {'M3', 'L2', 'L3'} and not cross_run_enabled(self.cross_run):
            return []
        try:
            return await self._retrieve(query, source_revision, layer, limit, state=state, phase=phase,
                                        allow_compatible=allow_compatible,
                                        artifact_exists=artifact_exists, artifact_read=artifact_read)
        except psycopg.Error:
            if self.fallback is None:
                raise
            self.postgres_failed = True
            return await self._retrieve(query, source_revision, layer, limit, state=state, phase=phase,
                                        allow_compatible=allow_compatible,
                                        artifact_exists=artifact_exists, artifact_read=artifact_read)

    async def _retrieve(self, query, source_revision, layer="M3", limit=10, *, state=None, phase=None,
                        allow_compatible=False, artifact_exists=None, artifact_read=None):
        """先在 SQL 中完成可见性过滤，再合并词法和稠密排名结果。

        过滤条件包含祖先作用域的 DESCENDANTS 权限、trusted 状态和
        source_revision；只有通过这些边界的记录才进入 Python 的 RRF 合并。
        """
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is None:
                return []
            vector = await self.embedding.encode(query) if self.embedding else None
            return self.fallback.retrieve(query, self.ctx, source_revision, layer, limit, vector,
                cross_run=cross_run_enabled(self.cross_run), state=state, phase=phase,
                allow_compatible=allow_compatible, artifact_exists=artifact_exists, artifact_read=artifact_read)
        # 作用域、可见性、状态和源码快照先在 SQL 中过滤，跨作用域候选不会进入 Python 排名。
        clauses, params = [], []
        for scope, rev in self.ctx.revisions:
            clauses.append("(scope_id=%s AND revision<=%s" + ("" if scope == self.ctx.active_scope else " AND visibility='DESCENDANTS'") + ")")
            params.extend([scope, rev])
        source_filter = ('TRUE' if allow_compatible else 'source_revision=%s') if layer == 'M3' else "source_revision IN (%s,'*')"
        where = "(" + " OR ".join(clauses) + ") AND status='trusted' AND layer=%s AND " + source_filter
        where += ' AND (expires_at IS NULL OR expires_at>%s)'
        params += [layer, *([] if layer == 'M3' and allow_compatible else [source_revision]), time.time()]
        if layer == 'M3':
            metadata = self.store.conn.execute(
                f'SELECT id,source_revision,applicability FROM memory_items WHERE {where}', params).fetchall()
            eligible = [row['id'] for row in metadata if applicable(row, state, phase)
                        and source_applicability(row, source_revision, state,
                            artifact_exists=artifact_exists, artifact_read=artifact_read) != 'stale']
            if not eligible:
                return []
            where += ' AND id=ANY(%s)'
            params += [eligible]
        lexical = self.store.conn.execute(f"""SELECT *, ts_rank(search, plainto_tsquery('simple', %s)) AS score
            FROM memory_items WHERE {where} AND search @@ plainto_tsquery('simple', %s)
            ORDER BY score DESC, id LIMIT %s""", [query, *params, query, limit * 2]).fetchall()
        dense = []
        if self.embedding:
            v = json.dumps(await self.embedding.encode(query))
            dense = self.store.conn.execute(f"""WITH visible AS MATERIALIZED
              (SELECT * FROM memory_items WHERE {where} AND embedding IS NOT NULL)
              SELECT *, embedding <=> %s::vector AS distance FROM visible
              WHERE embedding <=> %s::vector < 0.65 ORDER BY distance, id LIMIT %s""",
              [*params, v, v, limit * 2]).fetchall()
        records = {r["id"]: r for r in [*lexical, *dense]}
        ordered = rrf([r['id'] for r in lexical], [r['id'] for r in dense])
        selected = [{k: v for k, v in records[i].items() if k not in {"embedding", "search"}}
                    for i in ordered[:limit]]
        if layer == 'M3':
            for record in selected:
                record['applicability_status'] = source_applicability(record, source_revision, state,
                    artifact_exists=artifact_exists, artifact_read=artifact_read)
                record['why_retrieved'] = '当前源码和条件匹配的已验证经验'
        return selected

    def candidate(self, state, content, kind):
        """写入候选记忆，并在 PostgreSQL 不可用时委托 SQLite。"""
        if not cross_run_enabled(self.cross_run):
            return None
        if not public_content(content):
            raise PermissionError('最终 Oracle 或 held-out 内容不能写入经验')
        try:
            return self._candidate(state, content, kind)
        except psycopg.Error:
            if self.fallback is None:
                raise
            self.postgres_failed = True
            return self._candidate(state, content, kind)

    def _candidate(self, state, content, kind):
        """记录待审核的 M3 候选记忆，不直接提升为可信知识。

        候选保留源码 manifest 和作用域来源，本地后端还保存 source_run_id；
        后续只有通过人工晋升门槛才能参与 trusted 记忆检索。
        """
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is not None:
                return self.fallback.candidate(state, content, kind,
                                              dict(self.ctx.revisions)[self.ctx.active_scope])
            return
        record = experience_record(state, content, kind, dict(self.ctx.revisions)[self.ctx.active_scope])
        key = record['id']
        content = json.dumps(record['content'], ensure_ascii=False)
        self.store.conn.execute("""INSERT INTO memory_items
          (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,content,content_hash,
           source_run_id,patch_hash,environment_digest,test_spec_hash,applicability,evidence_refs,verification_refs)
          VALUES (%s,%s,'M3',%s,%s,'LOCAL','candidate',%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
          ON CONFLICT DO NOTHING""",
          (key, self.ctx.active_scope, kind, key, dict(self.ctx.revisions)[self.ctx.active_scope],
           state.source_manifest, content, digest(content), state.run_id, record['patch_hash'],
           record['environment_digest'], record['test_spec_hash'], json.dumps(record['applicability']),
           json.dumps(record['evidence_refs']), json.dumps(record['verification_refs'])))
        return key

    def promote(self, item_id, state, *, artifact_exists, artifact_read, artifact_read_bytes):
        """公开开发证据重读通过后晋升；PostgreSQL 和 SQLite 使用同一验证门。"""
        if not cross_run_enabled(self.cross_run):
            return None
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is None:
                return None
            return self.fallback.promote(item_id, self.ctx.active_scope, state=state,
                artifact_exists=artifact_exists, artifact_read=artifact_read,
                artifact_read_bytes=artifact_read_bytes)
        refs = verified_public_evidence(state, artifact_exists, artifact_read, artifact_read_bytes)
        row = self.store.conn.execute(
            'SELECT * FROM memory_items WHERE id=%s AND scope_id=%s',
            (item_id, self.ctx.active_scope)).fetchone()
        if row is None or row['status'] != 'candidate' or row['expires_at'] and row['expires_at'] <= time.time():
            raise PermissionError('经验缺失、撤回或过期')
        for field, state_field in (('source_revision', 'source_manifest'), ('source_run_id', 'run_id'),
                                   ('patch_hash', 'patch_hash'), ('environment_digest', 'environment_digest'),
                                   ('test_spec_hash', 'test_spec_hash')):
            if row[field] != getattr(state, state_field, None):
                raise PermissionError('经验与当前 patch/env/spec 不匹配')
        stored_refs = row['verification_refs']
        stored_refs = json.loads(stored_refs) if isinstance(stored_refs, str) else stored_refs
        if not set(refs) <= set(stored_refs):
            raise PermissionError('晋升引用未绑定公开验证 artifact')
        self.store.conn.execute("UPDATE memory_items SET status='trusted' WHERE id=%s AND scope_id=%s",
                                (item_id, self.ctx.active_scope))
        return item_id

    def revoke(self, item_id, reason):
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is not None:
                return self.fallback.revoke(item_id, self.ctx.active_scope, reason)
            return None
        if not str(reason).strip():
            raise ValueError('撤回必须记录原因')
        self.store.conn.execute(
            "UPDATE memory_items SET status='revoked',revoked_reason=%s,revoked_at=%s WHERE id=%s AND scope_id=%s",
            (str(reason), time.time(), item_id, self.ctx.active_scope))

    def clues(self, query, source_manifest, *, limit=3):
        if not cross_run_enabled(self.cross_run):
            return []
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            return self.fallback.clues(query, self.ctx, source_manifest, limit=limit) if self.fallback else []
        records = self.store.conn.execute(
            "SELECT * FROM memory_items WHERE scope_id=%s AND layer='M3' AND status IN ('candidate','trusted') "
            "AND revision<=%s AND (expires_at IS NULL OR expires_at>%s) "
            "AND search @@ plainto_tsquery('simple',%s) ORDER BY id LIMIT %s",
            (self.ctx.active_scope, dict(self.ctx.revisions)[self.ctx.active_scope], time.time(), query, limit)).fetchall()
        return [{**{key: value for key, value in record.items() if key not in {'embedding', 'search'}},
                 'applicability_status': 'stale' if record['source_revision'] != source_manifest else 'candidate',
                 'requires_probe': True, 'actionable': False} for record in records]

    def mark_compatible(self, item_id, state, probe_ref, *, artifact_exists, artifact_read):
        if not cross_run_enabled(self.cross_run):
            return None
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            return self.fallback.mark_compatible(item_id, state, probe_ref,
                artifact_exists=artifact_exists, artifact_read=artifact_read) if self.fallback else None
        probe = checked_probe(state, probe_ref, artifact_exists, artifact_read)
        row = self.store.conn.execute(
            'SELECT * FROM memory_items WHERE id=%s AND scope_id=%s', (item_id, self.ctx.active_scope)).fetchone()
        if row is None or row['status'] != 'trusted' or row['expires_at'] and row['expires_at'] <= time.time():
            raise PermissionError('仅未撤回且未过期的已验证经验可激活兼容版本')
        declared = conditions(row)
        declared.setdefault('compatibility', {})[state.source_manifest] = probe
        self.store.conn.execute('UPDATE memory_items SET applicability=%s::jsonb WHERE id=%s AND scope_id=%s',
                                (json.dumps(declared), item_id, self.ctx.active_scope))
        return item_id
