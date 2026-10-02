import copy
import json

import httpx
import pytest

from tracefix.model.gateway import BrowserPolicyRouter, Gateway, ModelError, ModelResult
from tracefix.runtime.contracts import BrowserAction, Decision


class EventBytes(httpx.AsyncByteStream):
    def __init__(self, fragments, before_read=None):
        self.fragments = fragments
        self.before_read = before_read
        self.closed = False

    async def __aiter__(self):
        for index, fragment in enumerate(self.fragments):
            if self.before_read:
                self.before_read(index)
            if isinstance(fragment, Exception):
                raise fragment
            yield fragment

    async def aclose(self):
        self.closed = True


def event(delta=None, *, finish=None, usage=None):
    payload = {'model': 'stream-revision', 'choices': []}
    if delta is not None or finish is not None:
        payload['choices'] = [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]
    if usage is not None:
        payload['usage'] = usage
    return ('data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n').encode()


def stream_response(fragments, before_read=None):
    stream = EventBytes(fragments, before_read)
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)


def final_stream(*, content='{"kind":"finish"}', usage=None):
    return stream_response([
        event({'content': content}), event(finish='stop'),
        event(usage=usage or {'total_tokens': 3}), b'data: [DONE]\n\n',
    ])


def use_transport(monkeypatch, responses):
    requests = []
    client_type = httpx.AsyncClient

    async def handle(request):
        requests.append(json.loads(request.content))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr('tracefix.model.gateway.httpx.AsyncClient', client)
    return requests


@pytest.fixture(autouse=True)
def gateway_defaults(monkeypatch):
    for name in ('TRACEFIX_STREAM', 'TRACEFIX_THINKING', 'TRACEFIX_MODEL_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)


async def test_default_thinking_streams_before_completion_and_preserves_usage(monkeypatch):
    deltas = []
    usages = []
    audits = []
    usage = {'prompt_tokens': 150, 'completion_tokens': 10, 'total_tokens': 160,
             'prompt_cache_hit_tokens': 120, 'prompt_cache_miss_tokens': 30,
             'prompt_tokens_details': {'cached_tokens': 120}}
    frames = [
        b': keepalive\n\n', event({'role': 'assistant', 'reasoning_content': '分析'}),
        event({'reasoning_content': '页面'}), event({'content': '{"kind":'}),
        event({'content': '"finish"}'}), event(finish='stop'), event(usage=usage),
        b'data: [DONE]\n\n',
    ]

    def before_read(index):
        if index == 2:
            assert deltas == [({'attempt': 1}, {'channel': 'reasoning', 'delta': '分析'})]
        if index == 5:
            assert len(deltas) == 4
            assert not usages

    response = stream_response(frames, before_read)
    requests = use_transport(monkeypatch, [response])
    result = await Gateway(key='ci').generate(BrowserAction, {},
        on_attempt=lambda model, request, attempt: {'attempt': attempt},
        on_delta=lambda exchange, delta: deltas.append((exchange, delta)),
        on_usage=usages.append, on_response=lambda exchange, raw: audits.append(raw))

    assert requests[0]['stream'] is True
    assert requests[0]['stream_options'] == {'include_usage': True}
    assert requests[0]['thinking'] == {'type': 'enabled'}
    assert result.value.kind == 'finish'
    assert result.model_revision == 'stream-revision'
    assert result.usage == usage
    assert usages == [usage]
    assert audits[0]['body']['choices'][0]['message']['reasoning_content'] == '分析页面'
    assert response.stream.closed


async def test_streamed_tool_arguments_execute_only_after_complete_usage_and_done(monkeypatch):
    executed = []
    usages = []
    fragments = [
        event({'reasoning_content': 'inspect page'}),
        event({'tool_calls': [{'index': 0, 'id': 'navigate-1', 'type': 'function',
                              'function': {'name': 'BrowserNavi', 'arguments': '{"value":'}}]}),
        event({'tool_calls': [{'index': 0, 'function': {
            'name': 'gate', 'arguments': '"https://example.test"}'}}]}),
        event(finish='tool_calls'), event(usage={'total_tokens': 7}), b'data: [DONE]\n\n',
    ]

    def before_read(index):
        assert executed == []

    response = stream_response(fragments, before_read)
    requests = use_transport(monkeypatch, [response, final_stream()])

    async def execute(name, arguments, call_id):
        assert response.stream.closed
        assert usages == [{'total_tokens': 7}]
        executed.append((name, arguments, call_id))
        return {'observation': {'id': 'fresh'}}

    result = await Gateway(key='ci').generate(BrowserAction, {},
        tool_executor=execute, on_usage=usages.append)

    assert result.value.kind == 'finish'
    assert executed == [('browser_navigate', {'value': 'https://example.test'}, 'navigate-1')]
    assistant, tool = requests[1]['messages'][-2:]
    assert assistant['reasoning_content'] == 'inspect page'
    assert assistant['tool_calls'][0]['function']['arguments'] == '{"value":"https://example.test"}'
    assert tool['tool_call_id'] == 'navigate-1'
    assert usages == [{'total_tokens': 7}, {'total_tokens': 3}]


@pytest.mark.parametrize('ending', [
    [], [b'data: {broken}\n\n'], [httpx.ReadError('connection lost')],
    [httpx.ReadTimeout('thinking timeout')], [b'data: [DONE]\n\n'],
])
async def test_incomplete_stream_is_audited_without_retry_or_tool_execution(monkeypatch, ending):
    fragments = [event({'reasoning_content': 'partial analysis'}),
        event({'tool_calls': [{'index': 0, 'id': 'snapshot-1', 'type': 'function',
                              'function': {'name': 'BrowserSnapshot', 'arguments': '{}'}}]})]
    requests = use_transport(monkeypatch, [stream_response(fragments + ending)])
    errors, audits, deltas = [], [], []

    async def execute(*args):
        pytest.fail('an incomplete stream cannot execute a tool')

    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_attempts=3).generate(BrowserAction, {},
            tool_executor=execute, on_delta=lambda exchange, delta: deltas.append(delta),
            on_response=lambda exchange, raw: audits.append(raw),
            on_error=lambda exchange, error: errors.append(error))

    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == 1
    assert errors[0]['will_retry'] is False
    assert errors[0]['requires_manual_review'] is True
    assert errors[0]['billing_status'] == 'unknown'
    assert errors[0]['request_status'] == 'response_received'
    assert audits[0]['stream_incomplete'] is True
    assert audits[0]['body']['choices'][0]['message']['reasoning_content'] == 'partial analysis'
    assert deltas == [{'channel': 'reasoning', 'delta': 'partial analysis'}]


