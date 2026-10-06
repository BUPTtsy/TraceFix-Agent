import asyncio
import builtins
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError

import tracefix.agents.deepagents_adapter as adapter_module
from tracefix.agents.deepagents_adapter import (
    DeepAgentsBoundaryError, DeepAgentsError, DeepAgentsInvestigation,
    DeepAgentsReadonlyAdapter, investigation_model,
)
from tracefix.runtime.contracts import Phase, digest
from tracefix.runtime.effects import file_resource
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.subtask_contracts import SubtaskResult, SubtaskSpec
from tracefix.runtime.worker import ReadOnlyWorker
from tracefix.storage.store import UnknownOperation


class ScriptedModel(BaseChatModel):
    steps: list[Any] = Field(default_factory=list)
    calls: list = Field(default_factory=list)
    summaries: int = 0
    summary_failure: Any = None

    @property
    def _llm_type(self):
        return 'scripted-deepagents-test'

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools)

    def _generate(self, messages, **kwargs):
        raise AssertionError('调查必须异步执行')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((messages, kwargs))
        if 'Context Extraction Assistant' in str(messages[-1].content):
            self.summaries += 1
            if self.summary_failure:
                raise self.summary_failure
            message = AIMessage(content='保留授权文件、证据和版本的上下文摘要')
        else:
            step = self.steps.pop(0)
            if callable(step):
                step = step(messages)
            if isinstance(step, BaseException):
                raise step
            message = step
        message = message.model_copy(deep=True, update={
            'usage_metadata': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5},
            'response_metadata': {'token_usage': {'total_tokens': 5, 'cost_usd': 0.01}}})
        return ChatResult(generations=[ChatGeneration(message=message)])


def request(**overrides):
    values = dict(goal='调查状态流转', allowed_files=('src/app.py', 'src/lib.py'),
        allowed_evidence_refs=('evidence:one', 'evidence:two'),
        source_manifest='manifest-current', worker_generation=7)
    values.update(overrides)
    return DeepAgentsInvestigation(**values)


def result(**overrides):
    values = dict(summary='授权调查结论', evidence_refs=['evidence:one'], files=['src/app.py'],
        suggested_experiments=[], unresolved=[], worker_generation=7,
        source_manifest='manifest-current',
        hypotheses=[dict(id='state-flow', summary='状态更新候选', support_refs=['evidence:one'],
            counterevidence_refs=['evidence:two'], candidate_paths=[dict(
                path='src/app.py', content_version='current-hash')])])
    values.update(overrides)
    return values


def calls(*items):
    return AIMessage(content='', tool_calls=[
        dict(name=name, args=arguments, id=identity, type='tool_call')
        for name, arguments, identity in items])


def submit(payload=None):
    return calls(('SubtaskResult', result() if payload is None else payload, 'submit-1'))


def adapter(model, **options):
    tools = {'Read': lambda **kwargs: 'frozen source',
             'Grep': lambda **kwargs: 'frozen match',
             'Glob': lambda **kwargs: list(kwargs['allowed_files'])}
    tools = options.pop('readonly_tools', tools)
    return DeepAgentsReadonlyAdapter(model=model, readonly_tools=tools, **options)


@pytest.fixture(autouse=True)
def prohibit_network_and_tracing(monkeypatch):
    monkeypatch.setenv('LANGSMITH_TRACING', 'false')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'false')
    monkeypatch.delenv('TRACEFIX_AGENT_MODE', raising=False)
    async def denied(*args, **kwargs):
        raise AssertionError('测试禁止网络')
    monkeypatch.setattr(httpx.AsyncClient, 'send', denied)


