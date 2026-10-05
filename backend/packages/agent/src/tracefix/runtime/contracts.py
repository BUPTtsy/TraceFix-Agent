"""Versioned contracts. Models propose actions; only the runtime changes state."""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
import time
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
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class Outcome(StrEnum):
    FIX_VERIFIED = "FIX_VERIFIED"
    NO_BUG_FOUND = "NO_BUG_FOUND"
    BUG_CONFIRMED = "BUG_CONFIRMED"
    INCONCLUSIVE = "INCONCLUSIVE"
    LOOP_DETECTED = "LOOP_DETECTED"
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
    page_generation: int | None = Field(default=None, ge=0)
    locator: Locator | None = None
    value: str | None = None
    preconditions: list[Assertion] = Field(default_factory=list)
    postconditions: list[Assertion] = Field(default_factory=list)
    wait: ObservableWait | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind in {"click", "type", "select"} and not self.locator:
            raise ValueError("元素操作需要稳定的 role/name 定位器")
        if self.kind in {"navigate", "type", "select", "press"} and self.value is None:
            raise ValueError("该操作需要提供 value")
        return self


class Assertion(Contract):
    locator: Locator
    condition: Literal["visible", "absent", "checked", "unchecked", "disabled", "enabled"] = "visible"


class ObservableWait(Contract):
    assertions: list[Assertion] = Field(default_factory=list)
    timeout_seconds: float = Field(default=5, gt=0, le=30)
    interval_seconds: float = Field(default=0.2, gt=0, le=2)
    max_observations: int = Field(default=10, ge=1, le=30)


class BehaviorStep(Contract):
    action: BrowserAction
    assertions: list[Assertion] = Field(default_factory=list)


