from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from tracefix.runtime.contracts import RunState, digest, new_id
from tracefix.storage.artifacts import sanitize, redact
from tracefix.runtime.effects import (operation_epoch, operation_resources,
                                     reconciliation_result, recovery_identity, resource_fence_conflicts)


class UnknownOperation(RuntimeError):
    """Intent exists without receipt: never blindly re-execute a side effect."""

    status = 'UNKNOWN_OPERATION'
    category = 'tool_execution'

    def __init__(self, operation_id):
        super().__init__('操作未确认，相关资源已锁定：' + operation_id)
        self.details = dict(operation_id=operation_id, requires_manual_review=True, retryable=False)


def _caller_identity():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


class MemoryStore:
    """Explicit CI smoke store; never selected by a real run or resume."""
    def __init__(self):
        self.runs, self.events, self.operations, self.approvals, self.projects = {}, {}, {}, {}, {}
        self.locked = set()
        self._operation_lock = threading.RLock()
        self._operation_owners = {}

    def save(self, state: RunState):
        with self._operation_lock:
            self.runs[state.run_id] = state.model_dump(mode="json")

    def load(self, run_id: str, scope: str) -> RunState:
        s = RunState(**self.runs[run_id])
        if s.scope_id != scope:
            raise PermissionError("该 Run 属于另一个项目")
        return s

    def event(self, state, kind, payload=None):
        with self._operation_lock:
            es = self.events.setdefault(state.run_id, [])
            event = dict(run_id=state.run_id, seq=len(es)+1, scope_id=state.scope_id,
                         phase=str(state.phase), type=kind, revision=state.revision, at=time.time(),
                         payload=redact(payload or {}))
            es.append(event)
            return event

    def trace(self, run_id, scope, after=0):
        self.load(run_id, scope)
        return copy.deepcopy([e for e in self.events.get(run_id, []) if e["seq"] > after])

    def snapshot(self, run_id, scope):
        with self._operation_lock:
            state = self.load(run_id, scope)
            pending = []
            for operation_id, record in self.operations.items():
                if record.get('run_id') == run_id and record.get('scope_id') == scope and record.get('status') != 'DONE':
                    pending.append({key: copy.deepcopy(record.get(key)) for key in
                                    ('operation_id', 'status', 'resources', 'epoch', 'resolution')})
                    pending[-1]['operation_id'] = operation_id
            return {'state': state.model_dump(mode='json'), 'pending_operations': pending}

    def trace_snapshot(self, run_id, scope):
        with self._operation_lock:
            snapshot = self.snapshot(run_id, scope)
            events = copy.deepcopy(self.events.get(run_id, []))
            return events, snapshot, events[-1]['seq'] if events else 0

    def begin(self, state, op_id, intent, *, owner=None, resources=None, ancestors=(), notify=None):
        resource_keys = operation_resources(intent, resources)
        with self._operation_lock:
            for record in self.operations.values():
                if record['status'] == 'STARTED' and not record.get('owner_token'):
                    record.update(status='UNKNOWN', resources=['*'], owner_token=None, epoch=None)
                if record['status'] != 'DONE' and not record.get('resources'):
                    record['resources'] = ['*']
            old = self.operations.get(op_id)
            if old:
                self._check_operation_identity(old, state, intent)
                if old['status'] != 'DONE':
                    raise UnknownOperation(op_id)
                return copy.deepcopy(old['receipt'])
            for key, record in self.operations.items():
                if (record['status'] != 'DONE'
                        and resource_fence_conflicts(state.scope_id, resource_keys, record)):
                    if record['status'] == 'STARTED' and record.get('owner_token') in ancestors:
                        continue
                    raise UnknownOperation(key)
            token = owner or new_id('owner')
            self.operations[op_id] = dict(intent_hash=digest(intent), scope_id=state.scope_id,
                run_id=state.run_id, status='STARTED', receipt=None, resources=resource_keys,
                owner_token=token, epoch=operation_epoch(state), resolution=None)
            if owner is None:
                self._operation_owners[(op_id, _caller_identity())] = token
            event = self.event(state, 'tool.started', {'operation_id': op_id, 'intent': intent,
                                                     'resources': resource_keys})
        if notify:
            notify(event)
        return None

    @staticmethod
    def _check_operation_identity(record, state, intent):
        if (record['intent_hash'] != digest(intent) or record['scope_id'] != state.scope_id
                or record['run_id'] != state.run_id):
            raise PermissionError('操作身份不匹配')

    def _owned_operation(self, state, op_id, owner):
        record = self.operations.get(op_id)
        token = owner or self._operation_owners.get((op_id, _caller_identity()))
        self._check_operation_owner(record, state, token)
        return record

    @staticmethod
    def _check_operation_owner(record, state, owner):
        if (not record or not owner or record.get('owner_token') != owner
                or record['scope_id'] != state.scope_id or record['run_id'] != state.run_id
                or record.get('epoch') != operation_epoch(state) or record['status'] != 'STARTED'):
            raise PermissionError('操作写入者、epoch 或状态不匹配')

    def finish(self, state, op_id, receipt, *, owner=None, notify=None):
        if receipt is None:
            raise ValueError('完成操作必须提供明确回执')
        json.dumps(receipt)
        with self._operation_lock:
            record = self._owned_operation(state, op_id, owner)
            record.update(status='DONE', receipt=copy.deepcopy(receipt))
            self._operation_owners.pop((op_id, _caller_identity()), None)
            event = self.event(state, 'tool.completed', {'operation_id': op_id, 'receipt': receipt})
        if notify:
            notify(event)

    def mark_unknown(self, state, op_id, *, owner=None, reason, notify=None):
        with self._operation_lock:
            record = self._owned_operation(state, op_id, owner)
            record.update(status='UNKNOWN', resolution={'reason': reason})
            self._operation_owners.pop((op_id, _caller_identity()), None)
            event = self.event(state, 'tool.unknown', {'operation_id': op_id, 'reason': reason,
                                                     'resources': record['resources']})
        if notify:
            notify(event)

    def reconcile(self, state, op_id, receipt, *, reviewer, notify=None):
        with self._operation_lock:
            record = self.operations.get(op_id)
            if not record or record['status'] != 'UNKNOWN':
                raise PermissionError('只能人工核对 UNKNOWN 操作')
            result = reconciliation_result(record, state, op_id, receipt, reviewer)
            record.update(status='DONE', receipt=copy.deepcopy(result),
                          resolution=copy.deepcopy({**receipt, 'reviewer': reviewer}))
            event = self.event(state, 'tool.reconciled', {'operation_id': op_id,
                                                       'reviewer': reviewer, 'receipt': receipt})
        if notify:
            notify(event)
        return copy.deepcopy(result)

    def recover_started(self, state, op_id, receipt, *, reviewer, notify=None):
        with self._operation_lock:
            record = self.operations.get(op_id)
            if not record or record['status'] != 'STARTED':
                raise PermissionError('只能恢复未确认的 STARTED 操作')
            recovery_identity(record, state, op_id, receipt, reviewer)
            record.update(status='UNKNOWN', resolution=copy.deepcopy({**receipt, 'reviewer': reviewer}))
            event = self.event(state, 'tool.unknown', {'operation_id': op_id, 'reviewer': reviewer,
                                                     'receipt': receipt})
        if notify:
            notify(event)

    @contextmanager
    def operation_guard(self, state, op_id, *, owner):
        with self._operation_lock:
            self._owned_operation(state, op_id, owner)
            yield

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
        with self._operation_lock:
            if run_id in self.locked:
                raise RuntimeError("该 Run 已有写入者")
            self.locked.add(run_id)
        try:
            yield
        finally:
            with self._operation_lock:
                self.locked.remove(run_id)


