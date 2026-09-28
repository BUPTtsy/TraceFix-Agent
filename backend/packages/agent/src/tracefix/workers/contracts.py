"""Contracts for dynamically delegated supervisor workers.

The worker package deliberately keeps the task contract independent from the
phase engine.  A supervisor may invent a role label for every task, while the
runtime still validates the tools, files, shell mode, and retry policy that
the worker is allowed to use.
"""
from __future__ import annotations

import time
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = "tracefix/workers/1"


def new_worker_id(prefix: str = "worker") -> str:
    return f"{prefix}_{uuid4().hex}"


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("worker path must be a non-empty string")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized.startswith("/") or ":" in path.parts[0] or ".." in path.parts:
        raise ValueError(f"worker path must be relative: {value!r}")
    if "." in path.parts:
        normalized = "/".join(part for part in path.parts if part != ".")
    return normalized


class WorkerStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class WorkerEventType(StrEnum):
    CREATED = "worker.created"
    QUEUED = "worker.queued"
    STARTED = "worker.started"
    PROGRESS = "worker.progress"
    RETRYING = "worker.retrying"
    COMPLETED = "worker.completed"
    PARTIAL = "worker.partial"
    FAILED = "worker.failed"
    CANCELLED = "worker.cancelled"
    EXPIRED = "worker.expired"
    RESULT_PERSISTED = "worker.result.persisted"
    JOINED = "workers.joined"


class WorkerTool(StrEnum):
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    CODE_READ = "code.read"
    CODE_REFERENCES = "code.references"
    CODE_WRITE = "code.write"
    SHELL = "shell"
    SHELL_READONLY = "shell.readonly"
    SHELL_PATCH = "shell.patch"
    NETWORK = "network"
    BROWSER = "browser"


class WorkerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class WorkerTask(WorkerContract):
    """Immutable authorization and execution description for one worker.

    ``role`` and ``prompt`` are intentionally free-form.  The supervisor may
    create a role such as ``代码探索者 2`` for one run and ``浏览器操作者`` for
    another, but tools and paths remain explicit runtime checked fields.
    """

    schema_version: str = SCHEMA_VERSION
    task_id: str = Field(default_factory=lambda: new_worker_id("task"))
    run_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    phase: str = Field(min_length=1, max_length=64)
    role: str = Field(min_length=1, max_length=160)
    role_label: str | None = Field(default=None, max_length=160)
    goal: str = Field(
        min_length=12,
        max_length=8_000,
        validation_alias=AliasChoices("goal", "description", "objective"),
    )
    prompt: str = Field(min_length=20, max_length=40_000)
    expected_output: str = Field(default="", max_length=4_000)
    completion_criteria: list[str] = Field(default_factory=list, max_length=30)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    allowed_files: list[str] = Field(default_factory=list, max_length=2_000)
    allowed_artifacts: list[str] = Field(default_factory=list, max_length=2_000)
    tools: list[str] = Field(
        default_factory=list,
        max_length=64,
        validation_alias=AliasChoices("tools", "allowed_tools"),
    )
    writable_files: list[str] = Field(default_factory=list, max_length=2_000)
    depends_on: list[str] = Field(default_factory=list, max_length=2_000)
    shell_mode: Literal["disabled", "readonly", "patch"] = "disabled"
    retry_limit: int = Field(default=3, ge=0, le=3)
    timeout_seconds: float | None = Field(default=None, gt=0, le=86_400)
    source_revision: int | str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    allow_worker_spawn: Literal[False] = False

    @field_validator("allowed_files", "writable_files")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        result = [_safe_relative_path(value) for value in values]
        if len(set(result)) != len(result):
            raise ValueError("worker paths must not be duplicated")
        return result

    @field_validator("allowed_artifacts", "depends_on")
    @classmethod
    def normalize_refs(cls, values: list[str]) -> list[str]:
        result = [value.strip() for value in values]
        if any(not value for value in result):
            raise ValueError("worker references must not be empty")
        if len(set(result)) != len(result):
            raise ValueError("worker references must not be duplicated")
        return result

    @field_validator("tools")
    @classmethod
    def normalize_tools(cls, values: list[str]) -> list[str]:
        result = [value.strip() for value in values]
        if any(not value for value in result):
            raise ValueError("worker tools must not be empty")
        if len(set(result)) != len(result):
            raise ValueError("worker tools must not be duplicated")
        known = {tool.value for tool in WorkerTool}
        unknown = sorted(set(result) - known)
        if unknown:
            raise ValueError(f"worker tools are not supported: {', '.join(unknown)}")
        return result

    @model_validator(mode="after")
    def validate_authorization(self) -> WorkerTask:
        if self.role_label is None:
            object.__setattr__(self, "role_label", self.role)
        if not set(self.writable_files) <= set(self.allowed_files):
            raise ValueError("writable_files must be a subset of allowed_files")
        if self.shell_mode == "patch" and self.phase.upper() != "PATCH":
            raise ValueError("shell write mode is only available in the PATCH phase")
        if self.shell_mode == "disabled" and {
            WorkerTool.SHELL.value,
            WorkerTool.SHELL_READONLY.value,
            WorkerTool.SHELL_PATCH.value,
        } & set(self.tools):
            raise ValueError("shell tools require shell_mode=readonly or shell_mode=patch")
        if self.shell_mode == "readonly" and WorkerTool.SHELL_PATCH.value in self.tools:
            raise ValueError("shell.patch requires shell_mode=patch")
        if self.shell_mode == "patch" and not (
            {WorkerTool.SHELL.value, WorkerTool.SHELL_PATCH.value} & set(self.tools)
        ):
            raise ValueError("shell_mode=patch requires a shell tool")
        return self

    @property
    def description(self) -> str:
        return self.goal

    @property
    def objective(self) -> str:
        return self.goal

    @property
    def allowed_tools(self) -> list[str]:
        return self.tools

    @property
    def max_attempts(self) -> int:
        return self.retry_limit + 1

    @property
    def shell_writes_allowed(self) -> bool:
        return self.shell_mode == "patch" and self.phase.upper() == "PATCH"

    @classmethod
    def from_delegate_args(
        cls,
        args: dict[str, Any],
        *,
        run_id: str,
        parent_task_id: str | None = None,
        source_revision: int | str | None = None,
    ) -> WorkerTask:
        """Build a task from the strict ``agent.delegate`` tool payload."""

        data = dict(args)
        data["run_id"] = run_id
        data["parent_task_id"] = parent_task_id
        if source_revision is not None:
            data["source_revision"] = source_revision
        else:
            data.pop("source_revision", None)
        if "goal" not in data and "objective" in data:
            data["goal"] = data["objective"]
        if "tools" not in data and "allowed_tools" in data:
            data["tools"] = data["allowed_tools"]
        if "shell_mode" not in data:
            tool_values = set(data.get("tools") or [])
            if "shell.patch" in tool_values:
                data["shell_mode"] = "patch"
            elif {"shell", "shell.readonly"} & tool_values:
                data["shell_mode"] = "readonly"
            else:
                data["shell_mode"] = "disabled"
        if "role_label" not in data and "role" in data:
            data["role_label"] = data["role"]
        criteria = data.get("completion_criteria", [])
        constraints = data.get("constraints", [])
        prompt = str(data.get("prompt", "")).strip()
        if data.get("expected_output"):
            prompt += f"\n\nExpected output:\n{data['expected_output']}"
        if criteria:
            prompt += "\n\nCompletion criteria:\n- " + "\n- ".join(map(str, criteria))
        if constraints:
            prompt += "\n\nAdditional constraints:\n- " + "\n- ".join(map(str, constraints))
        data["prompt"] = prompt
        data.pop("objective", None)
        data.pop("allowed_tools", None)
        return cls.model_validate(data)


