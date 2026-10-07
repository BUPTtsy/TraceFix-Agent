"""授权代码、规则、记忆及阶段提交工具的运行时绑定。"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ConfigDict, Field

from tracefix.runtime.contracts import (BrowserAction, CheckJudgement, Contract, Decision, PatchProposal,
                                       Phase, TestSpec, digest)
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolRejected, build_tool
from tracefix.runtime.effects import file_resource, make_operation_executor, run_effect


class EmptyInput(Contract):
    pass


class RuleGet(Contract):
    rule_id: str = Field(min_length=1, max_length=200)


class CodeAnalyze(Contract):
    paths: list[str] = Field(default_factory=list, max_length=100)
    path_globs: list[str] = Field(default_factory=lambda: ['**'], max_length=30)
    check: Literal['syntax', 'elements', 'a11y_name', 'event_binding', 'regex'] = 'elements'
    config: dict = Field(default_factory=dict)


class MemorySearch(Contract):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)


class ReferenceExpand(Contract):
    ref: str = Field(min_length=1, max_length=300)
    start: int = Field(default=1, ge=1)
    end: int | None = Field(default=None, ge=1)
    channel: str = Field(default='snapshot', min_length=1, max_length=80)
    expected_hash: str | None = Field(default=None, min_length=1)
    max_lines: int = Field(default=200, ge=1, le=200)
    max_chars: int = Field(default=16000, ge=1, le=16000)


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
    operation_executor: object = None
    resource_resolver: object = None

    def pipeline(self, **callbacks):
        if self.operation_executor is not None:
            callbacks['operation'] = self.operation_executor
        if self.resource_resolver is not None:
            callbacks['resource_resolver'] = self.resource_resolver
        return ToolPipeline(self.registry, self.handlers, self.phase, **callbacks)


def _value(value):
    return value.model_dump(mode='json') if hasattr(value, 'model_dump') else value


def materialize_patch_proposal(engine, state, proposal):
    if not proposal.staged_refs:
        return proposal
    if state.mode != 'repair' or state.phase not in {Phase.DIAGNOSE, Phase.PATCH}:
        raise ToolRejected('当前 Run 阶段不允许物化修复候选')
    if engine.workspace.require_repository_snapshot()['scope_id'] != state.scope_id:
        raise ToolRejected('候选工作区快照与当前 Run scope 不一致')
    edits = engine.workspace.materialize_staged_candidates(
        proposal.staged_refs, state=state, artifacts=engine.artifacts)
    if proposal.edits and proposal.edits != edits:
        raise ToolRejected('STALE_REF: 提交的完整内容与确认候选不一致；未接受补丁')
    return PatchProposal.model_validate({**proposal.model_dump(mode='json'),
                                        'edits': [edit.model_dump(mode='json') for edit in edits]})


def build_runtime_tools(engine, state, schema, context=None, validate_output=None):
    from tracefix.runtime.local_tools import local_tool_context, register_local_tools
    context = local_tool_context(engine, context)
    # 子 Agent 默认只读，写权限及路径由 Supervisor 契约显式声明。
    worker_mode = context.get('worker_depth') == 1
    worker_allowed_files = {
        str(path).replace('\\', '/') for path in context.get('worker_allowed_files', [])
    }
    worker_allowed_tools = set(context.get('worker_allowed_tools', []))
    worker_shell_mode = context.get('worker_shell_mode', 'disabled')
    registry, handlers, submissions = ToolRegistry(), {}, {}
    phase = Phase(state.phase)

    def worker_tool_allowed(name, side_effect):
        if not worker_mode:
            return True
        if name not in worker_allowed_tools:
            return False
        if name == 'Bash':
            return worker_shell_mode in {'readonly', 'patch'}
        if side_effect in {'write', 'external'}:
            return context.get('worker_write_enabled') is True
        return True

    def bind(name, description, input_model, handler, *, phases, side_effect='read',
             parallel_safe=True, submission=False, output_limit_tokens=4000, timeout_s=45,
             output_model=None, aliases=(), search_hint='', enabled=True, idempotency_key=None):
        if schema is CheckJudgement and name not in {'Read', 'Grep', 'Glob', 'code.analyze',
                                                      'rules.get', 'rules.applicable', 'context.expand'}:
            return
        if not worker_tool_allowed(name, side_effect):
            return
        definition = build_tool(name, description, input_model, handler,
            output_limit_tokens=output_limit_tokens, side_effect=side_effect,
            timeout_s=timeout_s,
            phases=frozenset(phases), parallel_safe=parallel_safe,
            idempotency_key=(idempotency_key or digest) if side_effect in {'write', 'external'} else None,
            submission=submission, submission_schema=schema if submission else None,
            category=name.split('.')[0], output_model=output_model, aliases=aliases,
            search_hint=search_hint, enabled=enabled)
        registry.register(definition.spec)
        handlers[name] = definition.handler

    register_local_tools(engine, state, context, bind)

    async def code_analyze(arguments, call_id):
        from tracefix.rules.analyzers import analyze_source
        from tracefix.runtime.local_tools import LocalTools

        local = LocalTools(engine, context, state)
        requested = {local.path(value).relative_to(local.root).as_posix() for value in arguments.paths}
        files = []
        versions = {}
        with local.mutex.workspace_read():
            for path in local.files():
                relative = path.relative_to(local.root).as_posix()
                if requested and relative not in requested:
                    continue
                if arguments.path_globs and not any(fnmatch.fnmatchcase(relative, pattern)
                                                    for pattern in arguments.path_globs):
                    continue
                if path.suffix.lower() not in {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.mts', '.cts', '.tsx', '.vue', '.html', '.htm'}:
                    continue
                staged = local.staged_content(relative)
                data = staged if staged is not None else path.read_bytes()
                files.append((relative, data.decode('utf-8')))
                versions[relative] = digest(data)
        result = await asyncio.to_thread(analyze_source, files,
                                        {**arguments.config, 'check': arguments.check})
        result.update(source_manifest=state.source_manifest, content_versions=versions)
        if callable(getattr(engine, 'put', None)):
            result['artifact_ref'] = engine.put(state, result, name='代码分析证据')
        return result

    bind('code.analyze', '分析授权源码的 AST 和模板结构，支持 JavaScript/TypeScript、React JSX/TSX、Vue3 SFC 和 HTML。'
         '按需检查 syntax、elements、a11y_name、event_binding 或 regex；paths 使用工作区绝对路径，省略时按 path_globs 选择。'
         '结果含实际位置、源码哈希和证据引用；status=error 时应结合页面截图、语义观察和 Read/Grep 回退检测，不能视为通过。',
         CodeAnalyze, code_analyze, phases=set(Phase), output_limit_tokens=6000,
         search_hint='AST DOM frontend React Vue JavaScript TypeScript HTML accessibility event handler')
    from tracefix.runtime.task_tools import register_task_tools
    from tracefix.runtime.web_tools import register_web_tools

    register_task_tools(engine, state, context, bind)
    register_web_tools(engine, state, context, bind)

    if not worker_mode and os.getenv('TRACEFIX_AGENT_MODE', '').lower() != 'single':
        from tracefix.workers.contracts import WorkerResult, WorkerTask
        from tracefix.workers.tools import supervisor_tools
        from tracefix.runtime.contracts import Usage, new_id

        async def delegate(arguments, call_id):
            if os.getenv('TRACEFIX_AGENT_MODE', '').lower() == 'single':
                raise ToolRejected('single 模式禁用模型子 Agent 委派')
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
                'task_board_ref': None, 'todo_list_ref': None,
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
             phases=set(Phase), side_effect='write', parallel_safe=False, timeout_s=86_400,
             aliases=('Agent',), search_hint='delegate scoped worker subagent task')

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
                limit=arguments.limit,
                cross_run=getattr(memory, 'cross_run', None))

        async def memory_note(arguments, call_id):
            # 工作记忆只能引用当前 Run 的证据，不能凭空扩大可信证据集合。
            refs = list(state.evidence_refs)
            if state.observation_ref:
                refs.append(state.observation_ref)
            try:
                value = await run_effect('memory.note', {
                    'path': str(memory.path), 'scope_id': state.scope_id,
                    'run_id': state.run_id, 'note': arguments.model_dump(mode='json'),
                    'allowed_evidence_refs': refs, 'source_manifest': state.source_manifest},
                    timeout_s=45)
            except (ValueError, PermissionError) as error:
                raise ToolRejected(str(error)) from error
            return _value(value)

        bind('memory.search', '搜索当前 Run、同 Job 及本作用域可信记忆，返回证据引用。',
             MemorySearch, memory_search, phases=set(Phase))
        bind('memory.note', '写入当前 Run 工作记忆；不会晋升可信长期记忆或扩大权限。',
             MemoryNote, memory_note, phases=set(Phase), side_effect='write', parallel_safe=False)

    async def expand_reference(arguments, call_id):
        from tracefix.knowledge.workset import expand_reference as expand

        try:
            return expand(engine.artifacts, state.scope_id, state.run_id, arguments.ref,
                start=arguments.start, end=arguments.end, channel=arguments.channel,
                expected_hash=arguments.expected_hash, binding={
                    'scope_id': state.scope_id, 'source_manifest': state.source_manifest,
                    'patch_hash': state.patch_hash,
                    'page_generation': getattr(state, 'page_generation', None),
                    'environment_digest': state.environment_digest,
                    'test_spec_hash': state.test_spec_hash,
                }, max_lines=arguments.max_lines, max_chars=arguments.max_chars)
        except (OSError, ValueError, PermissionError) as error:
            raise ToolRejected(str(error)) from error

    bind('context.expand', '在当前 Run 作用域内按 artifact 引用展开被省略的证据通道或行范围。',
         ReferenceExpand, expand_reference, phases=set(Phase), output_limit_tokens=5000)

    from tracefix.runtime.discovery_tools import register_discovery_tools

    register_discovery_tools(engine, state, context, registry, bind)

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
                    value = materialize_patch_proposal(engine, state, value)
                    engine.validate_patch_candidate(value, state=state)
            except Exception as error:
                if (getattr(error, 'status', 'FAILED') != 'FAILED'
                        or getattr(error, 'details', {}).get('requires_manual_review')):
                    raise
                if schema is PatchProposal and getattr(error, 'details', {}).get('error_code'):
                    from tracefix.runtime.tools import ToolResult
                    return ToolResult(call_id=call_id, name=name, isError=True,
                        result=error.details, error={**error.details, 'message': str(error), 'executed': False})
                raise ToolRejected(str(error)) from error
            submissions[name] = value
            return {'accepted': True, 'submission': name, 'value': value.model_dump(mode='json')}

        bind(name, '提交经过运行时校验的阶段结果；运行时继续决定断言、修复与状态转移。',
             input_model, submit, phases={required_phase}, side_effect='write',
             parallel_safe=False, submission=True, output_limit_tokens=2000)
    def resources(name, arguments):
        if name in {'Write', 'Edit', 'NotebookEdit'}:
            return [file_resource(arguments.get('file_path') or arguments['notebook_path'])]
        if name == 'Bash':
            return [file_resource(engine.workspace.root)]
        if name == 'memory.note':
            return [file_resource(memory.path)]
        if name == 'agent.delegate':
            paths = arguments.get('writable_files') or []
            return ([file_resource(engine.workspace.root / path) for path in paths]
                    if paths else ['delegation:' + state.run_id])
        if name in {'TaskCreate', 'TaskUpdate', 'TodoWrite'}:
            return ['run-tasks:' + state.scope_id + ':' + state.run_id]
        if name == 'Skill':
            return ['run-skills:' + state.scope_id + ':' + state.run_id]
        return ['*']

    store = getattr(engine, 'store', None)
    executor = (make_operation_executor(store, state, notify=getattr(engine, '_notify', None))
                if store is not None else None)
    return RuntimeTools(registry, handlers, phase, submissions, executor, resources)