class PostgresStore(MemoryStore):
    def __init__(self, dsn: str):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._operation_lock = threading.RLock()
        self._operation_owners = {}

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

    def snapshot(self, run_id, scope):
        return self.trace_snapshot(run_id, scope)[1]

    def trace_snapshot(self, run_id, scope):
        with self._operation_lock, self.conn.transaction():
            self.conn.execute("SELECT id FROM runs WHERE id=%s AND scope_id=%s FOR UPDATE", (run_id, scope))
            state = self.load(run_id, scope)
            rows = self.conn.execute(
                "SELECT id,status,resources,epoch,resolution FROM operations WHERE run_id=%s AND scope_id=%s AND status <> 'DONE'",
                (run_id, scope)).fetchall()
            snapshot = {'state': state.model_dump(mode='json'), 'pending_operations': [
                {"operation_id": row["id"], "status": row["status"],
                 "resources": copy.deepcopy(row["resources"]), "epoch": row["epoch"],
                 "resolution": copy.deepcopy(row["resolution"])} for row in rows]}
            rows = self.conn.execute("SELECT payload FROM events WHERE run_id=%s ORDER BY seq", (run_id,)).fetchall()
            events = [copy.deepcopy(row['payload']) for row in rows]
            return events, snapshot, events[-1]['seq'] if events else 0

    def _scope_lock(self, scope_id):
        self.conn.execute("SET LOCAL lock_timeout = '5s'")
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                          "current_schema() || ':r05:resource-admission', 0))")
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended("
                          "current_schema() || ':r05:' || %s, 0))", (scope_id,))

    def begin(self, state, op_id, intent, *, owner=None, resources=None, ancestors=(), notify=None):
        resource_keys = operation_resources(intent, resources)
        token = owner or new_id('owner')
        with self._operation_lock:
            with self.conn.transaction():
                self._scope_lock(state.scope_id)
                self.conn.execute("UPDATE operations SET status='UNKNOWN',resources='[\"*\"]'::jsonb "
                                  "WHERE scope_id=%s AND status='STARTED' AND owner_token IS NULL",
                                  (state.scope_id,))
            with self.conn.transaction():
                self._scope_lock(state.scope_id)
                old = self.conn.execute('SELECT * FROM operations WHERE id=%s FOR UPDATE', (op_id,)).fetchone()
                if old:
                    self._check_operation_identity(old, state, intent)
                    if old['status'] != 'DONE':
                        raise UnknownOperation(op_id)
                    return copy.deepcopy(old['receipt'])
                pending = self.conn.execute("SELECT id,scope_id,resources,status,owner_token FROM operations "
                    "WHERE status <> 'DONE'").fetchall()
                for record in pending:
                    if resource_fence_conflicts(state.scope_id, resource_keys, record):
                        if record['status'] == 'STARTED' and record.get('owner_token') in ancestors:
                            continue
                        raise UnknownOperation(record['id'])
                cursor = self.conn.execute('''INSERT INTO operations
                    (id,run_id,scope_id,intent_hash,status,receipt,resources,owner_token,epoch)
                    VALUES (%s,%s,%s,%s,'STARTED',NULL,%s::jsonb,%s,%s) ON CONFLICT DO NOTHING''',
                    (op_id, state.run_id, state.scope_id, digest(intent), json.dumps(resource_keys),
                     token, operation_epoch(state)))
                if cursor.rowcount != 1:
                    raise PermissionError('操作并发创建失败')
                event = self.event(state, 'tool.started', {'operation_id': op_id, 'intent': intent,
                                                         'resources': resource_keys})
            if owner is None:
                self._operation_owners[(op_id, _caller_identity())] = token
        if notify:
            notify(event)
        return None

    def _update_operation(self, state, op_id, status, receipt, resolution, owner):
        token = owner or self._operation_owners.get((op_id, _caller_identity()))
        self.conn.execute("SET LOCAL lock_timeout = '5s'")
        record = self.conn.execute('SELECT * FROM operations WHERE id=%s FOR UPDATE', (op_id,)).fetchone()
        self._check_operation_owner(record, state, token)
        cursor = self.conn.execute('''UPDATE operations SET status=%s,receipt=%s::jsonb,resolution=%s::jsonb
            WHERE id=%s AND scope_id=%s AND run_id=%s AND epoch=%s AND owner_token=%s AND status='STARTED' ''',
            (status, json.dumps(receipt), json.dumps(resolution), op_id, state.scope_id, state.run_id,
             operation_epoch(state), token))
        if cursor.rowcount != 1:
            raise PermissionError('操作完成未更新唯一 ownership 记录')

    def finish(self, state, op_id, receipt, *, owner=None, notify=None):
        if receipt is None:
            raise ValueError('完成操作必须提供明确回执')
        with self._operation_lock, self.conn.transaction():
            self._update_operation(state, op_id, 'DONE', receipt, None, owner)
            event = self.event(state, 'tool.completed', {'operation_id': op_id, 'receipt': receipt})
        self._operation_owners.pop((op_id, _caller_identity()), None)
        if notify:
            notify(event)

    def mark_unknown(self, state, op_id, *, owner=None, reason, notify=None):
        with self._operation_lock, self.conn.transaction():
            self._update_operation(state, op_id, 'UNKNOWN', None, {'reason': reason}, owner)
            event = self.event(state, 'tool.unknown', {'operation_id': op_id, 'reason': reason})
        self._operation_owners.pop((op_id, _caller_identity()), None)
        if notify:
            notify(event)

    def reconcile(self, state, op_id, receipt, *, reviewer, notify=None):
        with self._operation_lock, self.conn.transaction():
            self.conn.execute("SET LOCAL lock_timeout = '5s'")
            record = self.conn.execute('SELECT * FROM operations WHERE id=%s FOR UPDATE', (op_id,)).fetchone()
            if not record or record['status'] != 'UNKNOWN':
                raise PermissionError('只能人工核对 UNKNOWN 操作')
            result = reconciliation_result(record, state, op_id, receipt, reviewer)
            cursor = self.conn.execute('''UPDATE operations SET status='DONE',receipt=%s::jsonb,
                resolution=%s::jsonb WHERE id=%s AND scope_id=%s AND run_id=%s AND status='UNKNOWN' ''',
                (json.dumps(result), json.dumps({**receipt, 'reviewer': reviewer}), op_id,
                 state.scope_id, state.run_id))
            if cursor.rowcount != 1:
                raise PermissionError('人工核对未更新唯一 UNKNOWN 记录')
            event = self.event(state, 'tool.reconciled', {'operation_id': op_id,
                                                       'reviewer': reviewer, 'receipt': receipt})
        if notify:
            notify(event)
        return copy.deepcopy(result)

    def recover_started(self, state, op_id, receipt, *, reviewer, notify=None):
        with self._operation_lock, self.conn.transaction():
            self.conn.execute("SET LOCAL lock_timeout = '5s'")
            record = self.conn.execute('SELECT * FROM operations WHERE id=%s FOR UPDATE', (op_id,)).fetchone()
            if not record or record['status'] != 'STARTED':
                raise PermissionError('只能恢复未确认的 STARTED 操作')
            recovery_identity(record, state, op_id, receipt, reviewer)
            cursor = self.conn.execute('''UPDATE operations SET status='UNKNOWN',resolution=%s::jsonb
                WHERE id=%s AND scope_id=%s AND run_id=%s AND owner_token=%s AND epoch=%s
                AND status='STARTED' ''', (json.dumps({**receipt, 'reviewer': reviewer}), op_id,
                state.scope_id, state.run_id, record['owner_token'], record['epoch']))
            if cursor.rowcount != 1:
                raise PermissionError('STARTED 恢复未更新唯一 ownership 记录')
            event = self.event(state, 'tool.unknown', {'operation_id': op_id, 'reviewer': reviewer,
                                                     'receipt': receipt})
        if notify:
            notify(event)

    @contextmanager
    def operation_guard(self, state, op_id, *, owner):
        with self._operation_lock, self.conn.transaction():
            self.conn.execute("SET LOCAL lock_timeout = '5s'")
            record = self.conn.execute('SELECT * FROM operations WHERE id=%s FOR UPDATE', (op_id,)).fetchone()
            self._check_operation_owner(record, state, owner)
            yield

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
