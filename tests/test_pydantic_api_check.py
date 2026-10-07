import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from tracefix.model import protocol
from tracefix.model.gateway import Gateway, ModelError


def api_check():
    path = Path(__file__).resolve().parents[1] / 'tools/checks/check_api.py'
    spec = importlib.util.spec_from_file_location('pydantic_api_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completion(content=None, calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls:
        message['tool_calls'] = calls
    return httpx.Response(200, json={'id': 'fixture', 'created': 0,
        'object': 'chat.completion', 'model': 'diagnostic-revision',
        'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
        'choices': [{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
                     'message': message}]})


def call(name='BrowserSnapshot', arguments='{}'):
    return {'id': 'diagnostic-original-id', 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def transport(monkeypatch, responses):
    requests = []

    async def handle(request):
        requests.append({'url': str(request.url), 'json': json.loads(request.content)})
        return responses.pop(0)

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    return requests


async def test_diagnostic_uses_real_agent_typed_output_and_synthetic_host_port(monkeypatch):
    requests = transport(monkeypatch, [completion('{"ok":true,"sum":4}'),
        completion(calls=[call()]), completion('{"kind":"finish"}')])
    results = await api_check().run_checks(Gateway(key='fixture', stream=False, vision_model=''))
    assert len(requests) == 3
    assert results[0]['output'] == {'ok': True, 'sum': 4}
    assert results[1]['backend'] == 'pydantic_ai'
    assert results[1]['request_count'] == 2
    assert [usage['total_tokens'] for usage in results[1]['usage']] == [7, 7]
    assert results[1]['browser_executed'] is False
    assistant, tool = requests[-1]['json']['messages'][-2:]
    assert assistant['tool_calls'][0]['id'] == tool['tool_call_id'] == 'diagnostic-original-id'
    assert json.loads(tool['content'])['browser_executed'] is False
    assert results[-1]['status'] == 'skipped'


async def test_diagnostic_sends_image_to_configured_vision_model(monkeypatch):
    requests = transport(monkeypatch, [completion('{"ok":true,"sum":4}'),
        completion(calls=[call()]), completion('{"kind":"finish"}'),
        completion('{"color":"red"}')])
    results = await api_check().run_checks(Gateway(key='fixture', stream=False,
        text_model='text-fixture', vision_model='vision-fixture'))
    assert [request['json']['model'] for request in requests] == [
        'text-fixture', 'text-fixture', 'text-fixture', 'vision-fixture']
    image = requests[-1]['json']['messages'][1]['content'][1]
    assert image['image_url']['url'].startswith('data:image/png;base64,')
    assert results[-1]['output'] == {'color': 'red'}


async def test_diagnostic_requires_the_original_native_tool_exchange(monkeypatch):
    requests = transport(monkeypatch, [completion('{"kind":"finish"}')])
    with pytest.raises(RuntimeError, match='未完成 native 工具协议诊断'):
        await api_check().check_native_protocol(Gateway(key='fixture', stream=False))
    assert len(requests) == 1


async def test_diagnostic_rejects_effectful_browser_action_without_retry(monkeypatch):
    requests = transport(monkeypatch, [completion(calls=[
        call('BrowserNavigate', '{"value":"https://example.test"}')])])
    with pytest.raises(ModelError) as raised:
        await api_check().check_native_protocol(Gateway(key='fixture', stream=False))
    assert len(requests) == 1
    assert raised.value.category == 'tool_execution'
    assert raised.value.status == 'UNKNOWN_OPERATION'
