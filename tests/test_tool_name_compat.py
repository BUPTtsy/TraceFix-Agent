import copy
import json
import re

import httpx
import pytest

from tracefix.model.gateway import Gateway, ModelOutputError
from tracefix.model import protocol
from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.runtime.tool_handlers import EmptyInput, MemorySearch, RuleGet
from tracefix.runtime.tools import model_tool_name, ToolPipeline, ToolProtocolError, ToolRegistry, ToolSpec


class Result(Contract):
    summary: str


def call(name, call_id='lookup', arguments=None):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': model_tool_name(name), 'arguments': json.dumps(arguments or {})}}


def completion(calls=None):
    message = {'role': 'assistant', 'content': None if calls else '{"summary":"done"}'}
    if calls:
        message['tool_calls'] = calls
    return httpx.Response(200, json={'id': 'fixture', 'created': 0, 'object': 'chat.completion',
        'model': 'fixture', 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop', 'message': message}]})


def responses(monkeypatch, values):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return values.pop(0)

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    return requests


def test_native_tool_names_are_provider_safe_and_resolve_to_internal_ids():
    specs = [ToolSpec('rules.applicable', '适用规则', EmptyInput),
             ToolSpec('rules.get', '读取规则', RuleGet),
             ToolSpec('memory.search', '检索记忆', MemorySearch),
             ToolSpec('memory.note', '写入记忆', EmptyInput, side_effect='write',
                      idempotency_key=digest),
             ToolSpec('code.' + 'a' * 100, '读取代码', EmptyInput),
             ToolSpec('code.' + 'a' * 99 + 'b', '读取其他代码', EmptyInput)]
    registry = ToolRegistry(specs)
    native = registry.native_tools(Phase.EXPLORE)
    assert [tool['function']['name'] for tool in native[:4]] == [
        'RulesApplicable', 'RulesGet', 'MemorySearch', 'MemoryNote']
    for tool, spec in zip(native, specs):
        name = tool['function']['name']
        assert re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name)
        assert registry.get_wire(name, Phase.EXPLORE) is spec
        assert registry.get(spec.name, Phase.EXPLORE) is spec
    assert len({tool['function']['name'] for tool in native}) == len(specs)


async def test_pipeline_wire_alias_and_internal_name_share_completed_identity():
    registry = ToolRegistry([ToolSpec('rules.applicable', '适用规则', EmptyInput)])
    executed = []

    async def handler(arguments, call_id):
        executed.append(call_id)
        return {'rules': []}

    pipeline = ToolPipeline(registry, {'rules.applicable': handler}, Phase.DIAGNOSE)
    first = await pipeline.execute('RulesApplicable', {}, 'lookup')
    second = await pipeline.execute('rules.applicable', {}, 'lookup')
    assert first == second
    assert first.name == 'rules.applicable'
    assert pipeline.completed_calls['lookup'][0][0] == 'rules.applicable'
    assert executed == ['lookup']


def test_alias_collisions_and_phase_escalation_are_rejected():
    spec = ToolSpec('rules.get', '读取规则', RuleGet, phases=frozenset({Phase.DIAGNOSE}))
    registry = ToolRegistry([spec])
    with pytest.raises(ValueError, match='别名重复'):
        registry.register(ToolSpec('RulesGet', '其他工具', EmptyInput))
    with pytest.raises(ToolProtocolError, match='未授权'):
        registry.validate_batch([call('RulesGet', arguments={'rule_id': 'rule'})],
                                Phase.EXPLORE, wire_names=True)
    invalid = call('RulesGet', arguments={'rule_id': 'rule'})
    invalid['function']['name'] = 'rules.get'
    with pytest.raises(ToolProtocolError):
        registry.validate_batch([invalid], Phase.DIAGNOSE, wire_names=True)


async def test_gateway_alias_executes_internal_handler_and_preserves_receipt_identity(monkeypatch):
    lookup = call('MemoryNote')
    requests = responses(monkeypatch, [completion([lookup]), completion([lookup]), completion()])
    operations, events, executed = [], [], []
    registry = ToolRegistry([ToolSpec('memory.note', '写入记忆', EmptyInput,
                            side_effect='write', idempotency_key=digest)])

    async def handler(arguments, call_id):
        executed.append(call_id)
        return {'saved': True}

    async def operation(name, intent, perform, *, idempotency_key):
        operations.append((name, intent, idempotency_key))
        return await perform()

    pipeline = ToolPipeline(registry, {'memory.note': handler}, Phase.DIAGNOSE,
                            operation=operation,
                            emit=lambda kind, payload: events.append((kind, payload)))
    result = await Gateway(key='fixture', stream=False).generate(Result,
        {'phase': 'DIAGNOSE'}, tool_pipeline=pipeline)
    assert result.value.summary == 'done'
    assert executed == ['lookup']
    assert operations[0][0] == 'memory.note'
    assert operations[0][1]['tool_name'] == 'memory.note'
    assert pipeline.completed_calls['lookup'][0][0] == 'memory.note'
    assert requests[1]['messages'][-2]['tool_calls'] == [lookup]
    assert requests[1]['messages'][-1]['name'] == 'MemoryNote'
    assert json.loads(requests[1]['messages'][-1]['content'])['name'] == 'memory.note'
    assert requests[2]['messages'][-1]['content'] == requests[1]['messages'][-1]['content']


@pytest.mark.parametrize('history_name', ['rules.applicable', 'RulesApplicable'])
async def test_completed_tool_history_normalizes_alias_without_reexecuting(monkeypatch, history_name):
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [call(history_name)]},
        {'role': 'tool', 'tool_call_id': 'lookup', 'name': history_name,
         'content': '{"rules":[]}'},
    ]
    original = copy.deepcopy(history)
    requests = responses(monkeypatch, [completion([call('RulesApplicable')]), completion()])
    registry = ToolRegistry([ToolSpec('rules.applicable', '适用规则', EmptyInput)])

    async def handler(*args):
        pytest.fail('已完成的工具调用不能重复执行')

    result = await Gateway(key='fixture', stream=False).generate(Result, {}, messages=history,
        tool_registry=registry, tool_executor=handler)
    assert result.value.summary == 'done'
    assert history == original
    assistant = next(message for message in requests[0]['messages'] if message['role'] == 'assistant')
    tool = next(message for message in requests[0]['messages'] if message['role'] == 'tool')
    assert assistant['tool_calls'][0]['function']['name'] == 'RulesApplicable'
    assert tool['name'] == 'RulesApplicable'
    assert requests[1]['messages'][-1]['content'] == '{"rules":[]}'


