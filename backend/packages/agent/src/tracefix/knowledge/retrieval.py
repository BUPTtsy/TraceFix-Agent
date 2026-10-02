"""按作用域过滤的词法与稠密检索；RRF 分数不是概率。

检索层先依据冻结的作用域和源码版本过滤候选，再合并 PostgreSQL 的词法/
向量结果；数据库不可用时切换到 MemoryLibrary 的 SQLite FTS5 回退，并在
调用方留下 degraded 事件所需的能力说明。
"""
import json
import os
import re
from pathlib import Path

import httpx
import psycopg

from tracefix.runtime.contracts import digest


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


def chunks(path: str, text: str):
    """优先按 TS/TSX 声明边界切分，其他文本使用有界行窗口作为回退。

    每个卡片携带行范围和文件 hash，供模型引用源码时回溯到授权文件及其
    具体快照；窗口上限避免单个文件吞掉整个上下文预算。
    """
    ranges = []
    if path.endswith((".ts", ".tsx")):
        from tree_sitter import Language, Parser
        import tree_sitter_typescript as ts
        parser = Parser(Language(ts.language_tsx() if path.endswith('.tsx') else ts.language_typescript()))
        tree = parser.parse(text.encode())
        ranges = [(node.start_point.row, node.end_point.row + 1) for node in tree.root_node.children]
    lines = text.splitlines()
    if not ranges:
        ranges = [(i, min(i+80, len(lines))) for i in range(0, len(lines), 80)]
    for start, end in ranges:
        for i in range(start, end, 100):
            body = '\n'.join(lines[i:min(i+100,end)])
            if body.strip():
                yield {"path": path, "start": i+1, "end": min(i+100,end), "content": body,
                       "file_hash": digest(text.encode())}


class Retriever:
    """维护作用域隔离的知识索引，并合并词法和向量候选。

    ``fallback`` 是 PostgreSQL 失败后的显式 SQLite 后端；``postgres_failed``
    一旦置位，本次运行后续操作固定走回退，避免在半失败连接上反复重试。
    """

    def __init__(self, store, scopes, ctx, embedding=None, fallback=None):
        """绑定存储、作用域解析器、当前上下文和可选嵌入适配器。"""
        self.store, self.scopes, self.ctx, self.embedding = store, scopes, ctx, embedding
        self.fallback = fallback
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
            'disabled_capabilities': ['code_index', 'code_retrieval', 'repair_memory_read', 'repair_memory_write'],
            'message': '代码检索与修复记忆未启用：当前运行未连接 PostgreSQL。诊断将直接读取当前授权文件，项目文档检索仍可用。配置 PostgreSQL 后重新运行以恢复这些能力。',
        }

    async def index(self, workspace, revision):
        """索引工作区；PostgreSQL 异常时切换 SQLite 并重试一次。"""
        try:
            return await self._index(workspace, revision)
        except psycopg.Error:
            if self.fallback is None:
                raise
            self.postgres_failed = True
            return await self._index(workspace, revision)

    async def _index(self, workspace, revision):
        """按源版本写入工作区卡片，并删除该版本已经消失的卡片。

        当前源码快照写入 M1 trusted 卡片；旧 source_revision 不删除，供
        证据回放和版本过滤继续使用。
        """
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is not None:
                await self.index_local(workspace, revision)
            return
        current_ids = []
        for path in workspace.files():
            text = workspace.read(path)
            for card in chunks(path, text):
                key = digest([self.ctx.active_scope, revision, card])
                current_ids.append(key)
                content = json.dumps(card, ensure_ascii=False)
                vector = await self.embedding.encode(content) if self.embedding else None
                self.store.conn.execute("""INSERT INTO memory_items
                  (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,content,content_hash,embedding)
                  VALUES (%s,%s,'M1','fact',%s,'LOCAL','trusted',%s,%s,%s,%s,%s::vector)
                  ON CONFLICT(id) DO NOTHING""", (key, self.ctx.active_scope, path,
                  dict(self.ctx.revisions)[self.ctx.active_scope], revision, content, digest(content), json.dumps(vector) if vector else None))
        # 旧源码快照保持不可变并按版本过滤；同一快照删除的文件只会出现在显式 overlay 索引刷新中。
        self.store.conn.execute("DELETE FROM memory_items WHERE scope_id=%s AND layer='M1' AND source_revision=%s AND NOT (id=ANY(%s))",
                                (self.ctx.active_scope, revision, current_ids))

    async def index_local(self, workspace, revision):
        """使用本地 SQLite 索引同一批 M1 卡片，保持作用域和版本字段一致。"""
        current_ids = []
        for path in workspace.files():
            for card in chunks(path, workspace.read(path)):
                key = digest([self.ctx.active_scope, revision, card])
                current_ids.append(key)
                content = json.dumps(card, ensure_ascii=False)
                vector = await self.embedding.encode(content) if self.embedding else None
                self.fallback.upsert({'id': key, 'scope_id': self.ctx.active_scope,
                    'layer': 'M1', 'kind': 'fact', 'logical_key': path, 'status': 'trusted',
                    'revision': dict(self.ctx.revisions)[self.ctx.active_scope],
                    'source_revision': revision, 'content': content, 'embedding': vector})
        self.fallback.prune_index(self.ctx.active_scope, revision, current_ids)

    async def retrieve(self, query, source_revision, layer="M1", limit=10):
        """执行检索并在数据库错误后固定切换到本地回退。"""
        try:
            return await self._retrieve(query, source_revision, layer, limit)
        except psycopg.Error:
            if self.fallback is None:
                raise
            self.postgres_failed = True
            return await self._retrieve(query, source_revision, layer, limit)

    async def _retrieve(self, query, source_revision, layer="M1", limit=10):
        """先在 SQL 中完成可见性过滤，再合并词法和稠密排名结果。

        过滤条件包含祖先作用域的 DESCENDANTS 权限、trusted 状态和
        source_revision；只有通过这些边界的记录才进入 Python 的 RRF 合并。
        """
        self.scopes.assert_current(self.ctx)
        if not self.postgres_available():
            if self.fallback is None:
                return []
            vector = await self.embedding.encode(query) if self.embedding else None
            return self.fallback.retrieve(query, self.ctx, source_revision, layer, limit, vector)
        # 作用域、可见性、状态和源码快照先在 SQL 中过滤，跨作用域候选不会进入 Python 排名。
        clauses, params = [], []
        for scope, rev in self.ctx.revisions:
            clauses.append("(scope_id=%s AND revision<=%s" + ("" if scope == self.ctx.active_scope else " AND visibility='DESCENDANTS'") + ")")
            params.extend([scope, rev])
        where = "(" + " OR ".join(clauses) + ") AND status='trusted' AND layer=%s AND source_revision IN (%s,'*')"
        params += [layer, source_revision]
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
        return [{k: v for k, v in records[i].items() if k not in {"embedding", "search"}} for i in ordered[:limit]]

    def candidate(self, state, content, kind):
        """写入候选记忆，并在 PostgreSQL 不可用时委托 SQLite。"""
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
        key = digest([state.run_id, kind, content])
        self.store.conn.execute("""INSERT INTO memory_items
          (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,content,content_hash)
          VALUES (%s,%s,'M3',%s,%s,'LOCAL','candidate',%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
          (key, self.ctx.active_scope, kind, key, dict(self.ctx.revisions)[self.ctx.active_scope],
           state.source_manifest, content, digest(content)))