@pytest.mark.parametrize('phase', ['DIAGNOSE', 'REVIEW'])
async def test_real_framework_plans_investigates_and_returns_schema(phase):
    injected = []
    delivered = []
    audit = []
    model = ScriptedModel(steps=[
        calls(('write_todos', {'todos': [{'content': '读取源码', 'status': 'in_progress'}]}, 'plan-1')),
        calls(('Read', {'path': 'src/app.py'}, 'read-1'),
              ('Grep', {'path': 'src/lib.py', 'pattern': 'state'}, 'grep-1'),
              ('Glob', {'pattern': 'src/app.*'}, 'glob-1')),
        submit(),
    ])
    tools = {
        'Read': lambda **args: injected.append(('Read', args)) or 'frozen source',
        'Grep': lambda **args: injected.append(('Grep', args)) or 'frozen match',
        'Glob': lambda **args: injected.append(('Glob', args)) or list(args['allowed_files']),
    }
    output = await adapter(model, readonly_tools=tools, on_result=delivered.append,
        audit=lambda kind, payload: audit.append((kind, payload))).run(request(phase=phase))
    assert output.status == 'completed'
    assert output.files == ['src/app.py']
    assert output.hypotheses[0].counterevidence_refs == ['evidence:two']
    assert output.worker_generation == 7 and output.source_manifest == 'manifest-current'
    assert output.usage == {'model_calls': 3, 'tokens': 15, 'cost_usd': 0.03}
    assert {name for name, args in injected} == {'Read', 'Grep', 'Glob'}
    assert next(args for name, args in injected if name == 'Glob')['allowed_files'] == ('src/app.py',)
    assert {args['call_id'] for name, args in injected} == {'read-1', 'grep-1', 'glob-1'}
    assert delivered == [output] and delivered[0] is not output
    assert len([item for item in audit if item[0] == 'request']) == 3
    registered = {tool['function']['name'] for tool in model.calls[0][1]['tools']}
    assert registered == {'Read', 'Grep', 'Glob', 'write_todos', 'SubtaskResult'}


async def test_real_summarization_executes_and_is_audited():
    audit = []
    model = ScriptedModel(steps=[
        calls(('Read', {'path': 'src/app.py'}, 'read-1')),
        calls(('Read', {'path': 'src/lib.py'}, 'read-2')),
        calls(('Grep', {'path': 'src/lib.py', 'pattern': 'state'}, 'grep-1')),
        submit(),
    ])
    output = await adapter(model, summarize_at=1,
        audit=lambda kind, payload: audit.append((kind, payload))).run(request())
    assert output.status == 'completed'
    assert model.summaries >= 1
    assert output.usage['model_calls'] == 4 + model.summaries
    assert output.usage['tokens'] == len(model.calls) * 5
    assert len([entry for entry in audit if entry[0] == 'request']) == len(model.calls)
    assert any('上下文摘要' in str(message.content) for messages, options in model.calls for message in messages)


async def test_summary_failure_has_no_completed_fallback():
    model = ScriptedModel(steps=[
        calls(('Read', {'path': 'src/app.py'}, 'read-1')),
        calls(('Read', {'path': 'src/lib.py'}, 'read-2')),
        submit(),
    ], summary_failure=RuntimeError('summary transport failure'))
    with pytest.raises(DeepAgentsError):
        await adapter(model, summarize_at=1).run(request())


@pytest.mark.parametrize('name', ['Write', 'Edit', 'Bash', 'Git', 'BrowserNavigate',
                                   'DatabaseQuery', 'mcp.dynamic', 'write_file', 'task'])
async def test_unauthorized_model_tool_is_rejected_before_execution(name):
    model = ScriptedModel(steps=[calls((name, {}, 'forbidden'))])
    output = await adapter(model).run(request())
    assert output.status == 'rejected'
    assert output.gaps == ['deepagents_tool_denied']
    with pytest.raises(DeepAgentsBoundaryError):
        DeepAgentsReadonlyAdapter(model=model, readonly_tools={name: lambda: None})


