"""Thread-pool scheduler for dynamic supervisor workers."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .contracts import (
    WorkerEvent,
    WorkerEventType,
    WorkerResult,
    WorkerSnapshot,
    WorkerStatus,
    WorkerTask,
    new_worker_id,
)
from .locks import LockedWorkspace, WorkspaceMutex


class WorkerDelegationError(PermissionError):
    """Raised when a worker attempts to create or dispatch another worker."""


class WorkerCancelled(Exception):
    pass


class WorkerExecutionError(RuntimeError):
    """An attempt may attach partial structured information to this error."""

    def __init__(self, message: str, partial: WorkerResult | Mapping[str, Any] | None = None):
        super().__init__(message)
        self.partial = partial


@dataclass
class WorkerContext:
    task: WorkerTask
    worker_id: str
    attempt: int
    max_attempts: int
    lock_manager: WorkspaceMutex
    workspace: Any
    cancel_event: threading.Event
    _progress: Callable[..., None]

    def __getitem__(self, key: str) -> Any:
        values = {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "thread_name": threading.current_thread().name,
            "thread_id": threading.get_ident(),
            "worker_id": self.worker_id,
            "task_id": self.task.task_id,
        }
        return values[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise WorkerCancelled("worker cancellation requested")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def report_progress(self, summary: str, **payload: Any) -> None:
        self.check_cancelled()
        self._progress(summary, **payload)

    def spawn_worker(self, *args: Any, **kwargs: Any) -> None:
        raise WorkerDelegationError(
            "workers cannot spawn workers; return a follow-up suggestion to the supervisor"
        )

    dispatch_worker = spawn_worker

    def require_tool(self, tool: str) -> None:
        if tool not in self.task.tools:
            raise PermissionError(f"worker tool is not authorized: {tool}")

    def authorize_file(self, path: str, *, write: bool = False) -> None:
        if write and not self.task.write_enabled:
            raise PermissionError('Supervisor must explicitly enable worker writes')
        normalized = path.replace("\\", "/")
        if normalized not in self.task.allowed_files:
            raise PermissionError(f"worker file is not authorized: {path}")
        if write and normalized not in self.task.writable_files:
            raise PermissionError(f"worker write is not authorized: {path}")

    def authorize_shell(self, *, write: bool = False) -> None:
        if not ({"shell", "shell.readonly", "shell.patch"} & set(self.task.tools)):
            raise PermissionError("worker shell tool is not authorized")
        if write:
            if not self.task.write_enabled:
                raise PermissionError('Supervisor must explicitly enable worker writes')
            self.require_tool("shell.patch")
            if not self.task.shell_writes_allowed:
                raise PermissionError("shell writes are allowed only for a PATCH task")

    def authorize_shell_command(self, command: str) -> None:
        self.authorize_shell(write=_shell_command_writes(command))

    def read_file(self, path: str) -> str:
        self.authorize_file(path)
        if self.workspace is None:
            raise RuntimeError("worker workspace is unavailable")
        return self.workspace.read(path)

    def write_file(self, path: str, content: str | bytes) -> Any:
        self.require_tool("file.write")
        self.authorize_file(path, write=True)
        if self.workspace is None:
            raise RuntimeError("worker workspace is unavailable")
        return self.workspace.write(path, content)

    def apply_patch(self, proposal: Any) -> Any:
        self.require_tool("code.write")
        if self.workspace is None:
            raise RuntimeError("worker workspace is unavailable")
        edits = getattr(proposal, "edits", ())
        for edit in edits:
            self.authorize_file(edit.path, write=True)
        if not edits:
            raise ValueError("worker patch must contain edits")
        return self.workspace.apply(proposal)


def _shell_command_writes(command: str) -> bool:
    import re

    patterns = (
        r"(?:^|[;&|]\s*)(?:rm|rmdir|mv|cp|mkdir|touch|chmod|chown)\b",
        r"(?:^|[;&|]\s*)git\s+(?:add|apply|checkout|clean|commit|mv|reset|restore)\b",
        r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|pip)\s+(?:install|ci|add|remove|uninstall)\b",
        r"(?:^|[;&|]\s*)(?:curl|wget)\b[^\n]*\s(?:-o|--output|--output-document)(?:\s|=)",
        r"(?:>>?|\d>>?)\s*[^&|;\n]+",
        r"\b(?:tee|sed\s+-i|perl\s+-i)\b",
    )
    return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in patterns)


class _AuthorizedWorkspace:
    """Workspace facade that applies the task's file authorization first."""

    def __init__(self, context: WorkerContext, delegate: Any):
        self._context = context
        self._delegate = delegate

    def read(self, path: str):
        self._context.authorize_file(path)
        return self._delegate.read(path)

    def write(self, path: str, content: str | bytes):
        self._context.authorize_file(path, write=True)
        return self._delegate.write(path, content)

    def apply(self, proposal):
        edits = getattr(proposal, "edits", ())
        if not edits:
            raise ValueError("worker patch must contain edits")
        for edit in edits:
            self._context.authorize_file(edit.path, write=True)
        return self._delegate.apply(proposal)