async def test_received_usage_survives_failure_after_tail_block(monkeypatch):
    usage = {'total_tokens': 11, 'prompt_cache_hit_tokens': 8, 'prompt_cache_miss_tokens': 2}
    use_transport(monkeypatch, [stream_response([
        event({'content': '{"kind":"finish"}'}), event(finish='stop'), event(usage=usage),
        httpx.ReadError('missing done'),
    ])])
    usages, errors = [], []

    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {}, on_usage=usages.append,
            on_error=lambda exchange, error: errors.append(error))

    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert usages == [usage]
    assert errors[0]['billing_status'] == 'known'


async def test_stream_http_error_and_connect_failure_keep_attempt_boundaries(monkeypatch):
    requests = use_transport(monkeypatch, [
        httpx.ConnectError('not connected'),
        httpx.Response(503, headers={'Retry-After': '0'}, json={'error': 'busy'}),
        final_stream(),
    ])
    attempts, deltas, errors = [], [], []

    def attempt(model, request, number):
        attempts.append(copy.deepcopy(request))
        return {'attempt': number}

    await Gateway(key='ci', max_retry_delay=0).generate(BrowserAction, {}, on_attempt=attempt,
        on_delta=lambda exchange, delta: deltas.append((exchange, delta)),
        on_error=lambda exchange, error: errors.append(error))

    assert len(requests) == 3
    assert [request['attempt'] for request in attempts] == [1, 2, 3]
    assert len({request['logical_exchange_id'] for request in attempts}) == 1
    assert deltas[0][0] == {'attempt': 3}
    assert [error['category'] for error in errors] == ['network', 'service_unavailable']


