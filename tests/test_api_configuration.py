import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from tracefix.model.chat import stream_chat
from tracefix.model.gateway import Gateway


def load_api_check():
    path = Path(__file__).resolve().parents[1] / 'tools/checks/check_api.py'
    spec = importlib.util.spec_from_file_location('tracefix_api_check_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def provider_response(content=None, tool_calls=None):
    message = {'role': 'assistant', 'content': content}
    if tool_calls is not None:
        message['tool_calls'] = tool_calls
    return httpx.Response(200, json={
        'model': 'diagnostic-model', 'usage': {'total_tokens': 7},
        'choices': [{'finish_reason': 'tool_calls' if tool_calls else 'stop', 'message': message}],
    })


def snapshot_call():
    return {'id': 'call-diagnostic', 'type': 'function',
            'function': {'name': 'BrowserSnapshot', 'arguments': '{}'}}


def mock_responses(monkeypatch, responses):
    requests = []

    async def post(client, url, **kwargs):
        requests.append({'url': url, **copy.deepcopy(kwargs)})
        return responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    return requests


def test_default_configuration_matches_deepseek_example(monkeypatch):
    for name in ('TRACEFIX_BASE_URL', 'TRACEFIX_TEXT_MODEL',
                 'TRACEFIX_VISION_MODEL', 'TRACEFIX_TOOL_MODE', 'TRACEFIX_STREAM'):
        monkeypatch.delenv(name, raising=False)
    example = dotenv_values(Path(__file__).resolve().parents[1] / '.env.example')
    gateway = Gateway(key='ci')
    assert gateway.base_url == example['TRACEFIX_BASE_URL'] == 'https://api.deepseek.com'
    assert gateway.text_model == example['TRACEFIX_TEXT_MODEL'] == 'deepseek-chat'
    assert gateway.tool_mode == example['TRACEFIX_TOOL_MODE'] == 'native'
    assert gateway.stream is True
    assert gateway.vision_model == example['TRACEFIX_VISION_MODEL'] == ''


@pytest.mark.usefixtures('json_completion_transport')
async def test_native_diagnostic_pairs_tool_messages_without_images(monkeypatch):
    module = load_api_check()
    call = snapshot_call()
    responses = [provider_response('{"ok":true,"sum":4}'),
                 provider_response(tool_calls=[call]), provider_response('{"kind":"finish"}')]
    requests = mock_responses(monkeypatch, responses)
    gateway = Gateway(key='ci', text_model='deepseek-chat', vision_model='',
                      tool_mode='native', max_attempts=1)
    results = await module.run_checks(gateway)
    assert not responses
    assert len(requests) == 3
    assert all(request['url'].endswith('/chat/completions') for request in requests)
    assert all(isinstance(request['json']['messages'][1]['content'], str) for request in requests)
    assert all('response_format' not in request['json'] for request in requests[1:])
    assert any(tool['function']['name'] == 'BrowserSnapshot'
               for tool in requests[1]['json']['tools'])
    assistant, tool = requests[2]['json']['messages'][-2:]
    assert assistant['role'] == 'assistant' and assistant['tool_calls'] == [call]
    assert tool['role'] == 'tool' and tool['tool_call_id'] == call['id']
    assert json.loads(tool['content'])['browser_executed'] is False
    assert results[1]['kind'] == 'native_protocol'
    assert results[1]['usage'] == [{'total_tokens': 7}, {'total_tokens': 7}]
    assert results[-1]['status'] == 'skipped'


@pytest.mark.usefixtures('json_completion_transport')
async def test_json_diagnostic_skips_tools_and_unconfigured_vision(monkeypatch):
    module = load_api_check()
    requests = mock_responses(monkeypatch, [provider_response('{"ok":true,"sum":4}')])
    gateway = Gateway(key='ci', vision_model='', tool_mode='json', max_attempts=1)
    results = await module.run_checks(gateway)
    assert len(requests) == 1
    assert requests[0]['json']['response_format'] == {'type': 'json_object'}
    assert 'tools' not in requests[0]['json']
    assert [result['kind'] for result in results] == ['text', 'vision']
    assert results[-1]['status'] == 'skipped'


@pytest.mark.usefixtures('json_completion_transport')
async def test_explicit_vision_configuration_sends_image_to_vision_model(monkeypatch):
    module = load_api_check()
    requests = mock_responses(monkeypatch, [provider_response('{"ok":true,"sum":4}'),
                                            provider_response('{"color":"red"}')])
    gateway = Gateway(key='ci', text_model='text-test', vision_model='vision-test',
                      tool_mode='json', max_attempts=1)
    results = await module.run_checks(gateway)
    assert [request['json']['model'] for request in requests] == ['text-test', 'vision-test']
    image = requests[1]['json']['messages'][1]['content'][1]
    assert image['type'] == 'image_url'
    assert image['image_url']['url'].startswith('data:image/png;base64,iVBORw0KGgo')
    assert results[-1]['output'] == {'color': 'red'}


@pytest.mark.usefixtures('json_completion_transport')
async def test_native_diagnostic_fails_when_provider_skips_tool_call(monkeypatch):
    module = load_api_check()
    requests = mock_responses(monkeypatch, [provider_response('{"kind":"finish"}')])
    gateway = Gateway(key='ci', vision_model='', tool_mode='native', max_attempts=1)
    with pytest.raises(RuntimeError, match='未完成 native 工具协议诊断'):
        await module.check_native_protocol(gateway)
    assert len(requests) == 1


@pytest.mark.usefixtures('json_completion_transport')
async def test_native_diagnostic_refuses_other_browser_actions(monkeypatch):
    module = load_api_check()
    call = snapshot_call()
    call['function'] = {'name': 'BrowserNavigate', 'arguments': '{"value":"https://example.com"}'}
    requests = mock_responses(monkeypatch, [provider_response(tool_calls=[call])])
    gateway = Gateway(key='ci', vision_model='', tool_mode='native', max_attempts=1)
    with pytest.raises(RuntimeError, match='工具诊断只接受一次 BrowserSnapshot'):
        await module.check_native_protocol(gateway)
    assert len(requests) == 1


async def test_stream_chat_uses_same_default_text_model(monkeypatch):
    monkeypatch.setenv('TRACEFIX_API_KEY', 'ci')
    monkeypatch.delenv('TRACEFIX_TEXT_MODEL', raising=False)
    monkeypatch.delenv('TRACEFIX_BASE_URL', raising=False)
    requests = []

    class StreamResponse:
        is_success = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield 'data: [DONE]'

    def stream(client, method, url, **kwargs):
        requests.append({'method': method, 'url': url, **kwargs})
        return StreamResponse()

    monkeypatch.setattr(httpx.AsyncClient, 'stream', stream)
    events = [event async for event in stream_chat('hello', [], 'project', None, use_knowledge=False)]
    assert events == [{'sources': []}]
    assert requests[0]['json']['model'] == 'deepseek-chat'
    assert requests[0]['url'] == 'https://api.deepseek.com/chat/completions'
