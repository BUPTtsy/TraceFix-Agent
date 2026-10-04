"""按需启用只读委派；复用模型网关，并将子任务用量汇总到父 Run。"""
import asyncio
import os
from dataclasses import dataclass
from typing import Literal

from pydantic import Field
from langgraph.graph import END, START, StateGraph

from tracefix.runtime.contracts import Contract, Usage, digest, new_id
from tracefix.runtime.diagnosis import Hypothesis


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
    worker_generation: int | None = None
    source_manifest: str = ''
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)
    support_refs: list[str] = Field(default_factory=list, max_length=20)
    counterevidence_refs: list[str] = Field(default_factory=list, max_length=20)
    gaps: list[str] = Field(default_factory=list, max_length=10)
    worker_id: str = ''

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
        if os.getenv('TRACEFIX_AGENT_MODE', '').lower() == 'single' or os.getenv('TRACEFIX_WORKER', '1') == '0':
            raise PermissionError('当前单 Agent 或 TRACEFIX_WORKER 配置禁用模型委派')
        if spec.depth != 1 or spec.role not in {'code_investigator', 'evidence_reviewer',
                                               'code-explorer', 'evidence-reviewer', 'patch-reviewer'}:
            raise PermissionError('worker 能力或深度被拒绝')
        if state.phase not in {'DIAGNOSE', 'REVIEW'} or (state.phase == 'REVIEW' and spec.role != 'patch-reviewer'):
            raise PermissionError('worker 仅在诊断阶段可用')
        state.budget = state.budget.charge('subtasks')
        self.engine.store.save(state)
        if not set(spec.allowed_artifacts) <= set(state.evidence_refs):
            raise PermissionError('非本作用域证据')
        if len(spec.allowed_files) > 15:
            raise ValueError('只读调查材料超过 15 个文件，需拆分任务')
        if hasattr(self.engine.workspace, 'fragments'):
            cards = self.engine.workspace.fragments(spec.goal, preferred_paths=spec.allowed_files,
                                                   limit_chars=16_000)
            cards = [card for card in cards if card['path'] in spec.allowed_files]
        else:
            cards = [{'path': path, 'content': '\n'.join(self.engine.workspace.read(path).splitlines()[:160])}
                     for path in spec.allowed_files]
        source_manifest = state.source_manifest
        versions = {path: digest(self.engine.workspace.source_bytes(path)[1])
                    for path in spec.allowed_files}
        # 子 Run 使用独立身份与用量；项目约束重新存为子 Run artifact 以便审计。
        child = state.model_copy(deep=True, update={'run_id': new_id('subrun'),
            'parent_run_id': state.run_id, 'revision': 0, 'budget': Usage(),
            'model_exchange_refs': [], 'rule_snapshot_ref': None, 'subtask_refs': [],
            'observation_ref': None, 'test_spec_ref': None, 'agent_instructions_ref': None,
            'context_manifest_refs': [], 'evidence_refs': [], 'hypothesis_refs': [],
            'patch_ref': None, 'validation_refs': [], 'reasoning_refs': []})
        if state.agent_instructions_ref:
            child.agent_instructions_ref = self.engine.artifacts.put(child.scope_id, child.run_id,
                self.engine.agent_instructions(state), 'txt', label='项目约束')
        parent_step_id = f'{state.run_id}:{state.revision}'
        self.engine.store.save(child)
        self.engine.event(state, 'subtask.started', {'child_run_id': child.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role, 'generation': spec.generation,
            'source_manifest': source_manifest, 'tools': ['Read', 'Grep', 'Glob'],
            'model': getattr(self.engine.model, 'text_model', type(self.engine.model).__name__),
            'reasoning_effort': getattr(self.engine.model, 'reasoning_effort', None),
            'same_model_as_supervisor': True})
        try:
            async with self.semaphore:
                async def investigate(_data):
                    result = await self.engine.model_call(child, SubtaskResult,
                         {'goal':spec.goal,'role':spec.role,'scope':state.scope_id,'files':cards,
                         'worker_depth': 1, 'allowed_tools': ['Read', 'Grep', 'Glob'],
                         'worker_allowed_tools': ['Read', 'Grep', 'Glob'],
                         'worker_write_enabled': False, 'worker_shell_mode': 'disabled',
                         'worker_same_model': True, 'worker_readonly_investigation': True,
                         'worker_allowed_files': list(spec.allowed_files),
                         'allowed_evidence_refs': list(spec.allowed_artifacts),
                         'worker_generation': spec.generation,
                         'source_manifest': state.source_manifest,
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
                if result.worker_generation is not None and result.worker_generation != spec.generation:
                    raise ValueError('worker 返回的 generation 已过期')
                if result.source_manifest and result.source_manifest != source_manifest:
                    raise ValueError('worker 返回的 source 版本已过期')
                result = result.model_copy(update={'worker_generation': spec.generation,
                    'source_manifest': source_manifest, 'worker_id': child.run_id,
                    'usage': child.budget.model_dump()})
        finally:
            # 调查失败或被取消时也归并已经发生的用量，锁保护并行任务的累计值。
            async with self._usage_lock:
                usage = state.budget.model_dump()
                for metric in ('model_calls', 'browser_actions', 'tokens', 'cost_usd'):
                    usage[metric] += getattr(child.budget, metric)
                state.budget = Usage(**usage)
                self.engine.store.save(state)
        # 结果返回时再次校验父状态版本和引用范围，拒绝过期调查与越权输出。
        if (spec.generation != state.revision
                or self.engine.store.load(state.run_id, state.scope_id).revision != spec.generation):
            raise ValueError('worker 版本已过期')
        if result.worker_generation != spec.generation or result.source_manifest != state.source_manifest:
            raise ValueError('worker 返回的 generation/source 版本已过期')
        if any(digest(self.engine.workspace.source_bytes(path)[1]) != version
               for path, version in versions.items()):
            raise ValueError('worker 调查期间源码内容版本已变更')
        references = (set(result.evidence_refs) | set(result.support_refs)
                      | set(result.counterevidence_refs) | set(result.artifact_refs))
        for hypothesis in result.hypotheses:
            for field in ('support_refs', 'counterevidence_refs', 'evidence_refs'):
                references.update(hypothesis.get(field, []))
        for hypothesis in result.hypotheses:
            references.update(hypothesis.support_refs)
            references.update(hypothesis.counterevidence_refs)
            for candidate in hypothesis.candidate_paths:
                if candidate.path not in spec.allowed_files:
                    raise PermissionError('worker 假设候选路径超出授权范围')
                if candidate.content_version != versions[candidate.path]:
                    candidate.status = 'stale'
                    hypothesis.status = 'unresolved'
        if not references <= set(spec.allowed_artifacts) or not set(result.files) <= set(spec.allowed_files):
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
                return SubtaskResult(status='failed',
                    conclusion='只读子任务未完成，父 Run 可继续依据原始证据诊断。',
                    evidence_refs=[], files=[], suggested_experiments=[], unresolved=[str(error)[:1000]],
                    gaps=[str(error)[:1000]])
        async with asyncio.TaskGroup() as group:
            tasks=[group.create_task(isolated(spec)) for spec in specs]
        return [t.result() for t in tasks]
