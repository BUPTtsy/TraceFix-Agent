"""授权代码、规则、记忆及阶段提交工具的运行时绑定。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from pydantic import ConfigDict, Field

from tracefix.runtime.contracts import (BrowserAction, Contract, Decision, PatchProposal,
                                       Phase, TestSpec, digest)
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolRejected, ToolSpec


class EmptyInput(Contract):
    pass


class RuleGet(Contract):
    rule_id: str = Field(min_length=1, max_length=200)


class MemorySearch(Contract):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)


class StrictTestSpec(TestSpec):
    model_config = ConfigDict(extra='forbid')


class ReportFindings(Contract):
    summary: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    findings: list[str] = Field(default_factory=list, max_length=50)
    unresolved: list[str] = Field(default_factory=list, max_length=50)


@dataclass
class RuntimeTools:
    registry: ToolRegistry
    handlers: dict
    phase: Phase
    submissions: dict = field(default_factory=dict)

    def pipeline(self, **callbacks):
        return ToolPipeline(self.registry, self.handlers, self.phase, **callbacks)


def _value(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else value


def build_runtime_tools(engine, state, schema, context=None, validate_output=None):
    context = context or {}
    if getattr(engine, 'subagent_depth', 0) == 1:
        context = {**context, 'worker_depth': 1,
                   'worker_write_enabled': getattr(engine, 'worker_write_enabled', False),
                   'worker_allowed_files': getattr(engine, 'worker_allowed_files', []),
                   'worker_writable_files': getattr(engine, 'worker_writable_files', [])}
    # 子 Agent 默认只读，写权限及路径由 Supervisor 契约显式声明。
    worker_mode = context.get('worker_depth') == 1
    worker_allowed_files = {
        str(path).replace('\\', '/') for path in context.get('worker_allowed_files', [])
    }
    worker_allowed_tools = set(context.get('allowed_tools') or context.get('worker_allowed_tools') or [])
    worker_shell_mode = context.get('worker_shell_mode', 'disabled')
    registry, handlers, submissions = ToolRegistry(), {}, {}
    phase = Phase(state.phase)

    def worker_tool_allowed(name, side_effect):
        if not worker_mode:
            return True
        aliases = {
            'Read': {'Read', 'file.read', 'code.read'},
            'Glob': {'Glob', 'file.read', 'code.read'},
            'Grep': {'Grep', 'file.read', 'code.read'},
            'Write': {'Write', 'file.write', 'code.write'},
            'Edit': {'Edit', 'file.write', 'code.write'},
            'NotebookEdit': {'NotebookEdit', 'file.write', 'code.write'},
            'Bash': {'Bash', 'shell', 'shell.readonly', 'shell.patch'},
        }
        if not worker_allowed_tools or not (worker_allowed_tools & aliases.get(name, {name})):
            return False
        if name == 'Bash':
            return worker_shell_mode in {'readonly', 'patch'}
        if side_effect in {'write', 'external'}:
            return context.get('worker_write_enabled') is True
        return True

    def bind(name, description, input_model, handler, *, phases, side_effect='read',
             parallel_safe=True, submission=False, output_limit_tokens=4000, timeout_s=45):
        if not worker_tool_allowed(name, side_effect):
            return
        registry.register(ToolSpec(name, description, input_model,
            output_limit_tokens=output_limit_tokens, side_effect=side_effect,
            timeout_s=timeout_s,
            phases=frozenset(phases), parallel_safe=parallel_safe,
            idempotency_key=digest if side_effect in {'write', 'external'} else None,
            submission=submission, submission_schema=schema if submission else None,
            category=name.split('.')[0]))
        handlers[name] = handler

    from tracefix.runtime.local_tools import register_local_tools
    register_local_tools(engine, state, context, bind)

    if not worker_mode:
        from tracefix.workers.contracts import WorkerResult, WorkerTask
        from tracefix.workers.tools import supervisor_tools
        from tracefix.runtime.contracts import Usage, new_id

        async def delegate(arguments, call_id):
            task = WorkerTask.from_delegate_args(arguments, run_id=state.run_id,
                                                source_revision=state.revision)
            if task.write_enabled and not task.writable_files:
                raise ToolRejected('开启子 Agent 写权限必须显式指定 writable_files')
            if not set(task.allowed_artifacts) <= set(state.evidence_refs):
                raise ToolRejected('委派证据必须属于父 Run')
            for relative in task.allowed_files:
                engine.workspace.path(relative)
            for relative in task.writable_files:
                engine.workspace.path(relative, write=True)
            child = state.model_copy(deep=True, update={
                'run_id': new_id('subrun'), 'parent_run_id': state.run_id,
                'budget': Usage(), 'model_exchange_refs': [], 'context_manifest_refs': [],
                'repo_snapshot_ref': '', 'memory_snapshot_ref': '',
                'agent_instructions_ref': None, 'observation_ref': None,
                'test_spec_ref': None, 'replay_plan_ref': None,
                'exploration_plan_ref': None, 'evidence_refs': [],
                'working_set_refs': [], 'patch_ref': None, 'validation_refs': [],
                'report_ref': None, 'approval_ref': None, 'rule_snapshot_ref': None,
                'rule_snapshot_hash': '',
                'reasoning_refs': [], 'compaction_refs': [], 'working_memory_ref': None,
                'baseline_validation_refs': [], 'diagnosis_feedback_refs': [],
                'inherited_evidence_refs': []})

            copied_artifacts = {}

            def copy_artifact(reference):
                if reference in copied_artifacts:
                    return copied_artifacts[reference]
                if not engine.artifacts.exists(state.scope_id, state.run_id, reference):
                    raise ToolRejected('委派证据不存在或已损坏')
                extension = reference.rsplit('.', 1)[-1]
                raw = engine.artifacts.read(state.scope_id, state.run_id, reference)
                if extension == 'json':
                    try:
                        value = json.loads(raw)
                    except (TypeError, ValueError) as error:
                        raise ToolRejected('委派证据 JSON 无法解析') from error

                    def rewrite(item):
                        if isinstance(item, dict):
                            return {key: rewrite(value) for key, value in item.items()}
                        if isinstance(item, list):
                            return [rewrite(value) for value in item]
                        if isinstance(item, str) and engine.artifacts.exists(
                                state.scope_id, state.run_id, item):
                            return copy_artifact(item)
                        return item

                    raw = rewrite(value)
                copied_artifacts[reference] = engine.artifacts.put(
                    child.scope_id, child.run_id, raw, extension, label='委派授权证据')
                return copied_artifacts[reference]

            child.evidence_refs = [copy_artifact(reference) for reference in task.allowed_artifacts]
            if state.agent_instructions_ref:
                child.agent_instructions_ref = engine.artifacts.put(child.scope_id, child.run_id,
                    engine.agent_instructions(state), 'txt', label='项目约束')
            parent_step = f'{state.run_id}:{state.revision}'
            engine.store.save(child)
            engine.event(state, 'subtask.started', {'child_run_id': child.run_id,
                'parent_step_id': parent_step, 'role': task.role, 'generation': task.source_revision})
            try:
                call = engine.model_call(child, WorkerResult, {
                    'worker_depth': 1, 'worker_write_enabled': task.write_enabled,
                    'worker_allowed_files': task.allowed_files,
                    'worker_writable_files': task.writable_files,
                    'allowed_tools': task.allowed_tools,
                    'worker_shell_mode': task.shell_mode,
                    'goal': task.goal, 'instruction': task.prompt,
                    'role': task.role, 'allowed_evidence_refs': child.evidence_refs})
                result = await (asyncio.wait_for(call, timeout=task.timeout_seconds)
                                if task.timeout_seconds is not None else call)
                reverse_artifacts = {value: key for key, value in copied_artifacts.items()}
                result.evidence_refs = [reverse_artifacts.get(reference, reference)
                                        for reference in result.evidence_refs]
                result.artifacts = [reverse_artifacts.get(reference, reference)
                                    for reference in result.artifacts]
                if state.revision != task.source_revision:
                    raise ToolRejected('子 Agent 返回时父 Run generation 已变化')
                if not set(result.evidence_refs) <= set(task.allowed_artifacts):
                    raise ToolRejected('子 Agent 返回越权证据')
                if not set(result.files_touched) <= set(task.allowed_files):
                    raise ToolRejected('子 Agent 返回越权文件')
                result.task_id, result.run_id = task.task_id, state.run_id
                result.role, result.phase = task.role, task.phase
                result.data['child_run_id'] = child.run_id
                result.data['usage'] = child.budget.model_dump()
                engine.event(state, 'subtask.completed', {'child_run_id': child.run_id,
                    'parent_step_id': parent_step, 'role': task.role, 'usage': result.data['usage']})
                return result
            finally:
                usage = state.budget.model_dump()
                for metric in ('model_calls', 'browser_actions', 'tokens', 'cost_usd'):
                    usage[metric] += getattr(child.budget, metric)
                state.budget = Usage(**usage)
                engine.store.save(state)

        delegate_spec = supervisor_tools()[0]
        bind('agent.delegate', delegate_spec.description, delegate_spec.input_model, delegate,
             phases=set(Phase), side_effect='write', parallel_safe=False, timeout_s=86_400)

    async def rules_applicable(arguments, call_id):
        return {'rules': [_value(rule) for rule in engine.active_rules(state)]}

    async def rules_get(arguments, call_id):
        for rule in engine.active_rules(state):
            value = _value(rule)
            if value.get('id') == arguments.rule_id:
                return value
        raise ToolRejected('规则不在当前冻结且适用的集合中')

    if getattr(engine, 'rule_resolver', None) and not worker_mode:
        bind('rules.applicable', '列出当前阶段和页面实际适用的冻结规则。',
             EmptyInput, rules_applicable, phases=set(Phase))
        bind('rules.get', '读取当前适用集合内指定规则的冻结正文。',
             RuleGet, rules_get, phases=set(Phase))

    memory = getattr(engine, 'memory', None)
    if memory is not None and not worker_mode:
        from tracefix.knowledge.memory import MemoryNote

        async def memory_search(arguments, call_id):
            return await asyncio.to_thread(memory.search, arguments.query,
                scope_id=state.scope_id, run_id=state.run_id,
                job_id=getattr(state, 'job_id', None), source_manifest=state.source_manifest,
                limit=arguments.limit)

        async def memory_note(arguments, call_id):
            # 工作记忆只能引用当前 Run 的证据，不能凭空扩大可信证据集合。
            refs = list(state.evidence_refs)
            if state.observation_ref:
                refs.append(state.observation_ref)
            try:
                value = await asyncio.to_thread(memory.note, scope_id=state.scope_id,
                    run_id=state.run_id, note=arguments, allowed_evidence_refs=refs,
                    source_manifest=state.source_manifest)
            except (ValueError, PermissionError) as error:
                raise ToolRejected(str(error)) from error
            return _value(value)

        bind('memory.search', '搜索当前 Run、同 Job 及本作用域可信记忆，返回证据引用。',
             MemorySearch, memory_search, phases=set(Phase))
        bind('memory.note', '写入当前 Run 工作记忆；不会晋升可信长期记忆或扩大权限。',
             MemoryNote, memory_note, phases=set(Phase), side_effect='write', parallel_safe=False)

    submission = None
    if schema is TestSpec:
        submission = ('submit_test_spec', StrictTestSpec, Phase.PREPARE)
    elif schema in {Decision, BrowserAction}:
        submission = ('finish_exploration', schema, Phase.EXPLORE)
    elif schema is PatchProposal:
        submission = ('propose_patch', PatchProposal, Phase.DIAGNOSE)
    elif schema is ReportFindings:
        submission = ('report_findings', ReportFindings, phase)
    if submission and not worker_mode:
        name, input_model, required_phase = submission

        async def submit(arguments, call_id):
            # 提交仅接收经校验的候选结果；动作执行、补丁应用和阶段推进由运行时决定。
            try:
                value = schema.model_validate(arguments.model_dump(mode='json'))
                action = getattr(value, 'action', value)
                if schema in {Decision, BrowserAction} and action.kind != 'finish':
                    raise ToolRejected('结束探索只能提交 finish，动作必须通过浏览器工具执行')
                if validate_output:
                    validate_output(value)
                allowed_evidence = set(state.evidence_refs)
                if schema is Decision:
                    from tracefix.runtime.engine import normalize_decision_evidence_refs

                    observation = engine.get(state, state.observation_ref) if state.observation_ref else {}
                    value.evidence_refs = normalize_decision_evidence_refs(
                        value.evidence_refs, state.evidence_refs, state.observation_ref, observation)
                    if state.observation_ref:
                        allowed_evidence.add(state.observation_ref)
                if hasattr(value, 'evidence_refs') and not set(value.evidence_refs) <= allowed_evidence:
                    raise ToolRejected('提交引用了当前 Run 之外的证据')
                if schema is PatchProposal:
                    engine.validate_patch_candidate(value)
            except Exception as error:
                if (getattr(error, 'status', 'FAILED') != 'FAILED'
                        or getattr(error, 'details', {}).get('requires_manual_review')):
                    raise
                raise ToolRejected(str(error)) from error
            submissions[name] = value
            return {'accepted': True, 'submission': name, 'value': value.model_dump(mode='json')}

        bind(name, '提交经过运行时校验的阶段结果；运行时继续决定断言、修复与状态转移。',
             input_model, submit, phases={required_phase}, side_effect='write',
             parallel_safe=False, submission=True, output_limit_tokens=2000)
    return RuntimeTools(registry, handlers, phase, submissions)
