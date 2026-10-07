import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

pytest.importorskip('pydantic_ai')
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel

from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter, PydanticAIAdapterError
from tracefix.model import protocol
from tracefix.model.chat import stream_tool_chat
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import BrowserAction, Decision
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolResult, ToolSpec


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answer: int


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    index: int


def model(function):
    result = FunctionModel(function)
    result.stream = False
    return result


def typed_response(answer=42):
    return ModelResponse(parts=[TextPart(json.dumps({'answer': answer}))])


def tool_spec(name='repo.read', **options):
    return ToolSpec(name, 'Read injected evidence', Arguments, **options)


def transport(monkeypatch, responses):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(
            httpx.MockTransport(handle), boundary), event_hooks={
                'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    return requests


def response(content='{"answer":42}', *, calls=None, usage=None, finish='stop', status=200):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return httpx.Response(status, json={'id': 'provider-response', 'object': 'chat.completion',
        'created': 0, 'model': 'provider-revision', 'usage': usage or {
            'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8},
        'choices': [{'index': 0, 'finish_reason': finish, 'message': message}]})


def call(name='RepoRead', call_id='provider-call-9', arguments=None):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps({'index': 1} if arguments is None else arguments)}}


@pytest.mark.parametrize('schema', [BrowserAction, Decision])
async def test_gateway_always_exposes_native_browser_tools(monkeypatch, schema):
    monkeypatch.setenv('TRACEFIX_TOOL_MODE', 'json')
    finished = {'kind': 'finish'} if schema is BrowserAction else {'action': {'kind': 'finish'}}
    requests = transport(monkeypatch, [response(None, calls=[
        call('BrowserSnapshot', 'snapshot-1', {})], finish='tool_calls'),
        response(json.dumps(finished))])
    executed = []

    def execute(name, arguments, call_id):
        executed.append((name, arguments, call_id))
        return {'observation': {'id': 'observed', 'snapshot': '- heading "Ready"'}}

    result = await Gateway(key='fake', stream=False).generate(schema, {'phase': 'EXPLORE'},
        tool_executor=execute)
    assert executed == [('browser.snapshot', {}, 'snapshot-1')]
    assert {tool['function']['name'] for tool in requests[0]['tools']} == {
        'BrowserNavigate', 'BrowserClick', 'BrowserType', 'BrowserSelect',
        'BrowserPress', 'BrowserSnapshot'}
    assert requests[1]['messages'][-1]['tool_call_id'] == 'snapshot-1'
    action = result.value if schema is BrowserAction else result.value.action
    assert action.kind == 'finish'


@pytest.mark.parametrize('schema', [BrowserAction, Decision])
@pytest.mark.parametrize('output_tool', [False, True])
@pytest.mark.parametrize('kind', ['observe', 'click'])
async def test_gateway_rejects_browser_actions_in_final_output(monkeypatch, schema, output_tool, kind):
    action = {'kind': kind, 'observation_id': 'current', 'element_ref': 'e1',
              'locator': {'role': 'button', 'name': 'Save'}}
    output = action if schema is BrowserAction else {'action': action}
    reply = response(None, calls=[call('final_result', 'returned-action', output)],
        finish='tool_calls') if output_tool else response(json.dumps(output))
    requests = transport(monkeypatch, [reply])
    tools = [ToolSpec('finish_exploration', 'Finish exploration', schema, side_effect='write',
        idempotency_key=lambda value: value.model_dump_json(), submission=True,
        submission_schema=schema)] if output_tool else []
    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fake', stream=False, max_attempts=1).generate(schema, {}, tools=tools,
            tool_executor=lambda *args: pytest.fail('returned browser action must not execute'))
    assert raised.value.category == 'output_validation'
    assert len(requests) == 1


async def test_gateway_corrects_non_finish_browser_output_without_executing_it(monkeypatch):
    requests = transport(monkeypatch, [response('{"kind":"observe"}'),
                                       response('{"kind":"finish"}')])
    result = await Gateway(key='fake', stream=False, max_attempts=2).generate(BrowserAction, {},
        tool_executor=lambda *args: pytest.fail('returned browser action must not execute'))
    assert result.value.kind == 'finish'
    assert len(requests) == 2
    assert 'finish' in requests[1]['messages'][-1]['content']


