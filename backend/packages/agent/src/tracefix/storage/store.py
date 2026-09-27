from __future__ import annotations

import copy
import json
import time
from contextlib import contextmanager
from pathlib import Path

from tracefix.runtime.contracts import RunState, digest, new_id
from tracefix.storage.artifacts import sanitize, redact


class UnknownOperation(RuntimeError):
    """Intent exists without receipt: never blindly re-execute a side effect."""


class MemoryStore:
    """Explicit CI smoke store; never selected by a real run or resume."""
    def __init__(self):
        self.runs, self.events, self.operations, self.approvals, self.projects = {}, {}, {}, {}, {}
        self.locked = set()

    def save(self, state: RunState):
        self.runs[state.run_id] = state.model_dump(mode="json")

    def load(self, run_id: str, scope: str) -> RunState:
        s = RunState(**self.runs[run_id])
        if s.scope_id != scope:
            raise PermissionError("该 Run 属于另一个项目")
        return s

    def event(self, state, kind, payload=None):
        es = self.events.setdefault(state.run_id, [])
        event = dict(run_id=state.run_id, seq=len(es)+1, scope_id=state.scope_id,
                     phase=str(state.phase), type=kind, revision=state.revision, at=time.time(),
                     payload=redact(payload or {}))
        es.append(event)
        return event

    def trace(self, run_id, scope, after=0):
        self.load(run_id, scope)
        return copy.deepcopy([e for e in self.events.get(run_id, []) if e["seq"] > after])

    def begin(self, state, op_id, intent, *, notify=None):
        old = self.operations.get(op_id)
        if old:
            if old["intent_hash"] != digest(intent) or old["scope_id"] != state.scope_id:
                raise PermissionError("操作身份不匹配")
            if old["status"] != "DONE":
                raise UnknownOperation(op_id)
            return old["receipt"]
        self.operations[op_id] = dict(intent_hash=digest(intent), scope_id=state.scope_id,
                                     run_id=state.run_id, status="STARTED", receipt=None)
        event = self.event(state, "tool.started", {"operation_id": op_id, "intent": intent})
        if notify:
            notify(event)
        return None

    def finish(self, state, op_id, receipt, *, notify=None):
        self.operations[op_id].update(status="DONE", receipt=copy.deepcopy(receipt))
        event = self.event(state, "tool.completed", {"operation_id": op_id, "receipt": receipt})
        if notify:
            notify(event)

    def approval(self, state, action):
        key = "req_" + digest([state.run_id, state.continuation_count, state.patch_hash, action])[:24]
        self.approvals.setdefault(key, dict(id=key, run_id=state.run_id, scope_id=state.scope_id,
            action_hash=digest(action), patch_hash=state.patch_hash, expires_at=time.time()+3600,
            decision=None, consumed=False))
        return key

    def decide_approval(self, key, state, decision):
        a = self.approvals[key]
        self._check_approval(a, state)
        if a["decision"] is not None:
            raise PermissionError("审批已被决定过")
        a["decision"] = decision
        self.event(state, "approval", {"id": key, "decision": decision})

    def _check_approval(self, a, state):
        if (a["run_id"], a["scope_id"], a["patch_hash"]) != (state.run_id, state.scope_id, state.patch_hash):
            raise PermissionError("审批绑定不匹配")
        if a["expires_at"] < time.time() or a["consumed"]:
            raise PermissionError("审批已过期或已被使用")

    def consume_approval(self, key, state, action):
        a = self.approvals[key]
        self._check_approval(a, state)
        if a["action_hash"] != digest(action) or a["decision"] is None:
            raise PermissionError("审批缺失")
        a["consumed"] = True
        return a["decision"] == "approve"

    @contextmanager
    def writer(self, run_id):
        if run_id in self.locked:
            raise RuntimeError("该 Run 已有写入者")
        self.locked.add(run_id)
        try:
            yield
        finally:
            self.locked.remove(run_id)


