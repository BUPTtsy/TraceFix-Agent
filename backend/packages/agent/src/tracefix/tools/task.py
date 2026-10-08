"""Run-scoped task and todo tools.

The task board is deliberately stored as a private artifact rather than as
evidence.  It is planning state owned by one Run and must not influence phase
transitions, evidence gates, or a worker's file permissions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, model_validator

from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.tools.effects import apply_effect_receipt
from tracefix.tools.core import ToolRejected


TaskStatus = Literal["pending", "in_progress", "completed"]
MAX_TASKS = 200
MAX_METADATA_BYTES = 16000


def _validate_metadata(metadata):
    try:
        encoded = json.dumps(metadata or {}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("任务 metadata 必须是有限的 JSON 数据") from error
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("任务 metadata 超出字节上限")


class TaskCreateInput(Contract):
    subject: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=8000)
    activeForm: str | None = Field(default=None, min_length=1, max_length=300)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_metadata(self):
        _validate_metadata(self.metadata)
        return self


class TaskGetInput(Contract):
    taskId: str = Field(min_length=1, max_length=160)


class TaskListInput(Contract):
    pass


class TaskUpdateInput(Contract):
    taskId: str = Field(min_length=1, max_length=160)
    subject: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, min_length=1, max_length=8000)
    activeForm: str | None = Field(default=None, min_length=1, max_length=300)
    status: TaskStatus | None = None
    addBlocks: list[str] | None = Field(default=None, max_length=200)
    addBlockedBy: list[str] | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=160)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_relationship_lists(self):
        for values in (self.addBlocks, self.addBlockedBy):
            if values is not None and len(values) != len(set(values)):
                raise ValueError("任务依赖列表不能重复")
        _validate_metadata(self.metadata)
        return self


class TodoItem(Contract):
    content: str = Field(min_length=1, max_length=1000)
    status: TaskStatus
    activeForm: str = Field(min_length=1, max_length=300)


class TodoWriteInput(Contract):
    todos: list[TodoItem] = Field(max_length=200)

    @model_validator(mode="after")
    def validate_progress(self):
        if sum(item.status == "in_progress" for item in self.todos) > 1:
            raise ValueError("Todo 列表最多只能有一项 in_progress")
        return self


class TaskSummary(Contract):
    id: str
    subject: str
    description: str
    activeForm: str | None = None
    status: TaskStatus
    owner: str | None = None
    blocks: list[str] = Field(default_factory=list)
    blockedBy: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCreateOutput(Contract):
    task: TaskSummary


class TaskGetOutput(Contract):
    task: TaskSummary | None


class TaskListOutput(Contract):
    tasks: list[TaskSummary]


class TaskUpdateOutput(Contract):
    success: bool
    taskId: str
    updatedFields: list[str] = Field(default_factory=list)
    error: str | None = None
    statusChange: dict[str, str] | None = None


class TodoWriteOutput(Contract):
    oldTodos: list[TodoItem]
    newTodos: list[TodoItem]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_ref(state, name: str) -> str | None:
    value = getattr(state, name, None)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ToolRejected(f"{name} 不是有效的 artifact 引用")
    return value


def _load_json(engine, state, field: str, default: dict):
    reference = _state_ref(state, field)
    if not reference:
        return default.copy()
    try:
        value = engine.artifacts.json(state.scope_id, state.run_id, reference)
    except (OSError, PermissionError, ValueError, TypeError) as error:
        raise ToolRejected(f"{field} artifact 无法读取或校验：{error}") from error
    if not isinstance(value, dict):
        raise ToolRejected(f"{field} artifact 格式无效")
    if (
        value.get("scope_id") != state.scope_id or value.get("run_id") != state.run_id
    ):
        raise ToolRejected(f"{field} artifact 不属于当前 Run")
    if type(value.get("revision", 0)) is not int or value.get("revision", 0) < 0:
        raise ToolRejected(f"{field} artifact revision 无效")
    return value


def _save_json(engine, state, field: str, value, label: str):
    with apply_effect_receipt():
        value = {**value, "revision": value.get("revision", 0) + 1}
        reference = engine.artifacts.put(
            state.scope_id, state.run_id, value, "json", label=label
        )
        updated = state.model_copy(update={field: reference})
        engine.store.save(updated)
        setattr(state, field, reference)
    return reference


def _task_dict(task: dict) -> TaskSummary:
    return TaskSummary.model_validate(
        {
            "id": task["id"],
            "subject": task["subject"],
            "description": task["description"],
            "activeForm": task.get("activeForm"),
            "status": task["status"],
            "owner": task.get("owner"),
            "blocks": list(task.get("blocks", [])),
            "blockedBy": list(task.get("blockedBy", [])),
            "metadata": dict(task.get("metadata", {})),
        }
    )


def _task_board(engine, state) -> dict:
    board = _load_json(
        engine,
        state,
        "task_board_ref",
        {"version": 1, "scope_id": state.scope_id, "run_id": state.run_id, "tasks": {}, "create_calls": {}},
    )
    if board.get("version") != 1 or board.get("scope_id") != state.scope_id or board.get("run_id") != state.run_id:
        raise ToolRejected("任务板 artifact 版本或归属无效")
    if not isinstance(board.get("tasks"), dict) or not isinstance(board.get("create_calls"), dict):
        raise ToolRejected("任务板 artifact 结构无效")
    if len(board["tasks"]) > MAX_TASKS:
        raise ToolRejected("任务板超出任务数量上限")
    try:
        for task_id, task in board["tasks"].items():
            if not isinstance(task, dict) or task.get("id") != task_id:
                raise ValueError("任务标识与任务板不一致")
            _task_dict(task)
        for creation in board["create_calls"].values():
            if (
                not isinstance(creation, dict)
                or not isinstance(creation.get("input_hash"), str)
                or creation.get("task_id") not in board["tasks"]
            ):
                raise ValueError("任务创建回执无效")
    except (KeyError, TypeError, ValueError) as error:
        raise ToolRejected("任务板 artifact 内容无效") from error
    return board


def _assert_dependencies_ready(board: dict, task: dict):
    missing = [key for key in task.get("blockedBy", []) if key not in board["tasks"]]
    if missing:
        raise ToolRejected("任务依赖不存在：" + ", ".join(missing))
    blocked = [
        key for key in task.get("blockedBy", [])
        if board["tasks"][key].get("status") != "completed"
    ]
    if blocked and task.get("status") in {"in_progress", "completed"}:
        raise ToolRejected("任务仍被未完成依赖阻塞：" + ", ".join(blocked))


def _assert_acyclic(board: dict):
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str):
        if task_id in visiting:
            raise ToolRejected("任务依赖形成环")
        if task_id in visited:
            return
        visiting.add(task_id)
        task = board["tasks"][task_id]
        for child in task.get("blocks", []):
            if child not in board["tasks"]:
                raise ToolRejected("任务依赖不存在：" + child)
            visit(child)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in board["tasks"]:
        visit(task_id)


def _task_key(arguments: TaskCreateInput) -> str:
    return digest(arguments.model_dump(mode="json"))


def _planning_key(field: str, state, arguments) -> str:
    return digest(
        [
            field,
            _state_ref(state, "task_board_ref" if field == "task" else "todo_list_ref"),
            arguments.model_dump(mode="json"),
        ]
    )


def register_task_tools(engine, state, context, bind):
    """Register supervisor-only Run planning tools."""
    if (context or {}).get("worker_depth") == 1 or getattr(engine, "subagent_depth", 0) == 1:
        return

    async def task_create(arguments: TaskCreateInput, call_id: str):
        board = _task_board(engine, state)
        input_hash = _task_key(arguments)
        previous = board["create_calls"].get(call_id)
        if previous:
            if previous.get("input_hash") != input_hash:
                raise ToolRejected("同一 call_id 不可创建不同任务")
            return TaskCreateOutput(task=_task_dict(board["tasks"][previous["task_id"]]))
        for existing in board["tasks"].values():
            if (
                existing.get("subject") == arguments.subject
                and existing.get("description") == arguments.description
                and existing.get("activeForm") == arguments.activeForm
                and existing.get("status") == "pending"
                and existing.get("metadata", {}) == (arguments.metadata or {})
            ):
                board["create_calls"][call_id] = {"input_hash": input_hash, "task_id": existing["id"]}
                _save_json(engine, state, "task_board_ref", board, "运行任务板")
                return TaskCreateOutput(task=_task_dict(existing))
        if len(board["tasks"]) >= MAX_TASKS:
            raise ToolRejected("任务板已达到任务数量上限")
        task_id = "task_" + digest([state.scope_id, state.run_id, call_id])[:24]
        task = {
            "id": task_id,
            "subject": arguments.subject,
            "description": arguments.description,
            "activeForm": arguments.activeForm,
            "status": "pending",
            "owner": None,
            "blocks": [],
            "blockedBy": [],
            "metadata": arguments.metadata or {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        board["tasks"][task_id] = task
        board["create_calls"][call_id] = {"input_hash": input_hash, "task_id": task_id}
        _save_json(engine, state, "task_board_ref", board, "运行任务板")
        return TaskCreateOutput(task=_task_dict(task))

    async def task_get(arguments: TaskGetInput, call_id: str):
        board = _task_board(engine, state)
        task = board["tasks"].get(arguments.taskId)
        return TaskGetOutput(task=_task_dict(task) if task else None)

    async def task_list(arguments: TaskListInput, call_id: str):
        board = _task_board(engine, state)
        return TaskListOutput(tasks=[_task_dict(task) for task in board["tasks"].values()])

    async def task_update(arguments: TaskUpdateInput, call_id: str):
        board = _task_board(engine, state)
        current = board["tasks"].get(arguments.taskId)
        if current is None:
            return TaskUpdateOutput(success=False, taskId=arguments.taskId, error="Task not found")
        task = dict(current)
        updated: list[str] = []
        if arguments.subject is not None and arguments.subject != task["subject"]:
            task["subject"] = arguments.subject
            updated.append("subject")
        if arguments.description is not None and arguments.description != task["description"]:
            task["description"] = arguments.description
            updated.append("description")
        if arguments.activeForm is not None and arguments.activeForm != task.get("activeForm"):
            task["activeForm"] = arguments.activeForm
            updated.append("activeForm")
        if arguments.owner is not None and arguments.owner != task.get("owner"):
            task["owner"] = arguments.owner
            updated.append("owner")
        if arguments.metadata is not None:
            metadata = dict(task.get("metadata", {}))
            for key, value in arguments.metadata.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            if metadata != task.get("metadata", {}):
                try:
                    _validate_metadata(metadata)
                except ValueError as error:
                    raise ToolRejected(str(error)) from error
                task["metadata"] = metadata
                updated.append("metadata")
        for target in arguments.addBlocks or []:
            if target == task["id"]:
                raise ToolRejected("任务不能阻塞自身")
            if target not in board["tasks"]:
                raise ToolRejected("任务依赖不存在：" + target)
            if target not in task.setdefault("blocks", []):
                task["blocks"].append(target)
                if task["id"] not in board["tasks"][target].setdefault("blockedBy", []):
                    board["tasks"][target]["blockedBy"].append(task["id"])
                updated.append("addBlocks")
        for target in arguments.addBlockedBy or []:
            if target == task["id"]:
                raise ToolRejected("任务不能依赖自身")
            if target not in board["tasks"]:
                raise ToolRejected("任务依赖不存在：" + target)
            if target not in task.setdefault("blockedBy", []):
                task["blockedBy"].append(target)
                if task["id"] not in board["tasks"][target].setdefault("blocks", []):
                    board["tasks"][target]["blocks"].append(task["id"])
                updated.append("addBlockedBy")
        previous_status = task["status"]
        if arguments.status is not None and arguments.status != previous_status:
            task["status"] = arguments.status
            updated.append("status")
        board["tasks"][task["id"]] = task
        _assert_acyclic(board)
        for item in board["tasks"].values():
            _assert_dependencies_ready(board, item)
        task["updated_at"] = _now()
        board["tasks"][task["id"]] = task
        if updated:
            _save_json(engine, state, "task_board_ref", board, "运行任务板")
        return TaskUpdateOutput(
            success=True,
            taskId=task["id"],
            updatedFields=list(dict.fromkeys(updated)),
            statusChange=(
                {"from": previous_status, "to": task["status"]}
                if previous_status != task["status"]
                else None
            ),
        )

    async def todo_write(arguments: TodoWriteInput, call_id: str):
        old = _load_json(engine, state, "todo_list_ref", {
            "version": 1, "scope_id": state.scope_id, "run_id": state.run_id, "items": [],
        })
        try:
            if old.get("version") != 1 or not isinstance(old.get("items"), list):
                raise ValueError("待办清单结构无效")
            old_items = TodoWriteInput.model_validate({"todos": old["items"]}).todos
        except (TypeError, ValueError) as error:
            raise ToolRejected("待办清单 artifact 内容无效") from error
        new_items = list(arguments.todos)
        _save_json(
            engine,
            state,
            "todo_list_ref",
            {
                "version": 1,
                "revision": old.get("revision", 0),
                "scope_id": state.scope_id,
                "run_id": state.run_id,
                "items": [item.model_dump(mode="json") for item in new_items],
            },
            "运行待办清单",
        )
        return TodoWriteOutput(oldTodos=old_items, newTodos=new_items)

    bind(
        "TaskCreate", "在当前 Run 任务板创建一个可追踪任务。", TaskCreateInput,
        task_create, phases=set(Phase), side_effect="write", parallel_safe=False,
        output_model=TaskCreateOutput,
        idempotency_key=lambda value: _planning_key("task", state, value),
    )
    bind(
        "TaskGet", "读取当前 Run 任务板中的一个任务。", TaskGetInput,
        task_get, phases=set(Phase), side_effect="read", parallel_safe=True,
        output_model=TaskGetOutput,
    )
    bind(
        "TaskList", "列出当前 Run 任务板中的任务。", TaskListInput,
        task_list, phases=set(Phase), side_effect="read", parallel_safe=True,
        output_model=TaskListOutput,
    )
    bind(
        "TaskUpdate", "更新任务状态、负责人或依赖关系。", TaskUpdateInput,
        task_update, phases=set(Phase), side_effect="write", parallel_safe=False,
        output_model=TaskUpdateOutput,
        idempotency_key=lambda value: _planning_key("task", state, value),
    )
    bind(
        "TodoWrite", "覆盖当前 Run 的待办清单。", TodoWriteInput,
        todo_write, phases=set(Phase), side_effect="write", parallel_safe=False,
        output_model=TodoWriteOutput,
        idempotency_key=lambda value: _planning_key("todo", state, value),
    )


__all__ = [
    "TaskCreateInput", "TaskGetInput", "TaskListInput", "TaskUpdateInput",
    "TodoItem", "TodoWriteInput", "TaskCreateOutput", "TaskGetOutput",
    "TaskListOutput", "TaskUpdateOutput", "TodoWriteOutput", "register_task_tools",
]