async def test_real_agent_typed_output_and_bounded_correction():
    requests = []

    def execute(messages, info):
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return typed_response('invalid')
        return typed_response()

    result = await PydanticAIAdapter(model(execute), output_retries=1).generate(Output, {})
    assert result.value == Output(answer=42)
    assert len(requests) == 2
    assert any(part.part_kind == 'retry-prompt' for message in requests[1]
               for part in message.parts)
    assert result.usage['requests'] == 2


async def test_real_agent_tool_schema_id_and_read_concurrency_bound():
    active = peak = 0
    executed = []

    async def port(name, arguments, call_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        executed.append(call_id)
        active -= 1
        return {'evidence_ref': call_id}

    def execute(messages, info):
        if len(messages) == 1:
            assert info.function_tools[0].parameters_json_schema['additionalProperties'] is False
            return ModelResponse(parts=[ToolCallPart('RepoRead', {'index': index},
                tool_call_id=f'real-call-{index}') for index in range(6)])
        assert [part.tool_call_id for part in messages[-1].parts] == [
            f'real-call-{index}' for index in range(6)]
        return typed_response()

    await PydanticAIAdapter(model(execute)).generate(Output, {},
        tools=[tool_spec(parallel_safe=True)], tool_executor=port, max_concurrency=2)
    assert peak == 2
    assert len(executed) == 6


async def test_real_agent_writes_are_serial_and_unknown_is_terminal():
    executed = []

    async def port(name, arguments, call_id):
        executed.append(call_id)
        if arguments['index'] == 2:
            raise OSError('receipt unavailable')
        return {'saved': True}

    def execute(messages, info):
        return ModelResponse(parts=[ToolCallPart('RepoWrite', {'index': index},
            tool_call_id=f'write-{index}') for index in range(1, 4)])

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model(execute)).generate(Output, {}, tools=[tool_spec(
            'repo.write', side_effect='write', idempotency_key=lambda value: str(value.index))],
            tool_executor=port)
    assert executed == ['write-1', 'write-2']
    assert raised.value.status == 'UNKNOWN_OPERATION'


async def test_real_submission_is_an_output_tool_executed_by_host_pipeline():
    executed, operations = [], []
    spec = ToolSpec('submit.answer', 'Submit a verified answer', Output, side_effect='write',
        idempotency_key=lambda value: str(value.answer), submission=True, submission_schema=Output)

    async def submit(value, call_id):
        executed.append(call_id)
        return {'value': value.model_dump()}

    async def operation(name, intent, perform, **options):
        operations.append(name)
        return await perform()

    pipeline = ToolPipeline(ToolRegistry([spec]), {'submit.answer': submit}, 'DIAGNOSE',
                            operation=operation)

    def execute(messages, info):
        assert 'SubmitAnswer' not in {tool.name for tool in info.function_tools}
        assert 'SubmitAnswer' in {tool.name for tool in info.output_tools}
        return ModelResponse(parts=[ToolCallPart('SubmitAnswer', {'answer': 42},
                                                tool_call_id='submission-original-id')])

    result = await PydanticAIAdapter(model(execute)).generate(Output, {}, tool_pipeline=pipeline)
    assert result.value == pipeline.submission_value == Output(answer=42)
    assert executed == ['submission-original-id']
    assert operations == ['submit.answer']


async def test_provider_records_every_correction_usage_and_original_details(monkeypatch):
    requests = transport(monkeypatch, [response('{"answer":"invalid"}'), response()])
    attempts, usages, audits = [], [], []
    result = await Gateway(key='fake', stream=False).generate(Output, {},
        on_attempt=lambda name, request, attempt: attempts.append(request) or {'attempt': attempt},
        on_usage=usages.append, on_response=lambda exchange, raw: audits.append(raw))
    assert result.value == Output(answer=42)
    assert len(requests) == len(attempts) == len(usages) == len(audits) == 2
    assert result.usage['total_tokens'] == 16
    assert [audit['body']['usage']['total_tokens'] for audit in audits] == [8, 8]
    assert result.model_revision == 'provider-revision'