async def test_alias_dispatches_to_internal_executor_and_policy_uses_wire_names(monkeypatch):
    registry = [ToolSpec('agent.delegate', '派发任务', EmptyInput)]
    requests = responses(monkeypatch, [completion([call('AgentDelegate')]), completion()])
    executed = []

    async def executor(name, arguments, call_id):
        executed.append((name, arguments, call_id))
        return {'queued': True}

    await Gateway(key='fixture', stream=False, additional_tools=registry).generate(
        Result, {}, tool_executor=executor)
    assert executed == [('agent.delegate', {}, 'lookup')]
    assert requests[1]['messages'][-1]['name'] == 'AgentDelegate'
    assert '<supervisor_tool_protocol>' in requests[0]['messages'][0]['content']
    assert '每次' in requests[0]['messages'][0]['content']


async def test_unknown_wire_alias_is_rejected_before_any_execution(monkeypatch):
    invalid = call('RulesGet', 'bad')
    invalid['function']['name'] = 'rules.get'
    requests = responses(monkeypatch, [completion([call('RulesApplicable'), invalid])])
    registry = ToolRegistry([ToolSpec('rules.applicable', '适用规则', EmptyInput)])

    async def executor(*args):
        pytest.fail('批次校验失败时不能执行工具')

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fixture', stream=False).generate(Result, {},
            tool_registry=registry, tool_executor=executor)
    assert raised.value.category == 'tool_protocol'
    assert len(requests) == 1


