"""Bounded recovery decisions for loop and operation-boundary handling.

The module deliberately keeps recovery policy separate from the graph runner.  It
classifies observable evidence, accounts attempts per cause, and never treats a
new handle, timestamp, retry number, or patch hash as progress.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any, Mapping


class RecoveryCause(StrEnum):
    WAIT = "wait"
    STALE = "stale"
    CONTEXT = "context"
    REPEATED_NO_PROGRESS = "repeated_no_progress"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    ENVIRONMENT = "environment"


class RecoveryAction(StrEnum):
    AWAIT_CHILD = "await_child"
    REFRESH_OBSERVATION = "refresh_observation"
    COMPACT_CONTEXT = "compact_context"
    CORRECT_STRATEGY = "correct_strategy"
    STOP = "stop"


@dataclass(frozen=True)
class RecoveryBudget:
    max_attempts: int = 1
    deadline_seconds: float | None = None


@dataclass(frozen=True)
class RecoveryDecision:
    cause: RecoveryCause
    action: RecoveryAction
    attempt: int
    max_attempts: int
    can_continue: bool
    reason: str
    evidence: dict[str, Any]


_BUDGETS = {
    RecoveryCause.WAIT: RecoveryBudget(max_attempts=1),
    RecoveryCause.STALE: RecoveryBudget(max_attempts=1),
    RecoveryCause.CONTEXT: RecoveryBudget(max_attempts=1),
    RecoveryCause.REPEATED_NO_PROGRESS: RecoveryBudget(max_attempts=1),
    RecoveryCause.CANCELLED: RecoveryBudget(max_attempts=0),
    RecoveryCause.UNKNOWN: RecoveryBudget(max_attempts=0),
    RecoveryCause.ENVIRONMENT: RecoveryBudget(max_attempts=0),
}

_TECHNICAL_KEYS = {
    "attempt", "attempt_id", "attempts", "call", "call_id", "continuation",
    "continuation_id", "episode", "episode_id", "generation", "handle",
    "heartbeat", "id", "ref", "revision", "sequence", "step", "timestamp",
    "token_count", "retry", "retry_count", "patch_hash", "new_patch_hash",
}


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def classify_cause(
    error: Any = None,
    *,
    error_details: Mapping[str, Any] | None = None,
    signals: list[Mapping[str, Any]] | None = None,
    evidence: Mapping[str, Any] | None = None,
    pending_action: Any = None,
    child_status: str | None = None,
    cancelled: bool = False,
) -> RecoveryCause:
    """Classify a boundary using explicit status/evidence before heuristics."""

    details = dict(error_details or {})
    evidence = dict(evidence or {})
    status = str(details.get("status") or details.get("request_status") or "").upper()
    category = str(details.get("category") or "").lower()
    text = " ".join(str(value) for value in (error, details.get("message"), details.get("reason")))
    lowered = text.lower()
    if cancelled or status in {"CANCELLED", "CANCELED"} or "用户已取消" in text:
        return RecoveryCause.CANCELLED
    if status in {"UNKNOWN", "UNKNOWN_OPERATION", "APPLY_UNKNOWN"} or details.get("requires_manual_review"):
        return RecoveryCause.UNKNOWN
    if status in {"CONTEXT_OVERFLOW", "CONTEXT_PRESSURE", "COMPACT_FAILED"} or category == "context":
        return RecoveryCause.CONTEXT
    if child_status and str(child_status).lower() in {"running", "waiting", "pending", "queued"}:
        return RecoveryCause.WAIT
    if status in {"WAITING_CHILD", "WAITING_NETWORK", "TIMEOUT", "WAITING"}:
        return RecoveryCause.WAIT
    stale_markers = {"stale", "locator", "replay_unbound", "generation", "observation"}
    if (status in {"STALE", "REPLAY_UNBOUND"}
            or any(marker in lowered for marker in stale_markers)
            or evidence.get("stale") is True):
        return RecoveryCause.STALE
    if (status in {"ENVIRONMENT", "INFRA_FAILURE"} or category in {"environment", "infra"}
            or evidence.get("environment_changed") is True):
        return RecoveryCause.ENVIRONMENT
    signal_kinds = {str(item.get("kind", "")).lower() for item in (signals or [])}
    if signal_kinds & {"state_repeated", "action_cycle", "error_repeated", "no_progress", "repeated_no_progress"}:
        return RecoveryCause.REPEATED_NO_PROGRESS
    if isinstance(pending_action, Mapping) and pending_action.get("child_status"):
        return RecoveryCause.WAIT
    return RecoveryCause.ENVIRONMENT


def classify_stall(state: Any = None, **kwargs: Any) -> RecoveryCause:
    """Compatibility helper accepting a RunState-like object or mapping."""

    if state is not None:
        kwargs.setdefault("error", _value(state, "error"))
        kwargs.setdefault("error_details", _value(state, "error_details"))
        kwargs.setdefault("pending_action", _value(state, "pending_action"))
        kwargs.setdefault("cancelled", str(_value(state, "run_status", "")).upper() == "CANCELLED")
    return classify_cause(**kwargs)


def has_real_progress(before: Any, after: Any, evidence: Mapping[str, Any] | None = None) -> bool:
    """Return true only for a semantic/business change observable to the agent."""

    evidence = dict(evidence or {})
    for key in (
        "business_changed", "hypothesis_refuted", "verification_advanced",
        "child_settled", "new_observation", "observation_changed", "receipt_confirmed",
    ):
        if evidence.get(key) is True:
            return True
    if before is None or after is None:
        return False
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = set(before) | set(after)
        for key in keys:
            if str(key).lower() in _TECHNICAL_KEYS:
                continue
            if before.get(key) != after.get(key):
                return True
        return False
    return before != after


def _entries(history: list[Mapping[str, Any]] | None, cause: RecoveryCause) -> list[Mapping[str, Any]]:
    return [entry for entry in (history or []) if str(entry.get("cause")) == cause.value]


class RecoveryController:
    """Stateless policy helper; callers persist the returned episode entries."""

    def __init__(self, budgets: Mapping[RecoveryCause | str, RecoveryBudget] | None = None):
        self.budgets = dict(_BUDGETS)
        for key, value in (budgets or {}).items():
            self.budgets[RecoveryCause(key)] = value

    def budget(self, cause: RecoveryCause | str) -> RecoveryBudget:
        return self.budgets[RecoveryCause(cause)]

    @staticmethod
    def action(cause: RecoveryCause) -> RecoveryAction:
        return {
            RecoveryCause.WAIT: RecoveryAction.AWAIT_CHILD,
            RecoveryCause.STALE: RecoveryAction.REFRESH_OBSERVATION,
            RecoveryCause.CONTEXT: RecoveryAction.COMPACT_CONTEXT,
            RecoveryCause.REPEATED_NO_PROGRESS: RecoveryAction.CORRECT_STRATEGY,
        }.get(cause, RecoveryAction.STOP)

    def decide(
        self,
        cause: RecoveryCause | str,
        history: list[Mapping[str, Any]] | None = None,
        *,
        now: float | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> RecoveryDecision:
        cause = RecoveryCause(cause)
        budget = self.budget(cause)
        entries = _entries(history, cause)
        attempt = len(entries) + 1
        now = monotonic() if now is None else now
        deadline_expired = bool(entries and budget.deadline_seconds is not None
                                and now - float(entries[0].get("started_at", now)) >= budget.deadline_seconds)
        allowed = cause not in {RecoveryCause.CANCELLED, RecoveryCause.UNKNOWN, RecoveryCause.ENVIRONMENT}
        can_continue = allowed and attempt <= budget.max_attempts and not deadline_expired
        action = self.action(cause) if can_continue else RecoveryAction.STOP
        if not allowed:
            reason = "取消或未知副作用必须保持终态，等待人工核对"
        elif deadline_expired:
            reason = "同一原因 episode 已超过 deadline"
        elif not can_continue:
            reason = "同一原因 episode 已耗尽有限恢复次数"
        else:
            reason = f"允许第 {attempt} 次有界恢复"
        return RecoveryDecision(cause, action, attempt, budget.max_attempts, can_continue,
                                reason, dict(evidence or {}))

    def record(
        self,
        history: list[Mapping[str, Any]] | None,
        decision: RecoveryDecision,
        *,
        status: str,
        progress: bool = False,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now = monotonic() if now is None else now
        entry = {
            "cause": decision.cause.value,
            "action": decision.action.value,
            "attempt": decision.attempt,
            "max_attempts": decision.max_attempts,
            "status": status,
            "progress": bool(progress),
            "started_at": now,
            "reason": decision.reason,
            "evidence": decision.evidence,
        }
        return [dict(item) for item in (history or [])] + [entry]


__all__ = [
    "RecoveryAction", "RecoveryBudget", "RecoveryCause", "RecoveryController",
    "RecoveryDecision", "classify_cause", "classify_stall", "has_real_progress",
]