@pytest.mark.parametrize('failure,status,category', [
    (httpx.ReadError('disconnected'), 'UNKNOWN_OPERATION', 'stream_interrupted'),
    (httpx.ConnectError('offline'), 'WAITING_NETWORK', 'network'),
])
async def test_provider_transport_failure_is_audited_once_without_retry(
        monkeypatch, failure, status, category):
    requests = transport(monkeypatch, [failure])
    errors = []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {},
            on_error=lambda exchange, error: errors.append(error))
    assert len(requests) == len(errors) == 1
    assert raised.value.status == status
    assert raised.value.category == category


async def test_provider_history_is_reused_and_invalid_batch_cannot_execute(monkeypatch):
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [call()]},
               {'role': 'tool', 'name': 'RepoRead', 'tool_call_id': 'provider-call-9',
                'content': '{"evidence_ref":"saved"}'}]
    requests = transport(monkeypatch, [response(None, calls=[call()], finish='tool_calls'), response()])

    def port(*args):
        pytest.fail('restored call must not execute again')

    result = await Gateway(key='fake', stream=False).generate(Output, {}, messages=history,
        tool_registry=ToolRegistry([tool_spec()]), tool_executor=port)
    assert result.value.answer == 42
    assert len(requests) == 2
    assert 'saved' in requests[1]['messages'][-1]['content']


async def test_provider_validates_whole_batch_before_any_write(monkeypatch):
    transport(monkeypatch, [response(None, calls=[call('RepoWrite'), call('NotRegistered', 'bad')],
                                    finish='tool_calls')])
    spec = tool_spec('repo.write', side_effect='write', idempotency_key=lambda value: str(value.index))
    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {}, tools=[spec],
            tool_executor=lambda *args: pytest.fail('invalid batch cannot execute'))
    assert raised.value.category == 'tool_protocol'


async def test_every_request_has_budget_and_raw_usage_even_during_output_correction(monkeypatch):
    vendor_usage = {'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8,
                    'prompt_tokens_details': {'cached_tokens': 4},
                    'completion_tokens_details': {'reasoning_tokens': 2}}
    requests = transport(monkeypatch, [response('{"answer":"invalid"}', usage=vendor_usage),
                                       response(usage=vendor_usage)])
    attempts, manifests, usages = [], [], []
    result = await Gateway(key='fake', stream=False).generate(Output, {},
        on_attempt=lambda name, record, number: attempts.append(record),
        on_context=lambda manifest, compacted: manifests.append(manifest),
        on_usage=usages.append)
    assert len(requests) == len(attempts) == len(manifests) == len(usages) == 2
    assert all(record['context_manifest'] == manifest for record, manifest in zip(attempts, manifests))
    assert all(manifest['request_tokens'] <= manifest['input_limit'] for manifest in manifests)
    assert all(manifest['exact_tokenizer'] is False for manifest in manifests)
    assert result.usage['prompt_tokens_details']['cached_tokens'] == 8
    assert result.usage['completion_tokens_details']['reasoning_tokens'] == 4
    assert usages == [vendor_usage, vendor_usage]


@pytest.mark.parametrize('schema', [Output, str])
async def test_context_budget_exhaustion_preserves_pause_without_sending(monkeypatch, schema):
    requests = transport(monkeypatch, [])
    monkeypatch.setenv('TRACEFIX_CONTEXT_WINDOW', '4096')
    errors = []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False, max_output_tokens=256).generate(schema,
            {'instruction': 'protected' * 10000},
            messages=[{'role': 'user', 'content': 'protected' * 10000}] if schema is str else None,
            on_error=lambda exchange, error: errors.append(error))
    assert requests == []
    assert raised.value.category == 'context_window'
    assert raised.value.status == 'PAUSED'
    assert raised.value.details['requires_manual_review'] is False
    assert len(errors) == 1


