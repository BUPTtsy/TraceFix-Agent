"""Versioned contracts. Models propose actions; only the runtime changes state."""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "tracefix/1"


def digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    data = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(data).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Phase(StrEnum):
    PREPARE = "PREPARE"
    EXPLORE = "EXPLORE"
    REPRODUCE = "REPRODUCE"
    DIAGNOSE = "DIAGNOSE"
    PATCH = "PATCH"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    FINALIZE = "FINALIZE"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    ABNORMAL = "ABNORMAL"
    # Historical records may still contain FAILED. New automatic termination
    # paths use ABNORMAL only for detected loops.
    FAILED = "FAILED"


class Outcome(StrEnum):
    FIX_VERIFIED = "FIX_VERIFIED"
    NO_BUG_FOUND = "NO_BUG_FOUND"
    BUG_CONFIRMED = "BUG_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    LOOP_DETECTED = "LOOP_DETECTED"
    # Retained for deserializing records created before W-29.
    REPAIR_EXHAUSTED = "REPAIR_EXHAUSTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    INFRA_FAILURE = "INFRA_FAILURE"


class Usage(Contract):
    """Usage counters. They are observed and never used as run limits."""

    model_config = ConfigDict(extra="ignore")
    model_calls: int = 0
    browser_actions: int = 0
    patches: int = 0
    subtasks: int = 0
    tokens: int = 0
    cost_usd: float = 0
    # Deprecated fields stay assignable while old checkpoints are read, but
    # are excluded from serialized usage and never enforce a limit.
    max_model_calls: int | None = Field(default=80, exclude=True)
    max_browser_actions: int | None = Field(default=100, exclude=True)
    max_patches: int | None = Field(default=3, exclude=True)
    max_subtasks: int | None = Field(default=2, exclude=True)
    max_tokens: int | None = Field(default=160_000, exclude=True)
    max_cost_usd: float | None = Field(default=10, exclude=True)
    deadline: float | None = Field(default=None, exclude=True)
    last_progress: float | None = Field(default=None, exclude=True)
    stall_seconds: int | None = Field(default=None, exclude=True)

    def charge(self, kind: str, amount: int = 1) -> Usage:
        if kind not in {"model_calls", "browser_actions", "patches", "subtasks", "tokens"}:
            raise ValueError("未知的用量维度")
        if amount < 0:
            raise ValueError("用量不可退回")
        data = self.model_dump()
        data[kind] += amount
        return type(self)(**data)

    def check_time(self) -> None:
        # Kept as a source-compatible no-op for older callers.
        return None


# Compatibility import for code and persisted state written before W-29.
Budget = Usage


class BudgetExceeded(RuntimeError):
    pass


class ReplayUnbound(RuntimeError):
    """已记录的动作在当前页面上无法唯一绑定：重放不成立，但不是基础设施故障。"""


class Locator(Contract):
    role: str
    name: str


ActionKind = Literal["navigate", "click", "type", "select", "press", "observe", "finish"]


class BrowserAction(Contract):
    kind: ActionKind
    observation_id: str | None = None
    element_ref: str | None = None
    locator: Locator | None = None
    value: str | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind in {"click", "type", "select"} and not self.locator:
            raise ValueError("元素操作需要稳定的 role/name 定位器")
        if self.kind in {"navigate", "type", "select", "press"} and self.value is None:
            raise ValueError("该操作需要提供 value")
        return self


class Assertion(Contract):
    locator: Locator
    condition: Literal["visible", "absent", "checked", "disabled", "enabled"] = "visible"


class TestSpec(Contract):
    # Ignore the removed max_steps field when reading old artifacts.
    model_config = ConfigDict(extra="ignore")
    goal: str = Field(min_length=5)
    preconditions: list[str] = Field(default_factory=list)
    authorized_actions: list[ActionKind] = Field(default_factory=lambda: ["navigate", "click", "type", "select", "press", "observe", "finish"],
        description='Exact action kinds, never sentences. Include navigate and finish; authorize only actions needed by the goal.')
    assertions: list[Assertion] = Field(min_length=1)
    regression_plan: list[BrowserAction] = Field(default_factory=list)
    regression_assertions: list[Assertion] = Field(min_length=1)
    @model_validator(mode="after")
    def validate_actions(self):
        if not {'navigate', 'finish'} <= set(self.authorized_actions):
            raise ValueError('authorized_actions 必须包含流程必需的 navigate 和 finish')
        if any(action.kind not in self.authorized_actions for action in self.regression_plan):
            raise ValueError('regression_plan 含有未被 authorized_actions 授权的动作')
        if any(action.observation_id is not None or action.element_ref is not None for action in self.regression_plan):
            raise ValueError('regression_plan 的 observation_id 和 element_ref 必须为 null，重放时由运行时绑定')
        return self


class Decision(Contract):
    action: BrowserAction
    evidence_refs: list[str] = Field(default_factory=list)
    rule_refs: list[str] = Field(default_factory=list, max_length=50)
    expected_observation: str = ""
    summary: str = Field(default="", max_length=1200)


class FileEdit(Contract):
    path: str
    before_hash: str
    content: str = Field(max_length=150_000)


class PatchProposal(Contract):
    summary: str
    evidence_refs: list[str] = Field(min_length=1)
    rule_refs: list[str] = Field(default_factory=list, max_length=50)
    edits: list[FileEdit] = Field(min_length=1, max_length=8)


