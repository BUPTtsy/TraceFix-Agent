import copy
import gzip
import json
import zlib

import httpx
import pytest

from tracefix.model.gateway import Gateway, ModelError
from tracefix.runtime.contracts import BrowserAction
from tracefix.model.chat import stream_chat
from tracefix.model import protocol


def compressed(payload, encoding):
    if encoding == 'gzip':
        return gzip.compress(payload)
    if encoding == 'deflate':
        return zlib.compress(payload)
    if encoding == 'zstd':
        import zstandard
        return zstandard.ZstdCompressor().compress(payload)
    return payload


@pytest.mark.parametrize('encoding', ['identity', 'gzip', 'deflate', 'zstd'])
async def test_compressed_sse_audits_decoded_chunks_and_preserves_provider_response(monkeypatch, encoding):
    payload = b''.join([event({'reasoning_content': '分析压缩响应'}), event({'content': '完整中文正文'}),
        event(finish='stop'), event(usage={'total_tokens': 9}), b'data: [DONE]\n\n'])
    wire = compressed(payload, encoding)
    source = EventBytes([wire[index:index + 3] for index in range(0, len(wire), 3)])
    response = httpx.Response(200, headers={'content-type': 'text/event-stream', 'content-encoding': encoding}, stream=source)
    requests = use_transport(monkeypatch, [response])
    audits, deltas, usages = [], [], []
    result = await Gateway(key='ci', stream=True).generate(str, {'message': '测试压缩'},
        on_response=lambda exchange, value: audits.append(value),
        on_delta=lambda exchange, delta: deltas.append(delta), on_usage=usages.append)
    assert result.value == '完整中文正文'
    assert len(requests) == len(audits) == 1
    assert audits[0]['body']['choices'][0]['message']['reasoning_content'] == '分析压缩响应'
    assert audits[0]['billing_status'] == 'known'
    assert audits[0]['stream_incomplete'] is False
    assert usages == [{'total_tokens': 9}]
    assert ''.join(delta['delta'] for delta in deltas if delta['channel'] == 'content') == '完整中文正文'
    assert source.closed