async def test_completed_tool_before_disconnect_keeps_serializable_audit_and_receipt(monkeypatch):
    requests = transport(monkeypatch, [response(None, calls=[call()], finish='tool_calls'),
                                       httpx.ReadError('disconnected after tool')])
    errors, executed = [], []

    def port(name, arguments, call_id):
        executed.append(call_id)
        return {'evidence_ref': 'completed-evidence'}

    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {}, tools=[tool_spec()],
            tool_executor=port, on_error=lambda exchange, error: errors.append(error))
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == 2
    assert executed == ['provider-call-9']
    assert len(errors) == 1
    assert errors[0]['tool_results'][0]['tool_call_id'] == 'provider-call-9'
    assert errors[0]['tool_results'][0]['result'] == {'evidence_ref': 'completed-evidence'}
    assert errors[0]['message_history'][-1]['tool_call_id'] == 'provider-call-9'
    assert errors[0]['usage']['total_tokens'] == 8
    json.dumps(errors[0])


async def test_unknown_receipt_is_preserved_when_result_and_error_callbacks_fail(monkeypatch):
    requests = transport(monkeypatch, [response(None, calls=[call()], finish='tool_calls')])

    def fail_callback(*arguments):
        raise ValueError('audit callback unavailable')

    def port(name, arguments, call_id):
        return ToolResult(call_id=call_id, name=name, isError=True, executed=True,
            error={'status': 'UNKNOWN_OPERATION', 'operation_id': 'operation-9'})

    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {}, tools=[tool_spec()],
            tool_executor=port, on_tool_result=fail_callback, on_error=fail_callback)
    assert len(requests) == 1
    assert raised.value.category == 'tool_execution'
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['operation_id'] == 'operation-9'
    assert 'callback_error' in raised.value.details
    assert 'audit_callback_error' in raised.value.details


@pytest.mark.parametrize('body', [[], {'choices': [None]}, {'choices': []},
    {'choices': [{'finish_reason': 'stop', 'message': None}]},
    {'choices': [{'finish_reason': 'stop', 'message': {'content': {'answer': 42}}}]}])
async def test_provider_envelope_failures_are_distinct_from_typed_output_validation(monkeypatch, body):
    requests = transport(monkeypatch, [httpx.Response(200, json=body)])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {})
    assert len(requests) == 1
    assert raised.value.category == 'malformed_response'


async def test_multiple_submission_output_calls_are_rejected_before_execution(monkeypatch):
    requests = transport(monkeypatch, [response(None, finish='tool_calls', calls=[
        call('SubmitAnswer', 'first', {'answer': 42}),
        call('SubmitAnswer', 'second', {'answer': 43})])])
    spec = ToolSpec('submit.answer', 'Submit answer', Output, side_effect='write',
        idempotency_key=lambda value: str(value.answer), submission=True, submission_schema=Output)
    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {}, tools=[spec],
            tool_executor=lambda *arguments: pytest.fail('ambiguous submission cannot execute'))
    assert raised.value.category == 'tool_protocol'
    assert len(requests) == 1


async def test_submission_validation_after_execution_never_retries_effect(monkeypatch):
    requests = transport(monkeypatch, [response(None, finish='tool_calls', calls=[
        call('SubmitAnswer', 'submitted', {'answer': 42})])])
    spec = ToolSpec('submit.answer', 'Submit answer', Output, side_effect='write',
        idempotency_key=lambda value: str(value.answer), submission=True, submission_schema=Output)
    executed = []

    def submit(name, arguments, call_id):
        executed.append(call_id)
        return {'saved': True}

    def validate(output):
        raise ValueError('host postcondition unavailable')

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fake', stream=False, max_attempts=3).generate(Output, {},
            tools=[spec], tool_executor=submit, validate_output=validate)
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == 1
    assert executed == ['submitted']


async def test_runtime_model_settings_reach_framework_provider(monkeypatch):
    requests = transport(monkeypatch, [response()])
    await Gateway(key='fake', stream=False).generate(Output, {},
        model_settings={'temperature': 0.2, 'top_p': 0.7,
                        'extra_body': {'vendor_flag': 'keep'}})
    assert requests[0]['temperature'] == 0.2
    assert requests[0]['top_p'] == 0.7
    assert requests[0]['vendor_flag'] == 'keep'