@pytest.mark.parametrize('path', ['', '.', 'src/./app.py', '../secret.py', 'src/../app.py',
    '/src/app.py', 'C:/src/app.py', 'C:app.py', '//host/share', 'src\\app.py',
    'src/app\x00.py', 'src/app\x7f.py', 'src/*.py'])
def test_input_file_paths_are_canonical(path):
    with pytest.raises(ValidationError):
        request(allowed_files=(path,))


@pytest.mark.parametrize('phase', ['EXPLORE', 'PATCH', 'VERIFY'])
def test_phase_cannot_expand_existing_worker_authorization(phase):
    with pytest.raises(ValidationError):
        request(phase=phase)


@pytest.mark.parametrize('tool,args', [
    ('Read', {'path': 'src/secret.py'}), ('Read', {'path': '../secret.py'}),
    ('Grep', {'path': '/secret.py', 'pattern': 'secret'}),
    ('Glob', {'pattern': '../*'}),
])
async def test_file_scope_is_checked_before_injected_callable(tool, args):
    injected = []
    tools = {name: lambda **kwargs: injected.append(kwargs) or '' for name in ('Read', 'Grep', 'Glob')}
    output = await adapter(ScriptedModel(steps=[calls((tool, args, 'forbidden'))]),
                           readonly_tools=tools).run(request())
    assert output.status == 'rejected'
    assert output.gaps == ['deepagents_scope_denied']
    assert injected == []


async def test_glob_output_cannot_escape_prefiltered_scope():
    output = await adapter(ScriptedModel(steps=[calls(('Glob', {'pattern': 'src/app.*'}, 'glob-1'))]),
        readonly_tools={'Glob': lambda **kwargs: ['src/lib.py']}).run(request())
    assert output.status == 'rejected'
    assert output.gaps == ['deepagents_scope_denied']


@pytest.mark.parametrize('field', ['files', 'evidence_refs', 'support_refs', 'counterevidence_refs',
                                   'artifact_refs', 'hypothesis_support', 'hypothesis_counter', 'candidate'])
async def test_all_output_paths_and_nested_references_are_scoped(field):
    payload = result()
    if field == 'candidate':
        payload['hypotheses'][0]['candidate_paths'][0]['path'] = 'src/secret.py'
    elif field.startswith('hypothesis_'):
        name = 'support_refs' if field == 'hypothesis_support' else 'counterevidence_refs'
        payload['hypotheses'][0][name] = ['evidence:secret']
    else:
        payload[field] = ['src/secret.py' if field == 'files' else 'evidence:secret']
    output = await adapter(ScriptedModel(steps=[submit(payload)])).run(request())
    assert output.status == 'rejected'
    assert output.gaps == ['deepagents_scope_denied']
    assert output.files == output.evidence_refs == output.hypotheses == []


@pytest.mark.parametrize('field,value', [('worker_generation', 6), ('source_manifest', 'old-source')])
async def test_snapshot_mismatch_is_rejected(field, value):
    output = await adapter(ScriptedModel(steps=[submit(result(**{field: value}))])).run(request())
    assert output.status == 'rejected' and output.gaps == ['deepagents_version_mismatch']
    assert output.worker_generation == 7 and output.source_manifest == 'manifest-current'


async def test_framework_failures_never_return_completed():
    model = ScriptedModel(steps=[RuntimeError('provider disconnected')])
    events = []
    with pytest.raises(DeepAgentsError) as caught:
        await adapter(model, audit=lambda kind, value: events.append((kind, value))).run(request())
    assert caught.value.status == 'UNKNOWN_OPERATION'
    assert [kind for kind, value in events] == ['request', 'error']


@pytest.mark.parametrize('failure', [asyncio.CancelledError(), TimeoutError('timeout')])
async def test_cancel_and_timeout_propagate_and_keep_request_audit(failure):
    events = []
    delivered = []
    model = ScriptedModel(steps=[submit(), failure])
    model.steps = [failure]
    with pytest.raises(type(failure)):
        await adapter(model, audit=lambda kind, value: events.append((kind, value)),
                      on_result=delivered.append).run(request())
    assert events[0][0] == 'request'
    assert any(kind == 'error' for kind, value in events)
    assert delivered == []