class BehaviorScenario(Contract):
    id: str = Field(min_length=1)
    description: str = ""
    steps: list[BehaviorStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_checkpoints(self):
        # 中间检查点保留状态转移证据，避免只看最终状态而漏掉被补丁破坏的正向或逆向能力。
        if not self.steps[-1].assertions:
            raise ValueError('业务场景最后一步必须包含断言')
        if len(self.steps) > 1 and not any(step.assertions for step in self.steps[:-1]):
            raise ValueError('多步业务场景必须包含中间断言')
        if any(step.action.kind == 'finish' for step in self.steps):
            raise ValueError('业务场景不能使用 finish 跳过运行时验证')
        return self


class GuidanceAck(Contract):
    # 模型逐条说明引导如何应用，运行时按 ID 校验后记录确认结果。
    id: str = Field(min_length=1)
    how_applied: str = Field(min_length=1, max_length=2000)


class TestSpec(Contract):
    # Ignore the removed max_steps field when reading old artifacts.
    model_config = ConfigDict(extra="ignore")
    goal: str = Field(min_length=5)
    preconditions: list[str] = Field(default_factory=list)
    executable_preconditions: list[Assertion] = Field(default_factory=list)
    authorized_actions: list[ActionKind] = Field(default_factory=lambda: ["navigate", "click", "type", "select", "press", "observe", "finish"],
        description='Exact action kinds, never sentences. Include navigate and finish; authorize only actions needed by the goal.')
    assertions: list[Assertion] = Field(min_length=1)
    regression_plan: list[BrowserAction] = Field(default_factory=list)
    regression_assertions: list[Assertion] = Field(min_length=1)
    behavior_scenarios: list[BehaviorScenario] = Field(default_factory=list)
    guidance_ack: list[GuidanceAck] = Field(default_factory=list)
    @model_validator(mode="after")
    def validate_actions(self):
        if not {'navigate', 'finish'} <= set(self.authorized_actions):
            raise ValueError('authorized_actions 必须包含流程必需的 navigate 和 finish')
        if any(action.kind not in self.authorized_actions for action in self.regression_plan):
            raise ValueError('regression_plan 含有未被 authorized_actions 授权的动作')
        if any(action.observation_id is not None or action.element_ref is not None
               or action.page_generation is not None for action in self.regression_plan):
            raise ValueError('regression_plan 的 observation_id 和 element_ref 必须为 null，重放时由运行时绑定')
        ids = [scenario.id for scenario in self.behavior_scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError('业务场景 id 不可重复')
        for scenario in self.behavior_scenarios:
            for step in scenario.steps:
                if step.action.kind not in self.authorized_actions:
                    raise ValueError('业务场景含有未被 authorized_actions 授权的动作')
                if (step.action.observation_id is not None or step.action.element_ref is not None
                        or step.action.page_generation is not None):
                    raise ValueError('业务场景的 observation_id 和 element_ref 必须为 null')
        return self


class GuidanceConstraints(Contract):
    # 只描述可执行的收窄条件；最终允许范围是项目授权与所有约束的交集。
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    max_files_changed: int | None = Field(default=None, ge=1)
    max_lines_changed: int | None = Field(default=None, ge=1)
    allowed_actions: list[ActionKind] | None = None

    @model_validator(mode="after")
    def validate_restrictions(self):
        for pattern in self.include_paths + self.exclude_paths:
            if not pattern.strip() or pattern.startswith(('/', '\\')) or ':' in pattern or '..' in pattern.replace('\\', '/').split('/'):
                raise ValueError('约束路径必须是项目内的相对 glob')
        if self.allowed_actions is not None and not {'navigate', 'finish'} <= set(self.allowed_actions):
            raise ValueError('动作约束必须保留 navigate 和 finish')
        if not (self.include_paths or self.exclude_paths or self.max_files_changed is not None
                or self.max_lines_changed is not None or self.allowed_actions is not None):
            raise ValueError('必须提供可强制执行的收窄约束')
        return self


class Guidance(Contract):
    # hint 提供上下文，constraint 强制收窄，retarget 须确认后派生新的 Run。
    id: str = Field(default_factory=lambda: new_id('guidance'))
    run_id: str
    scope_id: str
    author: str = 'user'
    level: Literal['hint', 'constraint', 'retarget'] = 'hint'
    text: str = Field(min_length=1, max_length=2000)
    created_at: float = Field(default_factory=time.time)
    created_phase: Phase | None = None
    applied_step: int | None = None
    applied_call: int | None = None
    expires: Literal['run', 'phase', 'once'] = 'run'
    status: Literal['queued', 'applied', 'acknowledged', 'rejected', 'superseded'] = 'queued'
    constraints: GuidanceConstraints | None = None
    # L3 确认只记录用户意图，父子 Run 状态切换仍由运行时完成。
    confirmed_at: float | None = None
    rejection_reason: str | None = None
    how_applied: str | None = None
    child_run_id: str | None = None


class GUIIssue(Contract):
    title: str = Field(min_length=1, max_length=240)
    expected: str = Field(min_length=1, max_length=1200)
    actual: str = Field(min_length=1, max_length=1200)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)


class Decision(Contract):
    action: BrowserAction
    evidence_refs: list[str] = Field(default_factory=list)
    rule_refs: list[str] = Field(default_factory=list, max_length=50)
    expected_observation: str = ""
    summary: str = Field(default="", max_length=1200)
    guidance_ack: list[GuidanceAck] = Field(default_factory=list)
    issues: list[GUIIssue] = Field(default_factory=list, max_length=20)


class ReproductionPlan(Contract):
    action_indices: list[int] = Field(min_length=1)
    summary: str = Field(min_length=1)


class FileEdit(Contract):
    path: str
    before_hash: str
    content: str = Field(max_length=150_000)


class StagedCandidateRef(Contract):
    ref: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expected_overlay_revision: int = Field(ge=1)
    diff_ref: str = Field(min_length=1)


class PatchProposal(Contract):
    summary: str
    evidence_refs: list[str] = Field(min_length=1)
    rule_refs: list[str] = Field(default_factory=list, max_length=50)
    edits: list[FileEdit] = Field(default_factory=list, max_length=8)
    staged_refs: list[StagedCandidateRef] = Field(default_factory=list, max_length=8)
    guidance_ack: list[GuidanceAck] = Field(default_factory=list)

    @model_validator(mode='after')
    def patch_source(self):
        if not self.edits and not self.staged_refs:
            raise ValueError('补丁必须提供 edits 或 staged_refs')
        if self.edits and self.staged_refs and {edit.path for edit in self.edits} != {
                candidate.path for candidate in self.staged_refs}:
            raise ValueError('物化补丁路径必须与 staged_refs 一致')
        return self


class Validation(Contract):
    kind: Literal["static", "unit", "build", "health", "original", "regression", "behavior"]
    type: Literal["validation"] = "validation"
    passed: bool = Field(strict=True)
    scope_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_manifest: str
    patch_hash: str
    environment_digest: str
    verifier_version: str = SCHEMA_VERSION
    test_spec_hash: str
    artifact_ref: str = Field(min_length=1)
    scenario_id: str | None = None
    scenario_hash: str | None = None
    replay_plan_hash: str = Field(min_length=1)


class RunState(Contract):
    # 状态快照保存引用，完整证据与模型响应存放在对应 artifact 中。
    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id("run"))
    scope_id: str
    goal: str
    job_id: str | None = None
    parent_run_id: str | None = None
    continuation_instruction: str | None = None
    continuation_count: int = 0
    abnormal_termination: bool = False
    continuation_markers: list[dict] = Field(default_factory=list)
    remote_config: dict | None = None
    url: str
    mode: Literal["test", "repair"] = "test"
    execution_mode: Literal["interactive", "batch"] = "interactive"
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
    exploration_plan_ref: str | None = None
    reproduction_plan_frozen: bool = False
    skills_loaded: list[dict] = Field(default_factory=list)
    task_board_ref: str | None = None
    todo_list_ref: str | None = None
    observation_ref: str | None = None
    reproduction_binding_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    hypothesis_refs: list[str] = Field(default_factory=list)
    working_set_refs: list[str] = Field(default_factory=list)
    patch_ref: str | None = None
    staged_candidate_refs: list[dict] = Field(default_factory=list)
    patch_hash: str | None = None
    patch_base_commit: str | None = Field(default=None, pattern=r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$')
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
    behavior_check_refs: list[str] = Field(default_factory=list)
    error: str | None = None
    error_details: dict | None = None
    diagnosis_retry_count: int = 0
    diagnosis_feedback_refs: list[str] = Field(default_factory=list)
    failed_candidate_signatures: list[dict] = Field(default_factory=list)
    local_branch: str | None = None
    action_fingerprints: list[str] = Field(default_factory=list)
    loop_state_fingerprints: list[str] = Field(default_factory=list)
    loop_error_signatures: list[str] = Field(default_factory=list)
    loop_no_progress_steps: int = 0
    loop_warnings: list[dict] = Field(default_factory=list)
    loop_evidence: dict | None = None
    loop_cause: str | None = None
    recovery_episodes: list[dict] = Field(default_factory=list)
    last_error_signature: str | None = None
    baseline_validation_refs: list[str] = Field(default_factory=list)
    model_exchange_refs: list[str] = Field(default_factory=list)
    # 模型实际返回的 reasoning 独立保存，便于审计且避免扩大状态快照。
    reasoning_refs: list[str] = Field(default_factory=list)
    context_manifest_refs: list[str] = Field(default_factory=list)
    compaction_refs: list[str] = Field(default_factory=list)
    working_memory_ref: str | None = None
    context_phase: str | None = None
    context_compaction_cursor: int = 0
    rule_snapshot_ref: str | None = None
    rule_snapshot_hash: str = ""
    rule_refs: list[dict] = Field(default_factory=list)
    additional_rule_ids: list[str] = Field(default_factory=list)
    guidance: list[Guidance] = Field(default_factory=list)
    # 已应用的强制约束持续参与动作/补丁检查，不因模型确认而解除。
    guidance_constraints: list[GuidanceConstraints] = Field(default_factory=list)
    superseded_by_run_id: str | None = None
    inherited_evidence_refs: list[str] = Field(default_factory=list)


TRANSITIONS = {
    Phase.PREPARE: {Phase.EXPLORE, Phase.FINALIZE},
    Phase.EXPLORE: {Phase.REPRODUCE, Phase.FINALIZE},
    Phase.REPRODUCE: {Phase.EXPLORE, Phase.DIAGNOSE, Phase.FINALIZE},
    Phase.DIAGNOSE: {Phase.PATCH, Phase.FINALIZE},
    Phase.PATCH: {Phase.DIAGNOSE, Phase.VERIFY, Phase.FINALIZE},
    Phase.VERIFY: {Phase.DIAGNOSE, Phase.REVIEW, Phase.FINALIZE},
    Phase.REVIEW: {Phase.FINALIZE},
    Phase.FINALIZE: set(),
}
IMMUTABLE = {"run_id", "scope_id", "schema_version", "mode", "goal", "url"}


def reduce_state(state: RunState, expected_revision: int, **delta) -> RunState:
    # revision 防止并发更新覆盖；终止 Run（含已被改目标替代的 Run）不可再推进。
    if state.revision != expected_revision:
        raise ValueError("版本已过期")
    if state.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                            RunStatus.FAILED, RunStatus.SUPERSEDED}:
        raise ValueError("终止状态不可变更")
    if IMMUTABLE & delta.keys() or "revision" in delta:
        raise ValueError("该状态字段不可变更")
    if state.test_spec_ref and any(delta.get(field, getattr(state, field)) != getattr(state, field)
                                   for field in ('test_spec_ref', 'test_spec_hash')):
        raise ValueError('已冻结的 TestSpec 不可变更')
    if state.patch_base_commit and delta.get('patch_base_commit', state.patch_base_commit) != state.patch_base_commit:
        raise ValueError('已固定的补丁基线不可变更')
    if state.patch_hash and not state.test_spec_ref and delta.get('test_spec_ref'):
        raise ValueError('修补后不能补充冻结 TestSpec')
    if state.patch_hash and any(delta.get(field, getattr(state, field)) != getattr(state, field)
                               for field in ('replay_plan_ref', 'exploration_plan_ref', 'reproduction_plan_frozen')):
        raise ValueError('修补后不能更改冻结复现计划')
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
        delta["behavior_check_refs"] = []
    return RunState.model_validate({**state.model_dump(), **delta, "revision": state.revision + 1})


def verification_gate(state: RunState, validations: list[Validation], artifact_exists, *,
                      artifact_read=None, artifact_read_bytes=None) -> bool:
    from tracefix.runtime.verification import verify_artifacts

    # passed 是生产者声明，不是证明；门禁须核验类型、当前运行绑定与原始证据，并从快照重算 GUI 断言。
    # 只有引用存在还不够，缺少 JSON 或截图字节读取器时必须拒绝放行。
    return verify_artifacts(state, validations, artifact_exists, artifact_read, artifact_read_bytes)