@dataclass
class WorkerHandle:
    task: WorkerTask
    future: Future
    worker_id: str
    scheduler: "WorkerScheduler"

    def result(self, timeout: float | None = None) -> WorkerResult:
        return self.future.result(timeout=timeout)

    def done(self) -> bool:
        return self.future.done()

    def cancel(self) -> bool:
        return self.scheduler.cancel(self.task.task_id)

    @property
    def status(self) -> WorkerStatus:
        snapshot = self.scheduler.get_snapshot(self.task.task_id)
        return snapshot.status


@dataclass
class _Record:
    task: WorkerTask
    worker_id: str
    future: Future
    cancel_event: threading.Event
    runner: Callable[..., Any]
    scheduled: bool = False
    status: WorkerStatus = WorkerStatus.CREATED
    attempt: int = 0
    submitted_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    thread_name: str = ""
    thread_id: int | None = None
    summary: str = ""
    error: str | None = None


class WorkerScheduler:
    """Unbounded task queue backed by at most four worker threads."""

    DEFAULT_MAX_CONCURRENCY = 4
    DEFAULT_RETRY_LIMIT = 3

    def __init__(
        self,
        runner: Callable[..., Any] | None = None,
        event_sink: Callable[[WorkerEvent], None] | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        retry_limit: int = DEFAULT_RETRY_LIMIT,
        workspace: Any = None,
        lock_manager: WorkspaceMutex | None = None,
    ) -> None:
        if isinstance(max_concurrency, bool) or isinstance(max_concurrency, float) and not max_concurrency.is_integer():
            raise ValueError("max_concurrency must be an integer between 1 and 4")
        if not 1 <= int(max_concurrency) <= 4:
            raise ValueError("max_concurrency must be between 1 and 4")
        if isinstance(retry_limit, bool) or isinstance(retry_limit, float) and not retry_limit.is_integer():
            raise ValueError("retry_limit must be an integer between 0 and 3")
        if not 0 <= int(retry_limit) <= 3:
            raise ValueError("retry_limit must be between 0 and 3")
        self.runner = runner
        self.event_sink = event_sink
        self.max_concurrency = int(max_concurrency)
        self.retry_limit = int(retry_limit)
        self.executor = ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="tracefix-worker",
        )
        self.lock_manager = lock_manager or WorkspaceMutex(getattr(workspace, "root", None))
        self.workspace = (
            workspace if isinstance(workspace, LockedWorkspace)
            else LockedWorkspace(workspace, self.lock_manager) if workspace is not None
            else None
        )
        self._records: dict[str, _Record] = {}
        self._results: dict[str, WorkerResult] = {}
        self._events: list[WorkerEvent] = []
        self._waiting_for: dict[str, set[str]] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _emit(self, event_type: WorkerEventType, record: _Record, **payload: Any) -> WorkerEvent:
        payload.setdefault("thread_id", threading.get_ident())
        with self._lock:
            event = WorkerEvent(
                event_type=event_type,
                worker_id=record.worker_id,
                task_id=record.task.task_id,
                run_id=record.task.run_id,
                phase=record.task.phase,
                role=record.task.role,
                role_label=record.task.role_label or record.task.role,
                goal=record.task.goal,
                status=payload.pop("status", record.status),
                attempt=payload.pop("attempt", record.attempt),
                max_attempts=payload.pop("max_attempts", record.task.max_attempts),
                thread_name=payload.pop("thread_name", record.thread_name),
                thread_id=payload.pop("thread_id", record.thread_id),
                summary=payload.pop("summary", record.summary),
                error=payload.pop("error", record.error),
                payload=payload,
            )
            self._events.append(event)
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except TypeError:
                self.event_sink(event.event_type.value, event.as_payload())
        return event

    def submit(self, task: WorkerTask, runner: Callable[..., Any] | None = None) -> WorkerHandle:
        if not isinstance(task, WorkerTask):
            task = WorkerTask.model_validate(task)
        selected = runner or self.runner
        if selected is None:
            raise ValueError("a worker runner is required")
        with self._lock:
            if self._closed:
                raise RuntimeError("worker scheduler is closed")
            if task.task_id in self._records:
                raise ValueError(f"worker task_id already exists: {task.task_id}")
            if task.task_id in task.depends_on:
                raise ValueError("worker task cannot depend on itself")
            worker_id = new_worker_id("worker")
            future: Future = Future()
            record = _Record(task, worker_id, future, threading.Event(), selected,
                             submitted_at=time.time())
            self._records[task.task_id] = record
            self._emit(WorkerEventType.CREATED, record, status=WorkerStatus.CREATED)
            record.status = WorkerStatus.QUEUED
            self._emit(
                WorkerEventType.QUEUED,
                record,
                status=WorkerStatus.QUEUED,
                active=self.active_count(),
                max_concurrency=self.max_concurrency,
            )
            for dependency_id in task.depends_on:
                dependency = self._records.get(dependency_id)
                if dependency is None:
                    self._waiting_for.setdefault(dependency_id, set()).add(task.task_id)
                else:
                    dependency.future.add_done_callback(
                        lambda _future, task_id=task.task_id: self._dependencies_ready(task_id))
            self._dependency_available(task.task_id)
            self._schedule_if_ready(record)
            return WorkerHandle(task, future, worker_id, self)

    enqueue = submit

    def _dependency_available(self, dependency_id: str) -> None:
        waiting = self._waiting_for.pop(dependency_id, set())
        if not waiting:
            return
        dependency = self._records.get(dependency_id)
        if dependency is None:
            return
        for task_id in waiting:
            record = self._records.get(task_id)
            if record is None or record.future.done():
                continue
            dependency.future.add_done_callback(
                lambda _future, task_id=task_id: self._dependencies_ready(task_id))
            self._dependencies_ready(task_id)

    def _dependencies_ready(self, task_id: str) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.future.done() or record.scheduled:
                return
            if record.cancel_event.is_set():
                self._finish_cancelled(record, max(1, record.attempt), "worker cancelled while waiting")
                return
            dependencies = [self._records.get(dependency_id)
                            for dependency_id in record.task.depends_on]
            if not all(dependency is not None and dependency.future.done()
                       for dependency in dependencies):
                return
            self._schedule_if_ready(record)

    def _schedule_if_ready(self, record: _Record) -> None:
        if self._closed or record.scheduled or record.future.done() or record.cancel_event.is_set():
            return
        if any(not (dependency := self._records.get(dependency_id))
               or not dependency.future.done()
               for dependency_id in record.task.depends_on):
            return
        record.scheduled = True
        self.executor.submit(self._execute, record, record.runner)

    def _call_runner(self, runner, task, context: WorkerContext):
        value = runner(task, context)
        if inspect.isawaitable(value):
            async def wait_for_result():
                if task.timeout_seconds is None:
                    return await value
                return await asyncio.wait_for(value, timeout=task.timeout_seconds)
            value = asyncio.run(wait_for_result())
        return value

    def _coerce_result(self, value: Any, record: _Record, attempt: int, started: float) -> WorkerResult:
        if isinstance(value, WorkerResult):
            data = value.model_dump(mode="python")
        elif isinstance(value, Mapping):
            data = dict(value)
        elif isinstance(value, str):
            data = {"summary": value}
        else:
            raise TypeError("worker runner must return WorkerResult, mapping, or string")
        data.update({
            "task_id": record.task.task_id,
            "worker_id": record.worker_id,
            "run_id": record.task.run_id,
            "phase": record.task.phase,
            "role": record.task.role,
            "role_label": record.task.role_label or record.task.role,
            "attempt": attempt,
            "max_attempts": record.task.max_attempts,
            "thread_name": threading.current_thread().name,
            "started_at": data.get("started_at") or record.started_at or time.time(),
            "finished_at": time.time(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "thread_id": threading.get_ident(),
        })
        result = WorkerResult.model_validate(data)
        if not set(result.evidence_refs) <= set(record.task.allowed_artifacts):
            raise PermissionError("worker returned an unauthorized artifact reference")
        if not set(result.files_touched) <= set(record.task.allowed_files):
            raise PermissionError("worker returned an unauthorized file reference")
        return result

    def _execute(self, record: _Record, runner: Callable[..., Any]) -> None:
        started = time.monotonic()
        attempts = min(record.task.retry_limit, self.retry_limit) + 1
        last_error: str | None = None
        partial: WorkerResult | Mapping[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            if record.cancel_event.is_set():
                return self._finish_cancelled(record, attempt)
            with self._lock:
                record.status = WorkerStatus.RUNNING
                record.attempt = attempt
                record.started_at = time.time()
                record.thread_name = threading.current_thread().name
                record.thread_id = threading.get_ident()
            self._emit(
                WorkerEventType.STARTED,
                record,
                status=WorkerStatus.RUNNING,
                attempt=attempt,
                max_attempts=attempts,
                thread_name=record.thread_name,
            )
            context = WorkerContext(
                task=record.task,
                worker_id=record.worker_id,
                attempt=attempt,
                max_attempts=attempts,
                lock_manager=self.lock_manager,
                workspace=self.workspace,
                cancel_event=record.cancel_event,
                _progress=lambda summary, **data: self._emit(
                    WorkerEventType.PROGRESS,
                    record,
                    status=WorkerStatus.RUNNING,
                    attempt=attempt,
                    max_attempts=attempts,
                    thread_name=record.thread_name,
                    summary=summary,
                    **data,
                ),
            )
            if self.workspace is not None:
                context.workspace = _AuthorizedWorkspace(context, self.workspace)
            try:
                attempt_started = time.monotonic()
                value = self._call_runner(runner, record.task, context)
                if (record.task.timeout_seconds is not None
                        and time.monotonic() - attempt_started > record.task.timeout_seconds):
                    raise TimeoutError(
                        f"worker attempt exceeded {record.task.timeout_seconds:g} seconds"
                    )
                result = self._coerce_result(value, record, attempt, started)
                if result.status == WorkerStatus.CANCELLED:
                    return self._finish_cancelled(record, attempt, result.error or "worker cancelled")
                if result.status in {WorkerStatus.FAILED, WorkerStatus.EXPIRED}:
                    raise WorkerExecutionError(result.summary or "worker returned failure", result)
                if record.cancel_event.is_set():
                    return self._finish_cancelled(record, attempt)
                return self._finish(record, result)
            except WorkerCancelled as error:
                return self._finish_cancelled(record, attempt, str(error))
            except WorkerExecutionError as error:
                last_error = str(error)
                partial = error.partial
            except BaseException as error:
                last_error = f"{type(error).__name__}: {error}"
            if attempt < attempts:
                with self._lock:
                    record.status = WorkerStatus.RETRYING
                    record.error = last_error
                self._emit(
                    WorkerEventType.RETRYING,
                    record,
                    status=WorkerStatus.RETRYING,
                    attempt=attempt,
                    max_attempts=attempts,
                    error=last_error,
                    next_attempt=attempt + 1,
                )
        if isinstance(partial, WorkerResult):
            data = partial.model_dump(mode="python")
        elif isinstance(partial, Mapping):
            data = dict(partial)
        else:
            data = {}
        if data.get("evidence_refs"):
            data["evidence_refs"] = [
                ref for ref in data["evidence_refs"] if ref in record.task.allowed_artifacts
            ]
        result_files = data.get("files_touched", data.get("files"))
        if result_files:
            data["files_touched"] = [
                path for path in result_files if path in record.task.allowed_files
            ]
        data.update({
            "task_id": record.task.task_id,
            "worker_id": record.worker_id,
            "run_id": record.task.run_id,
            "phase": record.task.phase,
            "role": record.task.role,
            "role_label": record.task.role_label or record.task.role,
            "status": WorkerStatus.PARTIAL,
            "summary": data.get("summary") or "Worker retries exhausted; partial result returned to supervisor.",
            "unresolved": list(data.get("unresolved", [])) + [last_error or "unknown worker error"],
            "suggested_followups": list(data.get("suggested_followups", [])) or [
                "Review the failure summary and dispatch a narrower follow-up task if needed."
            ],
            "attempt": attempts,
            "max_attempts": attempts,
            "thread_name": record.thread_name,
            "thread_id": record.thread_id,
            "started_at": record.started_at,
            "finished_at": time.time(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "error": last_error,
        })
        self._finish(record, WorkerResult.model_validate(data))

    def _finish(self, record: _Record, result: WorkerResult) -> WorkerResult:
        with self._lock:
            record.status = result.status
            record.finished_at = time.time()
            record.summary = result.summary
            record.error = result.error
            self._results[record.task.task_id] = result
            if not record.future.done():
                record.future.set_result(result)
        self._emit(
            WorkerEventType.PARTIAL if result.status == WorkerStatus.PARTIAL else WorkerEventType.COMPLETED,
            record,
            status=result.status,
            attempt=result.attempt,
            max_attempts=result.max_attempts,
            thread_name=result.thread_name,
            summary=result.summary,
            error=result.error,
            duration_ms=result.duration_ms,
            result=result.model_dump(mode="json"),
        )
        return result

    def _finish_cancelled(self, record: _Record, attempt: int, error: str = "worker cancelled") -> WorkerResult:
        with self._lock:
            existing = self._results.get(record.task.task_id)
            if record.future.done() and existing is not None:
                return existing
        result = WorkerResult(
            task_id=record.task.task_id,
            worker_id=record.worker_id,
            run_id=record.task.run_id,
            phase=record.task.phase,
            role=record.task.role,
            role_label=record.task.role_label or record.task.role,
            status=WorkerStatus.CANCELLED,
            summary="Worker cancelled before completing its task.",
            attempt=attempt,
            max_attempts=record.task.max_attempts,
            thread_name=record.thread_name,
            error=error,
        )
        with self._lock:
            record.status = WorkerStatus.CANCELLED
            record.finished_at = time.time()
            record.error = error
            self._results[record.task.task_id] = result
            if not record.future.done():
                record.future.set_result(result)
        self._emit(
            WorkerEventType.CANCELLED,
            record,
            status=WorkerStatus.CANCELLED,
            attempt=attempt,
            max_attempts=record.task.max_attempts,
            thread_name=record.thread_name,
            summary=result.summary,
            error=error,
            duration_ms=result.duration_ms,
        )
        for dependency_id, waiting in list(self._waiting_for.items()):
            waiting.discard(record.task.task_id)
            if not waiting:
                self._waiting_for.pop(dependency_id, None)
        return result

    async def run_async(self, task: WorkerTask, runner: Callable[..., Any] | None = None) -> WorkerResult:
        return await self.wait(self.submit(task, runner))

    async def wait(self, handles, timeout: float | None = None):
        multiple = isinstance(handles, (list, tuple, set))
        items = list(handles) if multiple else [handles]
        futures = [asyncio.wrap_future(item.future if isinstance(item, WorkerHandle) else item) for item in items]
        values = await asyncio.wait_for(asyncio.gather(*futures), timeout=timeout) if timeout else await asyncio.gather(*futures)
        return values if multiple else values[0]

    def wait_blocking(self, handles, timeout: float | None = None):
        multiple = isinstance(handles, (list, tuple, set))
        items = list(handles) if multiple else [handles]
        values = [item.result(timeout) if isinstance(item, WorkerHandle) else item.result(timeout) for item in items]
        return values if multiple else values[0]

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.future.done():
                return False
            record.cancel_event.set()
            return True

    def get(self, task_id: str) -> WorkerResult | None:
        with self._lock:
            return self._results.get(task_id)

    def handle(self, task_id: str) -> WorkerHandle:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                raise KeyError(f"unknown worker task: {task_id}")
            return WorkerHandle(record.task, record.future, record.worker_id, self)

    def get_snapshot(self, task_id: str) -> WorkerSnapshot:
        with self._lock:
            record = self._records[task_id]
            return WorkerSnapshot(
                worker_id=record.worker_id,
                task_id=record.task.task_id,
                run_id=record.task.run_id,
                phase=record.task.phase,
                role=record.task.role,
                role_label=record.task.role_label or record.task.role,
                goal=record.task.goal,
                status=record.status,
                attempt=record.attempt,
                max_attempts=record.task.max_attempts,
                thread_name=record.thread_name,
                thread_id=record.thread_id,
                submitted_at=record.submitted_at,
                started_at=record.started_at,
                finished_at=record.finished_at,
                summary=record.summary,
                error=record.error,
            )

    def snapshot(self) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.snapshots()]

    def snapshots(self) -> list[WorkerSnapshot]:
        with self._lock:
            ids = list(self._records)
        return [self.get_snapshot(task_id) for task_id in ids]

    def events(self) -> tuple[WorkerEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._records.values() if record.status in {
                WorkerStatus.RUNNING,
                WorkerStatus.RETRYING,
            })

    def close(self, *, wait: bool = True, cancel_pending: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if cancel_pending:
                for record in self._records.values():
                    if not record.future.done():
                        record.cancel_event.set()
                        if record.status in {WorkerStatus.CREATED, WorkerStatus.QUEUED}:
                            self._finish_cancelled(record, record.attempt or 1, "scheduler shutdown")
                        if record.started_at is None and not record.future.done():
                            self._finish_cancelled(record, max(1, record.attempt))
            else:
                for record in self._records.values():
                    if not record.future.done() and not record.scheduled:
                        self._finish_cancelled(record, max(1, record.attempt), "scheduler shutdown")
        self.executor.shutdown(wait=wait, cancel_futures=cancel_pending)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.close(wait=wait, cancel_pending=cancel_futures)


__all__ = [
    "WorkerCancelled",
    "WorkerContext",
    "WorkerDelegationError",
    "WorkerExecutionError",
    "WorkerHandle",
    "WorkerScheduler",
]