async def test_bounded_requests_and_plan_size():
    model = ScriptedModel(steps=[calls(('Read', {'path': 'src/app.py'}, 'read-1')), submit()])
    with pytest.raises(DeepAgentsError, match='上界'):
        await adapter(model, max_model_calls=1).run(request())
    assert len(model.calls) == 1
    model = ScriptedModel(steps=[calls(('write_todos', {'todos': [
        {'content': str(index), 'status': 'pending'} for index in range(13)]}, 'plan-1'))])
    output = await adapter(model).run(request())
    assert output.status == 'rejected' and output.gaps == ['deepagents_plan_limit']


def test_gateway_configuration_builds_real_langchain_model():
    config = SimpleNamespace(base_url='https://provider.invalid/v1', key='test-secret',
        text_model='host-text', max_output_tokens=1234, timeout=15, stream=True,
        thinking='enabled', reasoning_effort='high')
    model = investigation_model(config)
    assert isinstance(model, BaseChatModel)
    assert model.model_name == 'host-text'
    assert model.openai_api_base == config.base_url
    assert model.openai_api_key.get_secret_value() == config.key
    assert model.max_tokens == 1234 and model.request_timeout == 15
    assert model.streaming is True and model.max_retries == 0
    assert model.extra_body == {'thinking': {'type': 'enabled'}}
    assert model.reasoning_effort == 'high'
    assert investigation_model(SimpleNamespace(teacher=model)) is model
    task = request(model_configuration=config)
    assert 'model_configuration' not in task.model_dump()
    assert config.key not in repr(task)


def test_missing_dependency_is_hard_failure_without_fallback(monkeypatch):
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name.startswith('deepagents'):
            raise ImportError('missing dependency')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    import importlib.util
    spec = importlib.util.spec_from_file_location('readonly_missing_test', adapter_module.__file__)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match='依赖未安装'):
        spec.loader.exec_module(module)


async def test_dependency_version_is_pinned_without_fallback(monkeypatch):
    monkeypatch.setattr(adapter_module, 'version', lambda name: '0.0.0')
    with pytest.raises(DeepAgentsError, match='0.4.12'):
        await adapter(ScriptedModel(steps=[submit()])).run(request())


def worker_fixture(tmp_path, model):
    engine, state = make_engine(tmp_path)
    state.phase = Phase.DIAGNOSE
    reference = engine.artifacts.put(state.scope_id, state.run_id, {'observation': 'frozen'})
    state.evidence_refs = [reference]
    engine.model = model
    engine.store.save(state)
    spec = SubtaskSpec('只读调查源码', 'code-explorer', state.revision, ('src/value.ts',), (reference,))
    return engine, state, spec


def worker_payload(state, reference):
    return dict(summary='调查完成', evidence_refs=[reference], files=['src/value.ts'],
        suggested_experiments=[], unresolved=[], worker_generation=state.revision,
        source_manifest=state.source_manifest)


async def test_worker_leaf_uses_deepagents_and_keeps_child_checkpoint_and_usage(tmp_path, monkeypatch):
    model = ScriptedModel()
    engine, state, spec = worker_fixture(tmp_path, model)
    model.steps = [calls(('Read', {'path': 'src/value.ts'}, 'read-1')),
                   submit(worker_payload(state, spec.allowed_artifacts[0]))]
    async def old_executor(*args, **kwargs):
        raise AssertionError('旧调查 executor 不得调用')
    monkeypatch.setattr(engine, 'model_call', old_executor)
    engine.worker_model = SimpleNamespace(generate=old_executor)
    monkeypatch.setenv('TRACEFIX_WORKER', '0')
    output = await ReadOnlyWorker(engine).run(state, spec)
    child = engine.store.load(output.worker_id, state.scope_id)
    assert child.parent_run_id == state.run_id and child.revision == 0
    assert output.usage['model_calls'] == 2 and output.usage['tokens'] == 10
    assert state.budget.model_calls == 2 and state.budget.tokens == 10
    assert state.budget.cost_usd == 0.02
    assert state.phase == Phase.DIAGNOSE and state.patch_ref is None
    assert engine.workspace.read('src/value.ts') == 'export const persisted = false;\n'
    assert engine.graph.checkpointer.get_tuple({'configurable': {'thread_id': output.worker_id}}) is not None
    assert engine.graph.checkpointer.get_tuple({'configurable': {'thread_id': state.run_id}}) is None
    assert child.model_exchange_refs
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())
    assert 'model_configuration' not in model.calls[0][0][-1].content


