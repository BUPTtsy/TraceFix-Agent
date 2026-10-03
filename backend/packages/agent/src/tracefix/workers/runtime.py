"""角色受限的子 Agent 运行时与独立浏览器沙箱工厂。

通用任务仍由 scheduler 调度；本模块增加角色/阶段/工具校验、父子轨迹身份、
取消传播，以及每个浏览器任务独立的 DockerRunner/MCPBrowser。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from tracefix.execution.browser import MCPBrowser
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.runtime.contracts import digest, new_id
from tracefix.workers.contracts import WorkerResult, WorkerStatus, WorkerTask, WorkerTool
from tracefix.workers.scheduler import WorkerContext, WorkerScheduler


# 角色只在声明阶段可用；工具允许集合单独校验，不能由提示词自行扩大。
ROLE_PHASES: dict[str, frozenset[str]] = {
    'planner': frozenset({'PLAN', 'PREPARE'}),
    'gui-scout': frozenset({'DISCOVER', 'EXPLORE'}),
    'code-explorer': frozenset({'DIAGNOSE'}),
    'evidence-reviewer': frozenset({'DIAGNOSE'}),
    'reproducer': frozenset({'DISCOVER', 'REPRODUCE'}),
    'patch-reviewer': frozenset({'REVIEW'}),
    'patch-worker': frozenset({'PATCH'}),
    'test-writer': frozenset({'PATCH'}),
}

ROLE_TOOLS: dict[str, frozenset[str]] = {
    'planner': frozenset({WorkerTool.FILE_READ.value, WorkerTool.CODE_READ.value,
                          WorkerTool.CODE_REFERENCES.value}),
    'gui-scout': frozenset({WorkerTool.BROWSER.value, WorkerTool.NETWORK.value}),
    'code-explorer': frozenset({WorkerTool.FILE_READ.value, WorkerTool.CODE_READ.value,
                                WorkerTool.CODE_REFERENCES.value}),
    'evidence-reviewer': frozenset({WorkerTool.FILE_READ.value}),
    'reproducer': frozenset({WorkerTool.BROWSER.value, WorkerTool.NETWORK.value}),
    'patch-reviewer': frozenset({WorkerTool.FILE_READ.value, WorkerTool.CODE_READ.value,
                                 WorkerTool.CODE_REFERENCES.value}),
    'patch-worker': frozenset({WorkerTool.FILE_READ.value, WorkerTool.FILE_WRITE.value,
                               WorkerTool.CODE_READ.value, WorkerTool.CODE_WRITE.value,
                               WorkerTool.SHELL_PATCH.value}),
    'test-writer': frozenset({WorkerTool.FILE_READ.value, WorkerTool.FILE_WRITE.value,
                              WorkerTool.CODE_READ.value}),
}


def canonical_role(role: str) -> str:
    value = role.casefold().strip().replace('_', '-').replace(' ', '-')
    for known in ROLE_PHASES:
        if value == known or value.startswith(known + '-'):
            return known
    return value


class SubAgentRejected(PermissionError):
    pass


@dataclass(frozen=True)
class SubAgentTrace:
    child_run_id: str
    parent_run_id: str
    parent_step_id: str
    task_id: str
    role: str
    phase: str
    generation: int | str | None
    depth: int
    status: str
    result_ref: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class HierarchyTrace:
    """线程安全的内存轨迹接收器，调用方可把事件持久化为 Run artifact。"""

    def __init__(self, sink: Callable[[dict[str, Any]], Any] | None = None):
        self.sink = sink
        self.records: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def emit(self, event: str, **payload):
        record = {'type': event, **payload}
        with self._lock:
            self.records.append(record)
        if self.sink:
            self.sink(record)
        return record

    def children(self, parent_run_id: str):
        return [item for item in self.records if item.get('parent_run_id') == parent_run_id]


@dataclass
class BrowserSandbox:
    runner: DockerRunner
    browser: MCPBrowser
    started: bool = False

    async def open(self, source_manifest: str):
        self.started = True
        await self.runner.inspect_images()
        health = await self.runner.start(source_manifest)
        if not health.get('passed'):
            raise RuntimeError('子 Agent 应用容器未通过健康检查')
        await self.browser.open()
        return self

    async def close(self):
        try:
            await self.browser.close()
        finally:
            # 浏览器关闭失败也必须回收容器，避免取消或异常后留下子沙箱。
            await self.runner.close()


class BrowserSandboxFactory:
    """为每个子 Run 创建独立网络、应用容器和浏览器进程。"""

    def __init__(self, profile, workspace):
        self.profile, self.workspace = profile, workspace

    def create(self, *, parent_run_id: str, child_run_id: str) -> BrowserSandbox:
        runner = DockerRunner(self.profile, self.workspace, child_run_id)
        browser = MCPBrowser(runner.browser_command(), Policy(self.profile.allowed_origins))
        return BrowserSandbox(runner, browser)


class IsolatedGuiScout:
    """使用独立 Docker 与 LangGraph 身份执行浏览器探索子 Run。"""

    def __init__(self, engine, parent_state, *, factory=None):
        self.engine, self.parent_state = engine, parent_state
        self.factory = factory or BrowserSandboxFactory(engine.profile, engine.workspace)

    async def __call__(self, task, sandbox=None):
        from tracefix.runtime.contracts import BrowserAction, Phase, RunState
        from tracefix.runtime.engine import Engine
        parent = self.parent_state
        if sandbox is None:
            sandbox = self.factory.create(parent_run_id=parent.run_id,
                                          child_run_id=str(task.metadata.get('child_run_id') or new_id('subrun')))
        if not set(task.allowed_artifacts) <= set(parent.evidence_refs +
                                                  ([parent.observation_ref] if parent.observation_ref else [])):
            raise SubAgentRejected('gui-scout 请求了父 Run 范围外的证据')
        child = RunState(run_id=sandbox.runner.run_id if sandbox else new_id('subrun'),
                         scope_id=parent.scope_id, goal=parent.goal, parent_run_id=parent.run_id,
                         url=str(task.metadata.get('url') or parent.url), mode='test',
                         source_manifest=parent.source_manifest)
        # 子 Run 全程使用已启动的独立沙箱，并复制父 Run 的冻结输入供审计。
        child.repo_snapshot_ref = self.engine.artifacts.put(child.scope_id, child.run_id, self.engine.source,
                                                           label='源码快照')
        if parent.test_spec_ref:
            frozen_spec = self.engine.get(parent, parent.test_spec_ref)
            child.test_spec_ref = self.engine.artifacts.put(child.scope_id, child.run_id, frozen_spec,
                                                           label='冻结测试规范')
            child.test_spec_hash = parent.test_spec_hash
        if parent.agent_instructions_ref:
            text = self.engine.agent_instructions(parent)
            child.agent_instructions_ref = self.engine.artifacts.put(child.scope_id, child.run_id,
                                                                     text, 'txt', label='项目约束')
            child.agent_instructions_hash = parent.agent_instructions_hash
            child.agent_instructions_path = parent.agent_instructions_path
        # GUI workers use the isolated Gateway when configured; otherwise retain supervisor behavior.
        model = getattr(self.engine, 'worker_model', None) or self.engine.model
        if hasattr(model, 'student') and model.student:
            model = type(model)(model.teacher, model.student)
        worker = Engine(self.engine.store, self.engine.artifacts, self.engine.scopes,
                        self.engine.context, self.engine.profile, self.engine.workspace,
                        sandbox.runner, sandbox.browser, model, self.engine.retriever,
                        self.engine.source, self.engine.graph.checkpointer,
                        rule_resolver=self.engine.rule_resolver, rule_library=self.engine.rule_library)
        worker.worker_model = getattr(self.engine, 'worker_model', None)
        worker.documents = self.engine.documents
        worker.subagent_role = 'gui-scout'
        worker.subagent_depth = 1
        worker.worker_write_enabled = task.write_enabled
        worker.worker_allowed_tools = list(task.tools)
        worker.worker_shell_mode = task.shell_mode
        worker.worker_allowed_files = list(task.allowed_files)
        worker.worker_writable_files = list(task.writable_files)
        worker.subagent_parent_step_id = task.metadata.get('parent_step_id')
        # 子 Run 禁止再次委派，避免递归派发绕过角色和并发限制。
        worker.subagent_enabled = False
        try:
            if not sandbox.started:
                await sandbox.open(str(task.metadata.get('source_manifest') or parent.source_manifest))
            # 工厂已启动沙箱，首个观测后直接进入 EXPLORE；
            # 再执行 PREPARE 会重复创建网络/容器，破坏资源与身份对应关系。
            raw = await sandbox.browser.action(BrowserAction(kind='navigate', value=child.url))
            child.observation_ref = await worker.capture(child, raw)
            child.phase = Phase.EXPLORE
            child.step = 3
            child.source_aligned = True
            child.environment_digest = sandbox.runner.actual_digest
            worker.store.save(child)
            await worker.run(child)
            child = worker.store.load(child.run_id, child.scope_id)
            events = worker.store.trace(child.run_id, child.scope_id)
            # 完整子轨迹存为 artifact，父 Run 只接收摘要和引用，保留证据来源。
            trace_ref = worker.artifacts.put(child.scope_id, child.run_id, events, label='子Agent完整轨迹')
            page_map = {'url': child.url, 'source_manifest': child.source_manifest,
                        'observation_ref': child.observation_ref}
            status = WorkerStatus.SUCCEEDED if str(child.run_status) == 'COMPLETED' else WorkerStatus.PARTIAL
            return WorkerResult(task_id=task.task_id, run_id=task.run_id, phase=task.phase,
                role=task.role, role_label=task.role_label or task.role, status=status,
                summary=f'独立探索结果：{child.outcome or child.run_status}',
                evidence_refs=[], data={'child_run_id': child.run_id, 'trace_ref': trace_ref,
                    'report_ref': child.report_ref, 'page_map': page_map,
                    'child_evidence_refs': child.evidence_refs, 'usage': child.budget.model_dump(),
                    'parent_run_id': parent.run_id, 'parent_step_id': task.metadata.get('parent_step_id')})
        finally:
            await sandbox.close()


@dataclass
class SubAgentRuntime:
    profile: Any | None = None
    workspace: Any | None = None
    browser_factory: BrowserSandboxFactory | None = None
    max_concurrency: int = 3
    trace: HierarchyTrace = field(default_factory=HierarchyTrace)
    scheduler: WorkerScheduler | None = None
    _cancelled: set[str] = field(default_factory=set)
    browser_concurrency: int = 1
    _browser_semaphore: asyncio.Semaphore = field(init=False)
    _browser_tasks: dict[str, set[asyncio.Task]] = field(default_factory=dict)

    def __post_init__(self):
        if self.browser_concurrency < 1:
            raise ValueError('浏览器子 Agent 并发数必须为正整数')
        self._browser_semaphore = asyncio.Semaphore(self.browser_concurrency)
        if self.browser_factory is None and self.profile is not None and self.workspace is not None:
            self.browser_factory = BrowserSandboxFactory(self.profile, self.workspace)
        if self.scheduler is None:
            self.scheduler = WorkerScheduler(max_concurrency=max(1, min(4, self.max_concurrency)),
                                             event_sink=self._worker_event,
                                             workspace=self.workspace)

    def _worker_event(self, event):
        payload = event.as_payload() if hasattr(event, 'as_payload') else dict(event)
        self.trace.emit(payload.get('type', 'subtask.event'), **payload)

    def validate(self, task: WorkerTask, *, parent_run_id: str, parent_step_id: str,
                 generation: int | str | None = None, depth: int = 1):
        # 在入队前集中校验深度、角色、阶段、工具和写入范围。
        role = canonical_role(task.role)
        phase = task.phase.upper()
        if depth != 1 or task.parent_task_id is not None:
            raise SubAgentRejected('子 Agent 不得递归派发')
        if role not in ROLE_PHASES or phase not in ROLE_PHASES[role]:
            raise SubAgentRejected(f'角色 {task.role} 不允许在阶段 {task.phase} 执行')
        common = {'Bash', 'Read', 'Glob', 'Grep'}
        if task.write_enabled:
            common.update({'Write', 'Edit', 'NotebookEdit', 'file.write', 'code.write'})
        allowed = ROLE_TOOLS[role] | common
        if not set(task.tools) <= allowed:
            raise SubAgentRejected(f'角色 {role} 请求了未授权工具：{sorted(set(task.tools) - allowed)}')
        if task.writable_files and not task.write_enabled:
            raise SubAgentRejected('Supervisor 必须显式开启子 Agent 写权限')
        if task.writable_files and not ({WorkerTool.FILE_WRITE.value,
                                         WorkerTool.CODE_WRITE.value,
                                         WorkerTool.SHELL_PATCH.value,
                                         'Write', 'Edit', 'NotebookEdit', 'Bash'} & set(task.tools)):
            raise SubAgentRejected('写权限任务必须声明 file.write、code.write 或 shell.patch')
        if role == 'test-writer' and any(not path.startswith('tests/') for path in task.writable_files):
            raise SubAgentRejected('test-writer 只能新增 tests/ 文件')
        if generation is not None and task.source_revision is not None and task.source_revision != generation:
            raise SubAgentRejected('子 Agent generation/source_revision 已过期')
        self.trace.emit('subtask.created', parent_run_id=parent_run_id, parent_step_id=parent_step_id,
                        task_id=task.task_id, role=role, phase=phase, generation=generation, depth=depth)
        return role

    async def dispatch(self, task: WorkerTask, *, parent_run_id: str, parent_step_id: str,
                       generation: int | str | None = None, runner: Callable[..., Any] | None = None,
                       depth: int = 1) -> WorkerResult:
        role = self.validate(task, parent_run_id=parent_run_id, parent_step_id=parent_step_id,
                             generation=generation, depth=depth)
        child_run_id = new_id('subrun')
        if parent_run_id in self._cancelled:
            raise asyncio.CancelledError('父 Run 已取消')
        self.trace.emit('subtask.queued', parent_run_id=parent_run_id, parent_step_id=parent_step_id,
                        child_run_id=child_run_id, task_id=task.task_id, role=role)
        if role in {'gui-scout', 'reproducer'}:
            # 记录在途浏览器任务，使父 Run 取消可以覆盖排队和执行中的子任务。
            handle = asyncio.current_task()
            self._browser_tasks.setdefault(parent_run_id, set()).add(handle)
            try:
                async with self._browser_semaphore:
                    result = await self._dispatch_browser(task, role, child_run_id, runner, generation)
            except asyncio.CancelledError:
                result = WorkerResult(task_id=task.task_id, run_id=task.run_id,
                    phase=task.phase, role=task.role, status=WorkerStatus.CANCELLED,
                    summary='父 Run 暂停或取消，子 Agent 已停止。')
            finally:
                self._browser_tasks[parent_run_id].discard(handle)
        else:
            result = await self.scheduler.run_async(task, runner or self._default_runner)
        if generation is not None and task.source_revision is not None and task.source_revision != generation:
            raise SubAgentRejected('子 Agent 结果 generation 已过期')
        self.trace.emit('subtask.completed', parent_run_id=parent_run_id, parent_step_id=parent_step_id,
                        child_run_id=child_run_id, task_id=task.task_id, role=role,
                        status=result.status, summary=result.summary, usage=result.data.get('usage', {}))
        return result

    async def group(self, tasks: list[WorkerTask], *, parent_run_id: str, parent_step_id: str,
                    generation: int | str | None = None, runners: Mapping[str, Callable[..., Any]] | None = None,
                    depth: int = 1):
        """并行派发任务；浏览器任务仍受独立沙箱与浏览器并发数限制。"""
        runners = runners or {}
        return await asyncio.gather(*(
            self.dispatch(task, parent_run_id=parent_run_id, parent_step_id=parent_step_id,
                          generation=generation, runner=runners.get(task.task_id), depth=depth)
            for task in tasks
        ))

    async def _dispatch_browser(self, task, role, child_run_id, runner, generation):
        if self.browser_factory is None:
            raise SubAgentRejected('未配置浏览器子 Agent 的隔离容器工厂')
        task.metadata.setdefault('child_run_id', child_run_id)
        sandbox = self.browser_factory.create(parent_run_id=task.run_id, child_run_id=child_run_id)
        # IsolatedGuiScout 自行管理沙箱生命周期，其余执行器由本方法统一回收。
        owns_sandbox = not isinstance(runner, IsolatedGuiScout)
        try:
            callback = runner or self._default_browser_runner
            if not isinstance(callback, IsolatedGuiScout):
                manifest = task.metadata.get('source_manifest')
                if not manifest:
                    raise SubAgentRejected('浏览器子 Agent 缺少冻结 source_manifest')
                await sandbox.open(manifest)
            value = callback(task, sandbox)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, WorkerResult):
                return value
            payload = dict(value or {}) if isinstance(value, Mapping) else {'summary': str(value)}
            payload.setdefault('status', WorkerStatus.SUCCEEDED)
            payload.setdefault('data', {})
            return WorkerResult.model_validate({**payload, 'task_id': task.task_id, 'run_id': task.run_id,
                'phase': task.phase, 'role': task.role, 'role_label': task.role_label or task.role})
        finally:
            if owns_sandbox:
                await sandbox.close()

    @staticmethod
    def _default_runner(task, context: WorkerContext):
        context.check_cancelled()
        return {'status': WorkerStatus.PARTIAL, 'summary': '未配置子 Agent 执行器，已安全跳过。',
                'unresolved': ['需要绑定角色执行器'], 'data': {'child_run_id': task.run_id + ':' + task.task_id}}

    @staticmethod
    async def _default_browser_runner(task, sandbox):
        return {'status': WorkerStatus.PARTIAL, 'summary': '未配置浏览器探索执行器，未执行动作。',
                'unresolved': ['需要绑定 gui-scout/reproducer 执行器']}

    def cancel(self, parent_run_id: str):
        # 持久保留取消标记以拒绝后续派发，并向浏览器协程与调度器传播取消。
        self._cancelled.add(parent_run_id)
        for handle in self._browser_tasks.get(parent_run_id, set()):
            handle.cancel()
        if self.scheduler:
            for snapshot in self.scheduler.snapshots():
                if snapshot.run_id == parent_run_id:
                    self.scheduler.cancel(snapshot.task_id)

    def close(self):
        if self.scheduler:
            self.scheduler.close(wait=False, cancel_pending=True)


__all__ = ['ROLE_PHASES', 'ROLE_TOOLS', 'SubAgentRejected', 'SubAgentTrace',
           'HierarchyTrace', 'BrowserSandbox', 'BrowserSandboxFactory', 'IsolatedGuiScout', 'SubAgentRuntime']