async def test_sent_request_cancellation_propagates_and_is_audited(monkeypatch):
    requests = transport(monkeypatch, [asyncio.CancelledError()])
    errors = []
    with pytest.raises(asyncio.CancelledError):
        await Gateway(key='fake', stream=False).generate(Output, {},
            on_error=lambda exchange, error: errors.append(error))
    assert len(requests) == len(errors) == 1
    assert errors[0]['category'] == 'cancelled'
    assert errors[0]['status'] == 'UNKNOWN_OPERATION'


async def test_restored_unknown_receipt_is_not_reexecuted_or_treated_as_success(monkeypatch):
    receipt = ToolResult(call_id='provider-call-9', name='repo.read', isError=True,
        executed=True, error={'status': 'UNKNOWN_OPERATION', 'operation_id': 'restored-operation'})
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [call()]},
               {'role': 'tool', 'name': 'RepoRead', 'tool_call_id': 'provider-call-9',
                'content': receipt.to_content()}]
    requests = transport(monkeypatch, [response(None, calls=[call()], finish='tool_calls')])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(Output, {}, messages=history,
            tools=[tool_spec()], tool_executor=lambda *args: pytest.fail('unknown call must not execute'))
    assert len(requests) == 1
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['operation_id'] == 'restored-operation'


async def test_image_history_is_restored_and_uses_configured_vision_provider(monkeypatch):
    requests = transport(monkeypatch, [response()])
    encoded = 'data:image/png;base64,iVBORw0KGgo='
    events = []
    history = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'inspect evidence'},
        {'type': 'image_url', 'image_url': {'url': encoded}}]}]
    await Gateway(key='fake', stream=False, vision_model='vision-fixture').generate(Output, {},
        messages=history, on_event=lambda kind, value: events.append((kind, value)))
    assert requests[0]['model'] == 'vision-fixture'
    assert requests[0]['messages'][1]['content'][1]['image_url']['url'] == encoded
    completed = next(value for kind, value in events if kind == 'adapter.completed')
    from tracefix.model.history import ModelProtocol
    projected = ModelProtocol.message_history(completed['message_history'])
    assert projected[0]['content'][1]['image_url']['url'] == encoded


async def test_unrelated_pipeline_cannot_authorize_an_approval_tool():
    spec = tool_spec(approval='always')
    pipeline = ToolPipeline(ToolRegistry([]), {}, 'DIAGNOSE')

    def execute(messages, info):
        pytest.fail('unbound approval tool cannot reach model execution')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model(execute)).generate(Output, {}, tools=[spec],
            tool_pipeline=pipeline, tool_executor=lambda *args: pytest.fail('cannot bypass approval'))
    assert raised.value.category == 'configuration'


async def test_framework_tool_history_reuses_completed_receipt_with_original_identity(monkeypatch):
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    history = [ModelResponse(parts=[ToolCallPart('RepoRead', {'index': 1},
                                                tool_call_id='provider-call-9')]),
        ModelRequest(parts=[ToolReturnPart('RepoRead', '{"evidence_ref":"saved"}',
                                          tool_call_id='provider-call-9')])]
    requests = transport(monkeypatch, [response(None, calls=[call()], finish='tool_calls'), response()])
    result = await Gateway(key='fake', stream=False).generate(Output, {}, message_history=history,
        tools=[tool_spec()], tool_executor=lambda *args: pytest.fail('completed call cannot execute'))
    assert result.value.answer == 42
    assert len(requests) == 2
    assert requests[-1]['messages'][-1]['content'] == '{"evidence_ref":"saved"}'


async def test_resumed_tool_rounds_count_toward_existing_limit(monkeypatch):
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [call()]},
        {'role': 'tool', 'name': 'RepoRead', 'tool_call_id': 'provider-call-9',
         'content': '{"evidence_ref":"saved"}'}]
    requests = transport(monkeypatch, [response(None, calls=[
        call(call_id='new-call')], finish='tool_calls')])
    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='fake', stream=False, max_tool_rounds=1).generate(Output, {},
            messages=history, tools=[tool_spec()],
            tool_executor=lambda *args: pytest.fail('tool round limit cannot reset on resume'))
    assert len(requests) == 1
    assert raised.value.details['tool_round'] == 1
    assert raised.value.category == 'tool_protocol'
