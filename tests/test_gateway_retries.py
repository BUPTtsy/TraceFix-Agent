import json

import httpx
import pytest

from tracefix.model import protocol
from tracefix.model.gateway import Gateway, ModelError
from tracefix.runtime.contracts import BrowserAction


def response(*, status=200, usage=1, finish='stop', content='{"kind":"finish"}'):
    return httpx.Response(status, json={'id': 'fixture', 'created': 0,
        'object': 'chat.completion', 'model': 'fake-model',
        'choices': [{'index': 0, 'finish_reason': finish,
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': usage, 'completion_tokens': 0, 'total_tokens': usage}})


def transport(monkeypatch, responses):
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    return requests


async def test_http_failure_is_audited_without_automatic_retry(monkeypatch):
    requests = transport(monkeypatch, [response(status=503, usage=2)])
    errors, usages = [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False, max_attempts=3).generate(BrowserAction, {},
            on_usage=usages.append, on_error=lambda exchange, error: errors.append(error))
    assert len(requests) == len(errors) == 1
    assert usages[0]['total_tokens'] == 2
    assert errors[0]['will_retry'] is False
    assert raised.value.category == 'service_unavailable'


async def test_each_output_correction_request_is_audited_and_usage_is_aggregated(monkeypatch):
    requests = transport(monkeypatch, [response(content='{"kind":"invalid"}', usage=4),
                                      response(usage=7)])
    usages, attempts = [], []
    result = await Gateway(key='fake', stream=False, max_attempts=2).generate(BrowserAction, {},
        on_usage=usages.append, on_attempt=lambda name, request, number: attempts.append(request))
    assert result.value.kind == 'finish'
    assert [usage['total_tokens'] for usage in usages] == [4, 7]
    assert result.usage['total_tokens'] == 11
    assert len(requests) == len(attempts) == 2


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'refusal'])
async def test_incomplete_finish_is_terminal(monkeypatch, finish):
    requests = transport(monkeypatch, [response(finish=finish)])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False, max_attempts=3).generate(BrowserAction, {})
    assert len(requests) == 1
    assert raised.value.category == 'incomplete_output'
    assert raised.value.details['will_retry'] is False


@pytest.mark.parametrize('error', [httpx.ConnectError('offline'), httpx.ConnectTimeout('connect'),
                                 httpx.PoolTimeout('pool')])
async def test_safe_transport_failure_is_waiting_network_without_retry(monkeypatch, error):
    requests = transport(monkeypatch, [error])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False, max_attempts=2).generate(BrowserAction, {})
    assert len(requests) == 1
    assert raised.value.status == 'WAITING_NETWORK'
    assert raised.value.details['request_status'] == 'not_sent'


async def test_read_timeout_is_unknown_without_retry(monkeypatch):
    requests = transport(monkeypatch, [httpx.ReadTimeout('read')])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='fake', stream=False).generate(BrowserAction, {})
    assert len(requests) == 1
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['will_retry'] is False


@pytest.mark.parametrize('value', [0, -1, 'x', 1.5, True])
def test_max_attempts_validation(value):
    with pytest.raises(ValueError):
        Gateway(key='fake', max_attempts=value)


def test_max_attempts_reads_environment(monkeypatch):
    monkeypatch.setenv('TRACEFIX_MODEL_MAX_ATTEMPTS', '4')
    assert Gateway(key='fake').max_attempts == 4