async def test_stream_content_validation_retry_keeps_previous_reasoning(monkeypatch):
    first = stream_response([
        event({'reasoning_content': 'first attempt'}), event({'content': '{"kind":"observe"}'}),
        event(finish='stop'), event(usage={'total_tokens': 4}), b'data: [DONE]\n\n',
    ])
    requests = use_transport(monkeypatch, [first, final_stream()])
    await Gateway(key='ci', max_retry_delay=0).generate(BrowserAction, {})

    assert requests[1]['messages'][-2]['reasoning_content'] == 'first attempt'
    assert requests[1]['messages'][-2]['content'] == '{"kind":"observe"}'
    assert '已有工具结果仍然有效' in requests[1]['messages'][-1]['content']


async def test_sse_handles_fragmented_utf8_and_multiline_data(monkeypatch):
    frame = event({'reasoning_content': '页面'}).replace(b', "choices"', b',\ndata: "choices"')
    fragments = [frame[index:index + 1] for index in range(len(frame))]
    fragments += [event({'content': '{"kind":"finish"}'}), event(finish='stop'),
                  event(usage={'total_tokens': 3}), b'data: [DONE]']
    use_transport(monkeypatch, [stream_response(fragments)])
    deltas = []
    await Gateway(key='ci').generate(BrowserAction, {}, on_delta=lambda exchange, delta: deltas.append(delta))
    assert deltas[0] == {'channel': 'reasoning', 'delta': '页面'}


async def test_buffered_compatibility_mode_omits_stream_options(monkeypatch):
    response = httpx.Response(200, json={'usage': {'total_tokens': 1}, 'choices': [
        {'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]})
    requests = use_transport(monkeypatch, [response])
    await Gateway(key='ci', stream=False, thinking='disabled').generate(BrowserAction, {})
    assert requests[0]['stream'] is False
    assert requests[0]['thinking'] == {'type': 'disabled'}
    assert 'stream_options' not in requests[0]


def test_gateway_settings_support_environment_and_explicit_compatible_thinking(monkeypatch):
    monkeypatch.setenv('TRACEFIX_STREAM', 'false')
    monkeypatch.setenv('TRACEFIX_THINKING', 'disabled')
    monkeypatch.setenv('TRACEFIX_MODEL_TIMEOUT', '240')
    configured = Gateway(key='ci')
    assert configured.stream is False
    assert configured.thinking == 'disabled'
    assert configured.timeout == 240
    explicit = Gateway(key='ci', base_url='https://compatible.test', stream=True,
                       thinking='enabled', timeout=120)
    assert explicit.stream is True
    assert explicit.thinking == 'enabled'
    assert explicit.timeout == 120
    monkeypatch.delenv('TRACEFIX_THINKING')
    assert Gateway(key='ci', base_url='https://compatible.test').thinking is None


@pytest.mark.parametrize('settings', [
    {'stream': 'invalid'}, {'stream': 1}, {'thinking': True}, {'thinking': 'auto'},
    {'timeout': 0}, {'timeout': float('nan')}, {'timeout': 'bad'},
])
def test_invalid_gateway_stream_settings_fail_early(settings):
    with pytest.raises(ValueError):
        Gateway(key='ci', **settings)


async def test_router_streaming_capability_keeps_nonstreaming_models_compatible():
    received = []

    class Teacher:
        async def generate(self, schema, context):
            return ModelResult(BrowserAction(kind='finish'), {'total_tokens': 1}, 'teacher', 'stop')

    class Student:
        supports_streaming = True
        tool_mode = 'json'

        async def generate(self, schema, context, on_delta=None):
            on_delta({'attempt': 1}, {'channel': 'reasoning', 'delta': 'inspect'})
            return ModelResult(BrowserAction(kind='finish'), {'total_tokens': 1}, 'student', 'stop')

    router = BrowserPolicyRouter(Teacher(), Student())
    assert router.supports_streaming is True
    await router.generate(Decision, {}, on_delta=lambda exchange, delta: received.append(delta))
    await router.generate(BrowserAction, {}, on_delta=lambda exchange, delta: received.append(delta))
    assert received == [{'channel': 'reasoning', 'delta': 'inspect'}]