class Validation(Contract):
    kind: Literal["static", "unit", "build", "health", "original", "regression"]
    passed: bool
    source_manifest: str
    patch_hash: str
    environment_digest: str
    verifier_version: str = SCHEMA_VERSION
    test_spec_hash: str
    artifact_ref: str


class RunState(Contract):
    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("run"))
    scope_id: str
    goal: str
    parent_run_id: str | None = None
    continuation_instruction: str | None = None
    continuation_count: int = 0
    abnormal_termination: bool = False
    continuation_markers: list[dict] = Field(default_factory=list)
    remote_config: dict | None = None
    url: str
    mode: Literal["test", "repair"] = "test"
    phase: Phase = Phase.PREPARE
    run_status: RunStatus = RunStatus.RUNNING
    outcome: Outcome | None = None
    revision: int = 0
    repo_snapshot_ref: str = ""
    memory_snapshot_ref: str = ""
    agent_instructions_ref: str | None = None
    agent_instructions_hash: str | None = None
    agent_instructions_path: str | None = None
    test_spec_ref: str | None = None
    replay_plan_ref: str | None = None
    observation_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    hypothesis_refs: list[str] = Field(default_factory=list)
    working_set_refs: list[str] = Field(default_factory=list)
    patch_ref: str | None = None
    patch_hash: str | None = None
    validation_refs: list[str] = Field(default_factory=list)
    pending_action: dict | None = None
    budget: Usage = Field(default_factory=Usage)
    subtask_refs: list[str] = Field(default_factory=list)
    approval_ref: str | None = None
    report_ref: str | None = None
    environment_digest: str = ""
    source_manifest: str = ""
    test_spec_hash: str = ""
    reproduced: bool = False
    source_aligned: bool = False
    step: int = 0
    replay_index: int = 0
    trial: int = 0
    failure_signatures: list[str] = Field(default_factory=list)
    validation_index: int = 0
    error: str | None = None
    error_details: dict | None = None
    local_branch: str | None = None
    action_fingerprints: list[str] = Field(default_factory=list)
    loop_state_fingerprints: list[str] = Field(default_factory=list)
    loop_error_signatures: list[str] = Field(default_factory=list)
    loop_no_progress_steps: int = 0
    loop_warnings: list[dict] = Field(default_factory=list)
    loop_evidence: dict | None = None
    last_error_signature: str | None = None
    baseline_validation_refs: list[str] = Field(default_factory=list)
    model_exchange_refs: list[str] = Field(default_factory=list)
    rule_snapshot_ref: str | None = None
    rule_snapshot_hash: str = ""
    rule_refs: list[dict] = Field(default_factory=list)
    additional_rule_ids: list[str] = Field(default_factory=list)


TRANSITIONS = {
    Phase.PREPARE: {Phase.EXPLORE, Phase.FINALIZE},
    Phase.EXPLORE: {Phase.REPRODUCE, Phase.FINALIZE},
    Phase.REPRODUCE: {Phase.EXPLORE, Phase.DIAGNOSE, Phase.FINALIZE},
    Phase.DIAGNOSE: {Phase.PATCH, Phase.FINALIZE},
    Phase.PATCH: {Phase.VERIFY, Phase.FINALIZE},
    Phase.VERIFY: {Phase.DIAGNOSE, Phase.REVIEW, Phase.FINALIZE},
    Phase.REVIEW: {Phase.FINALIZE},
    Phase.FINALIZE: set(),
}
IMMUTABLE = {"run_id", "scope_id", "schema_version", "mode", "goal", "url"}


def reduce_state(state: RunState, expected_revision: int, **delta) -> RunState:
    if state.revision != expected_revision:
        raise ValueError("版本已过期")
    if state.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                            RunStatus.FAILED}:
        raise ValueError("终止状态不可变更")
    if IMMUTABLE & delta.keys() or "revision" in delta:
        raise ValueError("该状态字段不可变更")
    if state.test_spec_ref and any(delta.get(field, getattr(state, field)) != getattr(state, field)
                                   for field in ('test_spec_ref', 'test_spec_hash')):
        raise ValueError('已冻结的 TestSpec 不可变更')
    phase = Phase(delta.get("phase", state.phase))
    if phase != state.phase and phase not in TRANSITIONS[state.phase]:
        if not (state.continuation_count and state.phase == Phase.PREPARE and phase == Phase.VERIFY
                and state.patch_hash and state.reproduced and state.source_aligned and state.replay_plan_ref):
            raise ValueError(f"非法的阶段转换：{state.phase} -> {phase}")
    if phase == Phase.PATCH and not (state.reproduced and state.source_aligned):
        raise ValueError("补丁门禁：需要先完成复现并与源码对齐")
    if delta.get("patch_hash", state.patch_hash) != state.patch_hash:
        delta["validation_refs"] = []
        delta["approval_ref"] = None
    return RunState.model_validate({**state.model_dump(), **delta, "revision": state.revision + 1})


def verification_gate(state: RunState, validations: list[Validation], artifact_exists) -> bool:
    required = {"static", "unit", "build", "health", "original", "regression"}
    good = set()
    for v in validations:
        current = (v.source_manifest == state.source_manifest and v.patch_hash == state.patch_hash
                   and v.environment_digest == state.environment_digest
                   and v.test_spec_hash == state.test_spec_hash
                   and v.verifier_version == SCHEMA_VERSION)
        if current and v.passed and artifact_exists(v.artifact_ref):
            good.add(v.kind)
    return bool(state.reproduced and state.source_aligned and state.patch_hash
                and required <= good and all(artifact_exists(r) for r in state.evidence_refs))