class WorkerResult(WorkerContract):
    """Structured result handed back to the supervisor model."""

    schema_version: str = SCHEMA_VERSION
    task_id: str = ""
    worker_id: str = ""
    run_id: str = ""
    phase: str = ""
    role: str = ""
    role_label: str = ""
    status: WorkerStatus = WorkerStatus.SUCCEEDED
    summary: str = Field(
        default="",
        max_length=12_000,
        validation_alias=AliasChoices("summary", "conclusion"),
    )
    evidence_refs: list[str] = Field(default_factory=list, max_length=2_000)
    files_touched: list[str] = Field(
        default_factory=list,
        max_length=2_000,
        validation_alias=AliasChoices("files_touched", "files"),
    )
    suggested_followups: list[str] = Field(
        default_factory=list,
        max_length=100,
        validation_alias=AliasChoices("suggested_followups", "suggested_experiments"),
    )
    unresolved: list[str] = Field(default_factory=list, max_length=100)
    artifacts: list[str] = Field(default_factory=list, max_length=2_000)
    patch_hash: str | None = None
    attempt: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=4, ge=1)
    thread_name: str = ""
    started_at: float | None = None
    finished_at: float | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    thread_id: int | None = None
    error: str | None = Field(default=None, max_length=4_000)
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def conclusion(self) -> str:
        return self.summary

    @property
    def files(self) -> list[str]:
        return self.files_touched

    @property
    def suggested_experiments(self) -> list[str]:
        return self.suggested_followups


class WorkerEvent(WorkerContract):
    """One lifecycle event suitable for CLI and Web trace rendering."""

    schema_version: str = SCHEMA_VERSION
    event_type: WorkerEventType
    timestamp: float = Field(default_factory=time.time)
    worker_id: str
    task_id: str
    run_id: str
    phase: str
    role: str
    role_label: str
    goal: str
    status: WorkerStatus
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=4, ge=1)
    thread_name: str = ""
    thread_id: int | None = None
    summary: str = ""
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def include_trace_fields(self) -> WorkerEvent:
        fields = {
            "event_type": self.event_type.value,
            "type": self.event_type.value,
            "timestamp": self.timestamp,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "role": self.role,
            "role_label": self.role_label,
            "goal": self.goal,
            "status": self.status.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "thread_name": self.thread_name,
            "thread_id": self.thread_id,
            "summary": self.summary,
            "error": self.error,
        }
        object.__setattr__(self, "payload", {**fields, **self.payload})
        return self

    def as_payload(self) -> dict[str, Any]:
        return dict(self.payload)

    @property
    def type(self) -> str:
        return self.event_type.value

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class WorkerSnapshot(WorkerContract):
    """Current scheduler state for a worker card or CLI status line."""

    worker_id: str
    task_id: str
    run_id: str
    phase: str
    role: str
    role_label: str
    goal: str
    status: WorkerStatus
    attempt: int = 0
    max_attempts: int = 4
    thread_name: str = ""
    thread_id: int | None = None
    submitted_at: float | None = None
    started_at: float | None = None
    finished_at: float | None = None
    summary: str = ""
    error: str | None = None
