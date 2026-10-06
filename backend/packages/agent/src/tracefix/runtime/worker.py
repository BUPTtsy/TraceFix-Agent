"""宿主调查子图与单写者生命周期；DeepAgents 仅执行只读调查叶节点。"""
import asyncio
import json
import os
from fnmatch import fnmatchcase

from langgraph.graph import END, START, StateGraph

from tracefix.runtime.contracts import Usage, digest, new_id
from tracefix.runtime.effects import file_resource
from tracefix.runtime.subtask_contracts import SubtaskResult, SubtaskSpec
from tracefix.runtime.tool_handlers import build_runtime_tools


class ReadOnlyWorker:
    def __init__(self, engine):
        self.engine = engine
        self.semaphore = asyncio.Semaphore(2)
        self._usage_lock = asyncio.Lock()

    def _current(self, state, spec, source_manifest, versions):
        self.engine.scopes.assert_current(self.engine.context)
        saved = self.engine.store.load(state.run_id, state.scope_id)
        if (spec.generation != state.revision or saved.revision != spec.generation
                or state.source_manifest != source_manifest or saved.source_manifest != source_manifest):
            raise ValueError('worker generation/source 版本已过期')
        if not set(spec.allowed_artifacts) <= set(saved.evidence_refs):
            raise PermissionError('worker 证据授权已变化')
        for path, version in versions.items():
            if digest(self.engine.workspace.source_bytes(path)[1]) != version:
                raise ValueError('worker 调查期间源码内容版本已变更')

    def _audit(self, child, check):
        def record(kind, payload):
            if kind == 'request':
                check()
                child.budget = child.budget.charge('model_calls')
            elif kind == 'usage':
                totals = child.budget.model_dump()
                for metric in ('tokens', 'cost_usd'):
                    totals[metric] += payload['delta'][metric]
                child.budget = Usage(**totals)
            reference = self.engine.put(child, {'framework': 'deepagents', 'kind': kind, **payload},
                                        name='DeepAgents 调查模型' + kind)
            child.model_exchange_refs.append(reference)
            self.engine.store.save(child)
            events = {'request': 'model.request.persisted', 'response': 'model.response.persisted',
                      'error': 'model.error.persisted', 'usage': 'model.usage'}
            self.engine.event(child, events[kind], {**payload, 'artifact_ref': reference,
                                                    'framework': 'deepagents'})
        return record

    def _tools(self, child, spec, check, versions):
        context = {'worker_depth': 1, 'worker_write_enabled': False,
                   'worker_allowed_files': list(spec.allowed_files),
                   'worker_allowed_tools': ['Read', 'Grep', 'Glob'], 'worker_shell_mode': 'disabled'}
        runtime = build_runtime_tools(self.engine, child, SubtaskResult, context)
        pipeline = runtime.pipeline(emit=lambda kind, value: self.engine.event(child, kind, value),
            store_artifact=lambda value: self.engine.put(child, value, name='调查工具完整结果'),
            scope_check=lambda: self.engine.scopes.assert_current(self.engine.context))

        async def invoke(name, paths, call_id, perform):
            check()
            resources = [file_resource(self.engine.workspace.path(path)) for path in paths]
            if not resources:
                resources = [file_resource(self.engine.workspace.root)]
            async def controlled():
                check()
                result = await perform()
                check()
                return result
            return await self.engine.operation(child, 'investigation.' + name,
                {'paths': list(paths), 'source_manifest': child.source_manifest, 'resources': resources},
                controlled, idempotency_key=call_id, tool_call_id=call_id)

        async def read(*, path, call_id):
            async def perform():
                receipt = await pipeline.execute('Read', {
                    'file_path': str(self.engine.workspace.path(path)),
                    'expected_content_version': versions[path]}, call_id)
                if receipt.is_error:
                    raise PermissionError(receipt.error.get('message', '只读工具拒绝'))
                return receipt.model_dump(mode='json', by_alias=True)
            return json.dumps(await invoke('Read', (path,), call_id, perform), ensure_ascii=False)

        async def grep(*, pattern, path, call_id):
            async def perform():
                receipt = await pipeline.execute('Grep', {'pattern': pattern,
                    'path': str(self.engine.workspace.path(path)), 'output_mode': 'content'}, call_id)
                if receipt.is_error:
                    raise PermissionError(receipt.error.get('message', '只读工具拒绝'))
                return receipt.model_dump(mode='json', by_alias=True)
            return json.dumps(await invoke('Grep', (path,), call_id, perform), ensure_ascii=False)

        async def glob(*, pattern, allowed_files, call_id):
            if (not set(allowed_files) <= set(spec.allowed_files)
                    or any(not fnmatchcase(path, pattern) for path in allowed_files)):
                raise PermissionError('Glob 文件超出授权范围')
            async def perform():
                return [path for path in allowed_files if self.engine.workspace.path(path).is_file()]
            return await invoke('Glob', allowed_files, call_id, perform)

        return {'Read': read, 'Grep': grep, 'Glob': glob}

    async def run(self, state, spec: SubtaskSpec):
        if os.getenv('TRACEFIX_AGENT_MODE', '').lower() == 'single':
            raise PermissionError('当前单 Agent 配置禁用模型委派')
        if spec.depth != 1 or spec.role not in {'code_investigator', 'evidence_reviewer',
                                               'code-explorer', 'evidence-reviewer', 'patch-reviewer'}:
            raise PermissionError('worker 能力或深度被拒绝')
        if state.phase not in {'DIAGNOSE', 'REVIEW'} or (state.phase == 'REVIEW' and spec.role != 'patch-reviewer'):
            raise PermissionError('worker 仅在诊断或授权补丁评审阶段可用')
        if not set(spec.allowed_artifacts) <= set(state.evidence_refs):
            raise PermissionError('非本作用域证据')
        if len(spec.allowed_files) > 15:
            raise ValueError('只读调查材料超过 15 个文件，需拆分任务')
        from tracefix.agents.deepagents_adapter import DeepAgentsInvestigation, DeepAgentsReadonlyAdapter

        source_manifest = state.source_manifest
        configuration = self.engine.model
        request = DeepAgentsInvestigation(goal=spec.goal, phase=state.phase,
            allowed_files=spec.allowed_files, allowed_evidence_refs=spec.allowed_artifacts,
            source_manifest=source_manifest, worker_generation=spec.generation,
            model_configuration=configuration)
        versions = {path: digest(self.engine.workspace.source_bytes(path)[1]) for path in request.allowed_files}
        check = lambda: self._current(state, spec, source_manifest, versions)
        check()
        cards = [{'path': path, 'content_version': versions[path],
                  'content': '\n'.join(self.engine.workspace.read(path).splitlines()[:160])}
                 for path in request.allowed_files]
        evidence = [{'ref': reference, 'content': self.engine.get(state, reference)}
                    for reference in spec.allowed_artifacts]
        check()
        request = request.model_copy(update={'context': {'role': spec.role, 'scope': state.scope_id,
            'files': cards, 'evidence': evidence, 'agent_instructions': self.engine.agent_instructions(state)}})
        state.budget = state.budget.charge('subtasks')
        self.engine.store.save(state)
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
        selected = getattr(configuration, 'teacher', configuration)
        self.engine.event(state, 'subtask.started', {'child_run_id': child.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role, 'generation': spec.generation,
            'source_manifest': source_manifest, 'tools': ['Read', 'Grep', 'Glob'],
            'model': getattr(selected, 'text_model', type(selected).__name__),
            'reasoning_effort': getattr(selected, 'reasoning_effort', None),
            'same_model_as_supervisor': True, 'framework': 'deepagents'})
        try:
            async with self.semaphore:
                async def investigate(_data):
                    adapter = DeepAgentsReadonlyAdapter(readonly_tools=self._tools(child, spec, check, versions),
                        audit=self._audit(child, check),
                        max_model_calls=getattr(selected, 'max_tool_rounds', 40))
                    result = await adapter.run(request)
                    return {'result': result.model_dump(mode='json')}
                graph = StateGraph(dict)
                graph.add_node('investigate', investigate)
                graph.add_edge(START, 'investigate')
                graph.add_edge('investigate', END)
                with self.engine.store.writer(child.run_id):
                    answer = await graph.compile(checkpointer=self.engine.graph.checkpointer).ainvoke(
                        {}, {'configurable': {'thread_id': child.run_id}})
                result = SubtaskResult.model_validate(answer['result'])
                if result.worker_generation != spec.generation or result.source_manifest != source_manifest:
                    raise ValueError('worker 返回的 generation/source 版本已过期')
                result = result.model_copy(deep=True, update={'worker_id': child.run_id,
                                                             'usage': child.budget.model_dump()})
        finally:
            async with self._usage_lock:
                usage = state.budget.model_dump()
                for metric in ('model_calls', 'browser_actions', 'tokens', 'cost_usd'):
                    usage[metric] += getattr(child.budget, metric)
                state.budget = Usage(**usage)
                self.engine.store.save(state)
        check()
        references = (set(result.evidence_refs) | set(result.support_refs)
                      | set(result.counterevidence_refs) | set(result.artifact_refs))
        for hypothesis in result.hypotheses:
            references.update(hypothesis.support_refs)
            references.update(hypothesis.counterevidence_refs)
            for candidate in hypothesis.candidate_paths:
                if candidate.path not in spec.allowed_files:
                    raise PermissionError('worker 假设候选路径超出授权范围')
                if candidate.content_version != versions[candidate.path]:
                    raise ValueError('worker 假设候选源码版本已过期')
        if not references <= set(spec.allowed_artifacts) or not set(result.files) <= set(spec.allowed_files):
            raise PermissionError('worker 返回了未被授权委派的引用')
        self.engine.event(child, 'subtask.finished', {'parent_run_id': state.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role, 'status': result.status,
            'usage': child.budget.model_dump()})
        trace_ref = self.engine.artifacts.put(child.scope_id, child.run_id,
            self.engine.store.trace(child.run_id, child.scope_id), label='子Agent完整轨迹')
        self.engine.event(state, 'subtask.completed' if result.status == 'completed' else 'subtask.failed', {'child_run_id': child.run_id,
            'parent_step_id': parent_step_id, 'role': spec.role,
            'status': result.status, 'trace_ref': trace_ref, 'usage': child.budget.model_dump()})
        return result

    async def group(self, state, specs):
        async def isolated(spec):
            try:
                return await self.run(state, spec)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.engine.event(state, 'subtask.failed', {'role': spec.role,
                    'error': str(error), 'generation': spec.generation})
                raise
        async with asyncio.TaskGroup() as group:
            tasks=[group.create_task(isolated(spec)) for spec in specs]
        return [t.result() for t in tasks]