@pytest.mark.parametrize('failure', [RuntimeError('disconnected'), asyncio.CancelledError(), TimeoutError('timeout')])
async def test_worker_failure_and_cancel_merge_all_observed_usage(tmp_path, failure):
    model = ScriptedModel()
    engine, state, spec = worker_fixture(tmp_path, model)
    model.steps = [calls(('Read', {'path': 'src/value.ts'}, 'read-1')), failure]
    expected = type(failure) if isinstance(failure, (asyncio.CancelledError, TimeoutError)) else DeepAgentsError
    with pytest.raises(expected):
        await ReadOnlyWorker(engine).run(state, spec)
    assert state.budget.model_calls == 2 and state.budget.tokens == 5
    assert state.budget.cost_usd == 0.01
    children = [run for run in engine.store.runs.values() if run.get('parent_run_id') == state.run_id]
    assert len(children) == 1
    assert any(event['type'] == 'model.error.persisted' for event in engine.store.events[children[0]['run_id']])


@pytest.mark.parametrize('drift', ['source', 'generation', 'manifest'])
async def test_worker_refuses_investigation_drift(tmp_path, drift):
    model = ScriptedModel()
    engine, state, spec = worker_fixture(tmp_path, model)
    def changed(messages):
        if drift == 'source':
            (engine.workspace.root / 'src/value.ts').write_text('changed', encoding='utf-8')
        elif drift == 'generation':
            state.revision += 1
            engine.store.save(state)
        else:
            state.source_manifest = 'new-manifest'
            engine.store.save(state)
        return submit(worker_payload(state, spec.allowed_artifacts[0]))
    model.steps = [changed]
    with pytest.raises(ValueError, match='版本'):
        await ReadOnlyWorker(engine).run(state, spec)
    assert state.budget.model_calls == 1


async def test_unknown_resource_fence_blocks_host_tools_and_propagates(tmp_path):
    model = ScriptedModel()
    engine, state, spec = worker_fixture(tmp_path, model)
    engine.store.begin(state, 'unknown-source', {'resources': [
        file_resource(engine.workspace.root / 'src/value.ts')]})
    engine.store.mark_unknown(state, 'unknown-source', reason='test')
    model.steps = [calls(('Read', {'path': 'src/value.ts'}, 'read-1'))]
    with pytest.raises(UnknownOperation):
        await ReadOnlyWorker(engine).run(state, spec)
    assert engine.store.operations['unknown-source']['status'] == 'UNKNOWN'


@pytest.mark.parametrize('phase,role', [('EXPLORE', 'code-explorer'), ('REVIEW', 'code-explorer')])
async def test_worker_phase_permission_precedes_framework(tmp_path, phase, role):
    model = ScriptedModel()
    engine, state, spec = worker_fixture(tmp_path, model)
    state.phase = Phase(phase)
    spec = SubtaskSpec(spec.goal, role, spec.generation, spec.allowed_files, spec.allowed_artifacts)
    with pytest.raises(PermissionError):
        await ReadOnlyWorker(engine).run(state, spec)
    assert model.calls == [] and state.budget.subtasks == 0