class PostgresStore(MemoryStore):
    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def setup(self):
        self.conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding='utf-8'))

    def close(self):
        self.conn.close()

    def save(self, state):
        self.conn.execute("INSERT INTO runs VALUES (%s,%s,%s::jsonb) ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state",
                          (state.run_id, state.scope_id, state.model_dump_json()))

    def load(self, run_id, scope):
        row = self.conn.execute("SELECT state FROM runs WHERE id=%s AND scope_id=%s", (run_id, scope)).fetchone()
        if not row:
            raise PermissionError("在当前项目中未找到该 Run")
        return RunState(**row["state"])

    def event(self, state, kind, payload=None):
        with self.conn.transaction():
            self.conn.execute("SELECT id FROM runs WHERE id=%s FOR UPDATE", (state.run_id,))
            seq = self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 AS n FROM events WHERE run_id=%s", (state.run_id,)).fetchone()["n"]
            e = dict(run_id=state.run_id, seq=seq, scope_id=state.scope_id, phase=str(state.phase),
                     type=kind, revision=state.revision, at=time.time(), payload=payload or {})
            raw = json.dumps(redact(e), ensure_ascii=False, default=str)
            self.conn.execute("INSERT INTO events VALUES (%s,%s,%s::jsonb)", (state.run_id, seq, raw))
            return json.loads(raw)

    def trace(self, run_id, scope, after=0):
        self.load(run_id, scope)
        events = []
        previous_seq = after
        required = {"run_id", "seq", "scope_id", "phase", "type", "revision", "at", "payload"}
        for row in self.conn.execute(
            "SELECT payload FROM events WHERE run_id=%s AND seq>%s ORDER BY seq", (run_id, after)):
            event = row["payload"]
            if not isinstance(event, dict) or not required <= event.keys():
                raise ValueError("存储事件不符合轨迹契约")
            if event["run_id"] != run_id or event["scope_id"] != scope:
                raise ValueError("存储事件不属于请求的作用域")
            if not isinstance(event["seq"], int) or event["seq"] <= previous_seq:
                raise ValueError("存储事件序号没有严格递增")
            if not isinstance(event["payload"], dict):
                raise ValueError("存储事件 payload 必须是对象")
            events.append(copy.deepcopy(event))
            previous_seq = event["seq"]
        return events

    def begin(self, state, op_id, intent, *, notify=None):
        with self.conn.transaction():
            old = self.conn.execute("SELECT * FROM operations WHERE id=%s FOR UPDATE", (op_id,)).fetchone()
            if old:
                if old["intent_hash"] != digest(intent) or old["scope_id"] != state.scope_id:
                    raise PermissionError("操作身份不匹配")
                if old["status"] != "DONE":
                    raise UnknownOperation(op_id)
                return old["receipt"]
            self.conn.execute("INSERT INTO operations VALUES (%s,%s,%s,%s,'STARTED',NULL)",
                              (op_id, state.run_id, state.scope_id, digest(intent)))
            event = self.event(state, "tool.started", {"operation_id": op_id, "intent": intent})
        if notify:
            notify(event)
        return None

    def finish(self, state, op_id, receipt, *, notify=None):
        with self.conn.transaction():
            self.conn.execute("UPDATE operations SET status='DONE',receipt=%s::jsonb WHERE id=%s AND scope_id=%s",
                              (json.dumps(receipt), op_id, state.scope_id))
            event = self.event(state, "tool.completed", {"operation_id": op_id, "receipt": receipt})
        if notify:
            notify(event)

    def approval(self, state, action):
        key = "req_" + digest([state.run_id, state.continuation_count, state.patch_hash, action])[:24]
        self.conn.execute("INSERT INTO approvals(id,run_id,scope_id,action_hash,patch_hash,expires_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (key, state.run_id, state.scope_id, digest(action), state.patch_hash, time.time()+3600))
        return key

    def decide_approval(self, key, state, decision):
        with self.conn.transaction():
            a = self.conn.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (key,)).fetchone()
            if not a:
                raise PermissionError("未知的审批")
            self._check_approval(a, state)
            if a["decision"] is not None:
                raise PermissionError("审批已被决定过")
            self.conn.execute("UPDATE approvals SET decision=%s WHERE id=%s", (decision, key))
            self.event(state, "approval", {"id": key, "decision": decision})

    def consume_approval(self, key, state, action):
        with self.conn.transaction():
            a = self.conn.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (key,)).fetchone()
            if not a:
                raise PermissionError("未知的审批")
            self._check_approval(a, state)
            if a["action_hash"] != digest(action) or a["decision"] is None:
                raise PermissionError("审批缺失")
            self.conn.execute("UPDATE approvals SET consumed=true WHERE id=%s", (key,))
            return a["decision"] == "approve"

    @contextmanager
    def writer(self, run_id):
        row = self.conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok", (run_id,)).fetchone()
        if not row["ok"]:
            raise RuntimeError("该 Run 已有写入者")
        try:
            yield
        finally:
            self.conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (run_id,))
