from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, model_validator

from tracefix.runtime.contracts import Contract, new_id


Lifecycle = Literal['CREATED', 'RUNNING', 'STOPPING', 'STOPPED', 'COMPLETED', 'BLOCKED', 'FAILED']
ExecutionStatus = Literal['PREPARED', 'STARTING', 'RUNNING', 'SUCCEEDED', 'INTERRUPTING', 'STOPPED', 'FAILED', 'UNKNOWN']


class HarnessJob(Contract):
    schema_version: Literal['harness/1'] = 'harness/1'
    job_id: str = Field(default_factory=lambda: new_id('hjob'))
    scope_id: str
    repo_id: str
    console_task_id: str
    goal_hash: str
    origin_base_commit: str
    accepted_commit: str = ''
    acceptance_refs: list[str]
    backend: Literal['claude_sdk'] = 'claude_sdk'
    local_commit_authorized: bool = False
    plan_version: int = 0
    revision: int = 0
    status: Lifecycle = 'CREATED'
    owner: str | None = None
    owner_epoch: int = 0
    stop_request_id: str | None = None
    blocker: str | None = None
    initialization_ref: str | None = None
    plan_ref: str | None = None
    quality: Literal['RECOVERABLE', 'INCREMENT_VERIFIED', 'MERGE_READY'] = 'RECOVERABLE'
    final_verification_ref: str | None = None
    final_source_manifest: str | None = None
    final_patch_hash: str | None = None
    final_target_commit: str | None = None
    required_target_commit: str | None = None
    config_ref: str | None = None
    config_digest: str = ''

    @model_validator(mode='after')
    def initial_commit(self):
        if not self.accepted_commit:
            self.accepted_commit = self.origin_base_commit
        if not self.acceptance_refs or len(self.acceptance_refs) != len(set(self.acceptance_refs)):
            raise ValueError('目标必须包含不重复的冻结验收项')
        return self


class HarnessWorkItem(Contract):
    schema_version: Literal['harness/1'] = 'harness/1'
    harness_task_id: str = Field(default_factory=lambda: new_id('hwork'))
    job_id: str
    scope_id: str
    title: str
    acceptance_refs: list[str]
    depends_on: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    writable_files: list[str] = Field(default_factory=list)
    status: Literal['PENDING', 'READY', 'LEASED', 'CANDIDATE', 'VERIFIED', 'BLOCKED'] = 'PENDING'
    revision: int = 0
    lease_owner: str | None = None
    lease_epoch: int = 0
    lease_expires_at: float | None = None
    verification_ref: str | None = None

    @model_validator(mode='after')
    def validate_scope(self):
        if not self.acceptance_refs:
            raise ValueError('工作项缺少可执行验收引用')
        if not set(self.writable_files) <= set(self.allowed_files):
            raise ValueError('可写范围必须属于授权范围')
        for path in self.allowed_files:
            if not path or '\\' in path or ':' in path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise ValueError('文件范围必须是安全的相对路径')
        return self


class HarnessSession(Contract):
    schema_version: Literal['harness/1'] = 'harness/1'
    session_id: str = Field(default_factory=lambda: new_id('hsession'))
    job_id: str
    scope_id: str
    kind: Literal['initializer', 'coder']
    harness_task_id: str | None = None
    run_id: str
    input_commit: str
    status: Lifecycle = 'CREATED'
    revision: int = 0
    owner: str | None = None
    owner_epoch: int = 0
    invocation_no: int = 0
    feedback_ref: str | None = None
    handoff_ref: str | None = None
    verification_ref: str | None = None
    candidate_commit: str | None = None
    source_manifest: str = ''
    patch_hash: str = ''
    environment_digest: str = ''
    test_spec_hash: str = ''
    check_plan_hash: str = ''

    @model_validator(mode='after')
    def kind_identity(self):
        if (self.kind == 'initializer') != (self.harness_task_id is None):
            raise ValueError('initializer 不关联工作项，coder 必须关联工作项')
        return self


class ExecutionAttempt(Contract):
    attempt_no: int
    owner_epoch: int
    reason: str
    sdk_session_id: str | None = None
    checkpoint_ref: str | None = None