async def test_batch_rejected_submission_is_audited_before_context_retry(monkeypatch):
    from tracefix.runtime.tools import ToolRejected

    requests = responses(monkeypatch, [completion([call('propose_patch')])])
    registry = ToolRegistry([ToolSpec('propose_patch', '提交补丁', EmptyInput,
        side_effect='write', idempotency_key=digest, submission=True)])
    audit = []

    async def reject(arguments, call_id):
        raise ToolRejected('补丁没有实际改动')

    async def operation(name, intent, perform, **kwargs):
        return await perform()

    pipeline = ToolPipeline(registry, {'propose_patch': reject}, Phase.DIAGNOSE,
                            operation=operation)
    with pytest.raises(ModelOutputError, match='补充诊断上下文') as raised:
        await Gateway(key='fixture', stream=False).generate(Result,
            {'phase': 'DIAGNOSE', 'execution_mode': 'batch'}, tool_pipeline=pipeline,
            on_tool_result=lambda exchange, result: audit.append(('tool', result)),
            on_error=lambda exchange, error: audit.append(('error', error)))
    assert len(requests) == 1
    assert [kind for kind, record in audit] == ['tool', 'error']
    assert json.loads(audit[0][1]['message']['content'])['executed'] is False
    assert raised.value.details['requires_manual_review'] is False
    assert raised.value.details['submission_rejected'] is True
    assert pipeline.submission_value is None


async def test_native_batch_noop_submissions_refresh_context_and_produce_valid_patch(tmp_path, monkeypatch):
    from tracefix.runtime.contracts import PatchProposal, RunStatus, Outcome
    from tracefix.runtime.smoke import make_engine

    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    engine.subagent_enabled = False
    inner = engine.model
    gateway = Gateway(key='fixture', stream=False)
    contexts, requests = [], []

    class NativePatches:
        supports_tool_executor = True
        supports_context_assembler = True

        async def generate(self, schema, context, **kwargs):
            if schema is PatchProposal:
                contexts.append(copy.deepcopy(context))
                return await gateway.generate(schema, context, **kwargs)
            callbacks = {name: value for name, value in kwargs.items() if name in {
                'image', 'agent_instructions', 'on_attempt', 'on_response', 'on_error', 'on_usage'}}
            return await inner.generate(schema, context, **callbacks)

    async def handle(request):
        requests.append(json.loads(request.content))
        context = contexts[-1]
        source = next(card for card in context['cards'] if card['path'] == 'src/value.ts')
        proposal = {'summary': '修复状态持久化', 'evidence_refs': context['available_evidence_refs'][-1:],
                    'edits': [{'path': source['path'], 'before_hash': source['before_hash'],
                              'content': source['content'] if len(contexts) <= 2
                              else 'export const persisted = true;\n'}]}
        return completion([call('propose_patch', 'patch-' + str(len(contexts)), proposal)])

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    engine.model = NativePatches()
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED, finished.error
    assert finished.outcome == Outcome.FIX_VERIFIED
    assert len(requests) == len(contexts) == 3
    assert [context['diagnosis_retry_count'] for context in contexts] == [0, 1, 2]
    assert len(contexts[2]['diagnosis_feedback']) == 2
    assert all(context['execution_mode'] == 'batch' for context in contexts)
    assert all(context['available_evidence_refs'][-1] in context['evidence_refs'] for context in contexts)
    report = engine.get(finished, finished.report_ref)
    assert report['patch_verification'] == 'verified'


@pytest.mark.parametrize('reference_kind', ['artifact', 'observation_id', 'screenshot'])
async def test_batch_finish_exploration_accepts_current_observation_evidence(
        tmp_path, monkeypatch, reference_kind):
    from tracefix.runtime.contracts import Decision
    from tracefix.runtime.smoke import make_engine
    from tracefix.runtime.tool_handlers import build_runtime_tools

    engine, state = make_engine(tmp_path)
    state.phase = Phase.EXPLORE
    state.execution_mode = 'batch'
    # 副作用 operation 的审计通知必须读取已登记的 RunState；生产路径由创建 Run 时完成登记。
    engine.store.save(state)
    screenshot = engine.put(state, b'fixture', 'png')
    observation = {'id': 'current-observation', 'snapshot': '', 'screenshot_ref': screenshot}
    state.observation_ref = engine.put(state, observation)
    references = {'artifact': state.observation_ref, 'observation_id': observation['id'],
                  'screenshot': screenshot}
    runtime = build_runtime_tools(engine, state, Decision, {'observation': observation})

    async def operation(name, intent, perform, **kwargs):
        return await perform()

    requests = responses(monkeypatch, [completion([call('finish_exploration', arguments={
        'action': {'kind': 'finish'}, 'evidence_refs': [references[reference_kind]]})])])
    result = await Gateway(key='fixture', stream=False).generate(Decision,
        {'phase': 'EXPLORE', 'execution_mode': 'batch'},
        tool_pipeline=runtime.pipeline(operation=operation))
    assert len(requests) == 1
    assert result.value.evidence_refs == [state.observation_ref]