@pytest.mark.parametrize('encoding', ['gzip', 'deflate', 'zstd'])
async def test_compressed_json_uses_httpx_decoding_before_audit(monkeypatch, encoding):
    payload = {'id': 'compressed-json', 'object': 'chat.completion', 'created': 1, 'model': 'revision', 'choices': [
        {'index': 0, 'message': {'role': 'assistant', 'content': 'JSON 中文'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 2, 'total_tokens': 4}}
    wire = compressed(json.dumps(payload, ensure_ascii=False).encode(), encoding)
    source = EventBytes([wire[index:index + 2] for index in range(0, len(wire), 2)])
    response = httpx.Response(200, headers={'content-type': 'application/json', 'content-encoding': encoding}, stream=source)
    use_transport(monkeypatch, [response])
    audits = []
    result = await Gateway(key='ci', stream=False).generate(str, {'message': '测试 JSON'},
        on_response=lambda exchange, value: audits.append(value))
    assert result.value == 'JSON 中文'
    assert audits[0]['body'] == payload
    assert audits[0]['billing_status'] == 'known'
    assert source.closed


@pytest.mark.parametrize('with_usage', [False, True])
async def test_truncated_zstd_stream_preserves_audit_and_billing_unknown_semantics(monkeypatch, with_usage):
    frames = [event({'content': '压缩部分正文'}), event(finish='stop')]
    if with_usage:
        frames.append(event(usage={'total_tokens': 13}))
    wire = compressed(b''.join(frames), 'zstd') + compressed(b'data: [DONE]\n\n', 'zstd')[:-1]
    source = EventBytes([wire[index:index + 7] for index in range(0, len(wire), 7)])
    response = httpx.Response(200, headers={'content-type': 'text/event-stream', 'content-encoding': 'zstd'}, stream=source)
    requests = use_transport(monkeypatch, [response])
    audits, errors = [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', stream=True).generate(str, {'message': '测试截断'},
            on_response=lambda exchange, value: audits.append(value),
            on_error=lambda exchange, value: errors.append(value))
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == len(audits) == len(errors) == 1
    assert audits[0]['stream_incomplete'] is True
    assert errors[0]['will_retry'] is False
    assert errors[0]['billing_status'] == ('known' if with_usage else 'unknown')
    assert audits[0]['body']['choices'][0]['message']['content'] == '压缩部分正文'
    assert source.closed


async def test_audited_close_drains_compressed_tail_without_changing_raw_chunks():
    from types import SimpleNamespace

    audits, deltas = [], []

    async def callback(name, exchange, value):
        deltas.append(value)

    async def finish_response(response, body):
        audits.append(body)

    payload = b''.join([event({'content': '关闭时仍审计完整响应'}), event(finish='stop'),
        event(usage={'total_tokens': 8}), b'data: [DONE]\n\n'])
    wire = compressed(payload, 'zstd')
    source = EventBytes([wire[index:index + 3] for index in range(0, len(wire), 3)])
    response = httpx.Response(200, headers={'content-encoding': 'zstd'})
    boundary = SimpleNamespace(exchange='attempt', callback=callback, finish_response=finish_response)
    audited = protocol.AuditedStream(source, response, boundary)
    iterator = audited.__aiter__()
    assert await anext(iterator) == wire[:3]
    await audited.aclose()
    assert audits[0]['choices'][0]['message']['content'] == '关闭时仍审计完整响应'
    assert audits[0]['usage'] == {'total_tokens': 8}
    assert source.closed
    assert audited.finished


async def test_unsupported_encoding_is_audited_as_unknown_without_utf8_fallback(monkeypatch):
    payload = b''.join([event({'content': '不可接受未声明的解码'}), event(finish='stop'),
        b'data: [DONE]\n\n'])
    source = EventBytes([payload])
    response = httpx.Response(200, headers={'content-type': 'text/event-stream', 'content-encoding': 'unsupported'}, stream=source)
    requests = use_transport(monkeypatch, [response])
    audits, errors = [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', stream=True).generate(str, {'message': '未知压缩'},
            on_response=lambda exchange, value: audits.append(value),
            on_error=lambda exchange, value: errors.append(value))
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == len(audits) == len(errors) == 1
    assert audits[0]['stream_incomplete'] is True
    assert errors[0]['billing_status'] == 'unknown'
    assert errors[0]['will_retry'] is False
    assert source.closed


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

    def client(boundary):
        return client_type(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    return requests


@pytest.fixture(autouse=True)
def gateway_defaults(monkeypatch):
    for name in ('TRACEFIX_STREAM', 'TRACEFIX_THINKING', 'TRACEFIX_MODEL_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('fragments', [
    [event({'content': 'partial'})],
    [event(finish='stop')],
    [b'data: [DONE]\n\n'],
    [event(finish='length'), b'data: [DONE]\n\n'],
    [b'data: invalid\n\n', event(finish='stop'), b'data: [DONE]\n\n'],
    [b'data: {"error":{"message":"failed"}}\n\n'],
])
async def test_chat_rejects_incomplete_or_invalid_response(monkeypatch, fragments):
    use_transport(monkeypatch, [stream_response(fragments)])
    with pytest.raises(ModelError):
        await Gateway(key='fake', stream=True).generate(str, {'message': 'hello'})


async def test_chat_requires_successful_finish_and_done(monkeypatch):
    use_transport(monkeypatch, [final_stream(content='complete')])
    monkeypatch.setenv('TRACEFIX_API_KEY', 'fake')
    events = [item async for item in stream_chat('hello', [], 'scope', None, False)]
    assert {'delta': 'complete'} in events
    assert events[-1]['turn_messages'][-1]['content'] == 'complete'


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
    assert executed == [('browser.navigate', {'value': 'https://example.test'}, 'navigate-1')]
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


async def test_cancelled_stream_preserves_partial_audit_and_does_not_drain_tail(monkeypatch):
    import asyncio

    read_indices, errors, audits = [], [], []

    def cancel_read(index):
        read_indices.append(index)
        if index == 1:
            raise asyncio.CancelledError()

    response = stream_response([event({'content': 'partial'}),
        event(finish='stop'), b'data: [DONE]\n\n'], before_read=cancel_read)
    requests = use_transport(monkeypatch, [response])
    with pytest.raises(asyncio.CancelledError):
        await Gateway(key='ci', stream=True).generate(str, {'message': 'hello'},
            on_response=lambda exchange, value: audits.append(value),
            on_error=lambda exchange, value: errors.append(value))
    assert len(requests) == len(audits) == len(errors) == 1
    assert read_indices == [0, 1]
    assert audits[0]['stream_incomplete'] is True
    assert audits[0]['body']['choices'][0]['message']['content'] == 'partial'
    assert errors[0]['category'] == 'cancelled'
    assert errors[0]['status'] == 'UNKNOWN_OPERATION'
    assert response.stream.closed


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

    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_retry_delay=0).generate(BrowserAction, {}, on_attempt=attempt,
            on_delta=lambda exchange, delta: deltas.append((exchange, delta)),
            on_error=lambda exchange, error: errors.append(error))

    assert len(requests) == 1
    assert [request['attempt'] for request in attempts] == [1]
    assert len({request['logical_exchange_id'] for request in attempts}) == 1
    assert deltas == []
    assert raised.value.status == 'WAITING_NETWORK'
    assert [error['category'] for error in errors] == ['network']


async def test_stream_content_validation_retry_keeps_previous_reasoning(monkeypatch):
    first = stream_response([
        event({'reasoning_content': 'first attempt'}), event({'content': '{"kind":"invalid"}'}),
        event(finish='stop'), event(usage={'total_tokens': 4}), b'data: [DONE]\n\n',
    ])
    requests = use_transport(monkeypatch, [first, final_stream()])
    await Gateway(key='ci', max_retry_delay=0).generate(BrowserAction, {})

    assert requests[1]['messages'][-2]['reasoning_content'] == 'first attempt'
    assert requests[1]['messages'][-2]['content'] == '{"kind":"invalid"}'
    assert requests[1]['messages'][-1]['role'] == 'user'
    assert requests[1]['messages'][-1]['content']


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
    response = httpx.Response(200, json={'id': 'fixture', 'created': 0, 'object': 'chat.completion',
        'model': 'fixture', 'usage': {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1},
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': '{"kind":"finish"}'}}]})
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
