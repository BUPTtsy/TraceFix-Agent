from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from dataclasses import dataclass
from typing import Any


CLI_CONTRACT_VERSION = "tracefix-cli/1"


class CursorError(ValueError):
    pass


class CursorScopeError(CursorError):
    pass


@dataclass(frozen=True)
class EventCursor:
    scope_id: str
    run_id: str
    seq: int

    def __post_init__(self):
        if (not isinstance(self.scope_id, str) or not self.scope_id
                or not isinstance(self.run_id, str) or not self.run_id or isinstance(self.seq, bool)
                or not isinstance(self.seq, int) or self.seq < 0):
            raise CursorError("事件游标无效")

    def encode(self) -> str:
        raw = json.dumps({"scope_id": self.scope_id, "run_id": self.run_id, "seq": self.seq},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str, scope_id: str, run_id: str) -> "EventCursor":
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise CursorError("事件游标必须是非空字符串")
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"scope_id", "run_id", "seq"}:
                raise CursorError("事件游标字段无效")
            cursor = cls(data["scope_id"], data["run_id"], data["seq"])
        except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as error:
            raise CursorError("事件游标格式无效") from error
        if (cursor.scope_id, cursor.run_id) != (scope_id, run_id):
            raise CursorScopeError("事件游标作用域与 Run 不匹配")
        if cursor.encode() != value:
            raise CursorError("事件游标编码不规范")
        return cursor


@dataclass(frozen=True)
class EventBatch:
    events: list[dict[str, Any]]
    cursor: str
    high_watermark: int
    snapshot: dict[str, Any] | None = None
    requires_new_observation: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CLI_CONTRACT_VERSION,
            "events": copy.deepcopy(self.events),
            "cursor": self.cursor,
            "snapshot": copy.deepcopy(self.snapshot),
            "requires_new_observation": self.requires_new_observation,
            "high_watermark": self.high_watermark,
        }


class EventAdapter:
    def __init__(self, store):
        self.store = store

    def read(self, run_id: str, scope_id: str, cursor: str | None = None) -> EventBatch:
        all_events, store_snapshot, high = self.store.trace_snapshot(run_id, scope_id)
        self._validate_events(all_events, run_id, scope_id)
        start = 0
        cursor_error = False
        if cursor is not None:
            try:
                start = EventCursor.decode(cursor, scope_id, run_id).seq
            except CursorScopeError:
                raise
            except CursorError:
                cursor_error = True
        events = [event for event in all_events if event["seq"] > start]
        reason = "cursor_invalid" if cursor_error else None
        if not cursor_error and start > high:
            reason = "cursor_ahead"
        elif not cursor_error and all_events and all_events[0]["seq"] > start + 1:
            reason = "cursor_expired"
        elif not cursor_error and (events and events[0]["seq"] != start + 1
                or any(right["seq"] != left["seq"] + 1
                       for left, right in zip(events, events[1:]))):
            reason = "durable_gap"
        snapshot = None
        if reason:
            snapshot = self._snapshot(run_id, scope_id, high, reason, all_events, store_snapshot)
            events = []
        return EventBatch(events=copy.deepcopy(events),
                          cursor=EventCursor(scope_id, run_id, high).encode(),
                          snapshot=snapshot,
                          requires_new_observation=bool(snapshot), high_watermark=high)

    def _snapshot(self, run_id: str, scope_id: str, high: int, reason: str,
                  events: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
        state = snapshot.get("state", {})
        snapshot.update({
            "scope_id": scope_id,
            "run_id": run_id,
            "high_watermark": high,
            "reason": reason,
            "child_identities": self._children(events),
            "child_history_complete": (not events or events[0]["seq"] == 1)
                                      and all(right["seq"] == left["seq"] + 1
                                              for left, right in zip(events, events[1:])),
            "observation_ref": state.get("observation_ref"),
        })
        return copy.deepcopy(snapshot)

    @staticmethod
    def _children(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        children: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") or {}
            child_id = payload.get("child_run_id") or payload.get("task_id")
            if not child_id:
                continue
            entry = children.setdefault(str(child_id), {"child_run_id": payload.get("child_run_id")})
            for key in ("task_id", "role", "status", "generation", "parent_step_id"):
                if key in payload:
                    entry[key] = payload[key]
            entry["last_event"] = event["type"]
            entry["last_seq"] = event["seq"]
        return list(children.values())

    @staticmethod
    def _validate_events(events: list[dict[str, Any]], run_id: str, scope_id: str) -> None:
        previous = 0
        for event in events:
            required = {"run_id", "seq", "scope_id", "phase", "type", "revision", "at", "payload"}
            if not isinstance(event, dict) or not required <= event.keys() or not isinstance(event['payload'], dict):
                raise CursorError("存储事件不符合轨迹契约")
            if event.get("run_id") != run_id or event.get("scope_id") != scope_id:
                raise CursorError("事件作用域与请求不匹配")
            sequence = event.get("seq")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= previous:
                raise CursorError("事件序号必须严格递增")
            previous = sequence
