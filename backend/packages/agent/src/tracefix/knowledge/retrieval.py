"""按作用域过滤的词法与稠密检索；RRF 分数不是概率。"""
import json
import re
from pathlib import Path

import httpx

from tracefix.runtime.contracts import digest


def rrf(*rankings, k=60):
    """合并多个有序结果集，使用 reciprocal rank fusion 计算稳定排序。"""
    scores = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return sorted(scores, key=lambda x: (-scores[x], x))


class EmbeddingAdapter:
    """调用固定版本的 BGE-M3 服务，并校验返回向量维度。"""

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
    """优先按 TS/TSX 声明边界切分，其他文本使用有界行窗口作为回退。"""
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
    """维护作用域隔离的知识索引，并合并词法和向量候选。"""

    def __init__(self, store, scopes, ctx, embedding=None):
        """绑定存储、作用域解析器、当前上下文和可选嵌入适配器。"""
        self.store, self.scopes, self.ctx, self.embedding = store, scopes, ctx, embedding

    def degradation(self):
        if getattr(self.store, 'conn', None) is not None:
            return None
        return {
            'reason': 'postgres_unavailable',
            'disabled_capabilities': ['code_index', 'code_retrieval', 'repair_memory_read', 'repair_memory_write'],
            'message': '代码检索与修复记忆未启用：当前运行未连接 PostgreSQL。诊断将直接读取当前授权文件，项目文档检索仍可用。配置 PostgreSQL 后重新运行以恢复这些能力。',
        }

    async def index(self, workspace, revision):
        """按源版本写入工作区卡片，并删除该版本已经消失的卡片。"""
        if self.degradation():
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
        # Old source snapshots remain immutable and version-filtered; deleted files in the
        # SAME snapshot can only occur on an explicit overlay index refresh.
        self.store.conn.execute("DELETE FROM memory_items WHERE scope_id=%s AND layer='M1' AND source_revision=%s AND NOT (id=ANY(%s))",
                                (self.ctx.active_scope, revision, current_ids))

    async def retrieve(self, query, source_revision, layer="M1", limit=10):
        """先在 SQL 中完成可见性过滤，再合并词法和稠密排名结果。"""
        self.scopes.assert_current(self.ctx)
        if self.degradation():
            return []
        # Scope, visibility, status, content snapshot, source version filtering happen
        # in SQL BEFORE either ranking. No cross-scope candidates enter Python.
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
        """记录待审核的 M3 候选记忆，不直接提升为可信知识。"""
        if self.degradation():
            return
        key = digest([state.run_id, kind, content])
        self.store.conn.execute("""INSERT INTO memory_items
          (id,scope_id,layer,kind,logical_key,visibility,status,revision,source_revision,content,content_hash)
          VALUES (%s,%s,'M3',%s,%s,'LOCAL','candidate',%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
          (key, self.ctx.active_scope, kind, key, dict(self.ctx.revisions)[self.ctx.active_scope],
           state.source_manifest, content, digest(content)))
