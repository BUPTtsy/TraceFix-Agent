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
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolSpec


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
        'name': name, 'arguments': json.dumps(arguments or {'index': 1})}}


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
    result = await Gateway(key='fake', stream=False, tool_mode='json').generate(Output, {},
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
