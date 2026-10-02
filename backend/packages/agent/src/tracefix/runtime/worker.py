"""按需启用只读委派；复用模型网关，并将子任务用量汇总到父 Run。"""
import asyncio
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from langgraph.graph import END, START, StateGraph

from tracefix.runtime.contracts import Contract, Usage, new_id


class SubtaskResult(Contract):
    id: str = ''
    status: Literal['completed', 'partial', 'failed', 'timeout', 'rejected'] = 'completed'
    conclusion: str = Field(default='', max_length=1600)
    summary: str = Field(default='', max_length=1500)
    evidence_refs: list[str] = Field(max_length=10)
    files: list[str] = Field(max_length=10)
    suggested_experiments: list[str] = Field(max_length=5)
    unresolved: list[str] = Field(max_length=5)
    artifact_refs: list[str] = Field(default_factory=list, max_length=20)
    usage: dict = Field(default_factory=dict)

    @property
    def output_summary(self):
        return self.summary or self.conclusion


@dataclass(frozen=True)
class SubtaskSpec:
    goal: str
    role: str
    generation: int
    allowed_files: tuple[str, ...]
    allowed_artifacts: tuple[str, ...]
    depth: int = 1


class ReadOnlyWorker:
    def __init__(self, engine):
        self.engine = engine
        self.semaphore = asyncio.Semaphore(2)
        self._usage_lock = asyncio.Lock()

    async def run(self, state, spec: SubtaskSpec):
        if spec.depth != 1 or spec.role not in {'code_investigator', 'evidence_reviewer',
                                               'code-explorer', 'evidence-reviewer', 'patch-reviewer'}:
            raise PermissionError('worker 能力或深度被拒绝')
        if state.phase not in {'DIAGNOSE', 'REVIEW'} or (state.phase == 'REVIEW' and spec.role != 'patch-reviewer'):
            raise PermissionError('worker 仅在诊断阶段可用')
        state.budget = state.budget.charge('subtasks')
        self.engine.store.save(state)
        if not set(spec.allowed_artifacts) <= set(state.evidence_refs):
            raise PermissionError('非本作用域证据')
        cards = [{'path': p, 'content': self.engine.workspace.read(p)} for p in spec.allowed_files]
        # 子 Run 使用独立身份与用量；项目约束重新存为子 Run artifact 以便审计。
        child = state.model_copy(deep=True, update={'run_id': new_id('subrun'),
            'parent_run_id': state.run_id, 'revision': 0, 'budget': Usage(),
            'model_exchange_refs': [], 'rule_snapshot_ref': None, 'subtask_refs': [],
            'observation_ref': None, 'test_spec_ref': None, 'agent_instructions_ref': None})
        if state.agent_instructions_ref:
            child.agent_instructions_ref = self.engine.artifacts.put(child.scope_id, child.run_id,
                self.engine.agent_instructions(state), 'txt', label='项目约束')
        parent_step_id = f'{state.run_id}:{state.revision}'
        self.engine.store.save(child)
        self.engine.event(state, 'subtask.started', {'child_run_id': child.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role, 'generation': spec.generation})
        try:
            async with self.semaphore:
                async def investigate(_data):
                    result = await self.engine.model_call(child, SubtaskResult,
                         {'goal':spec.goal,'role':spec.role,'scope':state.scope_id,'files':cards,
                         'worker_depth': 1, 'allowed_tools': [],
                         'worker_allowed_files': list(spec.allowed_files),
                         'allowed_evidence_refs': list(spec.allowed_artifacts),
                         'instruction': '只读调查并返回证据支持的中文结论；不得委派、修改、审批或发布。',
                         'evidence':[self.engine.get(state,r) for r in spec.allowed_artifacts]})
                    return {'result': result.model_dump(mode='json')}
                graph = StateGraph(dict)
                graph.add_node('investigate', investigate)
                graph.add_edge(START, 'investigate')
                graph.add_edge('investigate', END)
                with self.engine.store.writer(child.run_id):
                    # 独立 thread_id 隔离 checkpoint，避免子任务覆盖父 Run 的图状态。
                    answer = await graph.compile(checkpointer=self.engine.graph.checkpointer).ainvoke(
                        {}, {'configurable': {'thread_id': child.run_id}})
                result = SubtaskResult.model_validate(answer['result'])
        finally:
            # 调查失败或被取消时也归并已经发生的用量，锁保护并行任务的累计值。
            async with self._usage_lock:
                usage = state.budget.model_dump()
                for metric in ('model_calls', 'browser_actions', 'tokens', 'cost_usd'):
                    usage[metric] += getattr(child.budget, metric)
                state.budget = Usage(**usage)
                self.engine.store.save(state)
        # 结果返回时再次校验父状态版本和引用范围，拒绝过期调查与越权输出。
        if spec.generation != state.revision:
            raise ValueError('worker 版本已过期')
        if not set(result.evidence_refs) <= set(spec.allowed_artifacts) or not set(result.files) <= set(spec.allowed_files):
            raise PermissionError('worker 返回了未被授权委派的引用')
        self.engine.event(child, 'subtask.finished', {'parent_run_id': state.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role, 'usage': child.budget.model_dump()})
        trace_ref = self.engine.artifacts.put(child.scope_id, child.run_id,
            self.engine.store.trace(child.run_id, child.scope_id), label='子Agent完整轨迹')
        self.engine.event(state, 'subtask.completed', {'child_run_id': child.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role,
            'trace_ref': trace_ref, 'usage': child.budget.model_dump()})
        return result

    async def group(self, state, specs):
        async def isolated(spec):
            try:
                return await self.run(state, spec)
            except asyncio.CancelledError:
                # 取消必须向外传播，TaskGroup 才能同步取消其他在途子任务。
                raise
            except Exception as error:
                # 普通调查失败转成受限结果，使父 Run 能继续使用原始证据。
                self.engine.event(state, 'subtask.failed', {'role': spec.role,
                    'error': str(error), 'generation': spec.generation})
                return SubtaskResult(conclusion='只读子任务未完成，父 Run 可继续依据原始证据诊断。',
                    evidence_refs=[], files=[], suggested_experiments=[], unresolved=[str(error)[:1000]])
        async with asyncio.TaskGroup() as group:
            tasks=[group.create_task(isolated(spec)) for spec in specs]
        return [t.result() for t in tasks]