class SdkExecution(Contract):
    schema_version: Literal['harness/1'] = 'harness/1'
    execution_id: str = Field(default_factory=lambda: new_id('sdkexec'))
    job_id: str
    scope_id: str
    session_id: str
    node_kind: str
    logical_invocation_id: str
    invocation_no: int
    input_hash: str
    status: ExecutionStatus = 'PREPARED'
    revision: int = 0
    owner: str | None = None
    owner_epoch: int = 0
    attempt_no: int = 1
    attempt_history: list[ExecutionAttempt] = Field(default_factory=list)
    sdk_session_id: str | None = None
    sdk_version: str = ''
    cli_version: str = ''
    model_id: str = ''
    workspace_ref: str | None = None
    result_ref: str | None = None
    checkpoint_ref: str | None = None
    transcript_ref: str | None = None
    observed_usage: dict = Field(default_factory=dict)


class ArtifactIdentity(Contract):
    schema_version: Literal['harness/1'] = 'harness/1'
    scope_id: str
    job_id: str
    session_id: str
    run_id: str


class InitializationManifest(ArtifactIdentity):
    kind: Literal['initialization_manifest'] = 'initialization_manifest'
    goal_hash: str
    policy_version: str
    input_commit: str
    environment_digest: str
    commands: list[dict]
    healthy: bool
    known_failures: list[str] = Field(default_factory=list)
    versions: dict = Field(default_factory=dict)


class TaskPlan(ArtifactIdentity):
    kind: Literal['task_plan'] = 'task_plan'
    plan_version: int = Field(ge=1)
    acceptance_refs: list[str]
    work_items: list[HarnessWorkItem]

    @model_validator(mode='after')
    def validate_dependencies(self):
        items = {item.harness_task_id: item for item in self.work_items}
        if len(items) != len(self.work_items) or not items:
            raise ValueError('计划必须包含 ID 唯一的工作项')
        visited, active = set(), set()

        def visit(task_id):
            if task_id not in items or task_id in active:
                raise ValueError('工作项依赖缺失或成环')
            if task_id in visited:
                return
            active.add(task_id)
            for dependency in items[task_id].depends_on:
                visit(dependency)
            active.remove(task_id)
            visited.add(task_id)

        for item in items.values():
            if item.job_id != self.job_id or item.scope_id != self.scope_id:
                raise ValueError('工作项不属于计划的 Job/scope')
            visit(item.harness_task_id)
        if set(self.acceptance_refs) != {ref for item in items.values() for ref in item.acceptance_refs}:
            raise ValueError('计划必须完整覆盖冻结目标验收项')
        return self


class SessionHandoff(ArtifactIdentity):
    kind: Literal['session_handoff'] = 'session_handoff'
    input_commit: str
    candidate_commit: str
    source_manifest: str
    patch_hash: str
    verification_ref: str
    completed: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    failed_attempt_refs: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    environment_digest: str
    pending_operations: list[str] = Field(default_factory=list)


class ExecutionCheckpoint(ArtifactIdentity):
    kind: Literal['execution_checkpoint'] = 'execution_checkpoint'
    execution_id: str
    owner_epoch: int
    status: ExecutionStatus
    workspace_snapshot_ref: str
    transcript_ref: str | None = None
    sdk_session_id: str | None = None
    event_cursor: int = 0
    recovery_reason: str | None = None
    pending_operations: list[str] = Field(default_factory=list)
    writers_alive: bool
    artifacts_consistent: bool


class VerificationCommand(Contract):
    command: list[str]
    exit_code: StrictInt
    evidence_ref: str


class VerificationEvidence(ArtifactIdentity):
    kind: Literal['verification_evidence'] = 'verification_evidence'
    candidate_commit: str
    source_manifest: str
    patch_hash: str
    environment_digest: str
    test_spec_hash: str
    check_plan_hash: str
    command: list[str]
    exit_code: StrictInt
    receipt_refs: list[str] = Field(default_factory=list)


class VerificationRecord(ArtifactIdentity):
    kind: Literal['verification_record'] = 'verification_record'
    candidate_commit: str
    input_commit: str
    source_manifest: str
    patch_hash: str
    environment_digest: str
    test_spec_hash: str
    check_plan_hash: str
    acceptance_refs: list[str]
    commands: list[VerificationCommand]
    passed: StrictBool
    reproduction_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)
    target_commit: str | None = None


class HarnessFeedback(ArtifactIdentity):
    kind: Literal['harness_feedback'] = 'harness_feedback'
    execution_id: str
    candidate_commit: str | None = None
    source_manifest: str
    verification_ref: str
    summary: str


class HarnessEvent(Contract):
    event_id: str = Field(default_factory=lambda: new_id('hevent'))
    job_id: str
    scope_id: str
    seq: int
    owner_epoch: int
    type: str
    revision: int
    payload: dict
