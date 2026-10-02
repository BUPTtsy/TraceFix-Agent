import httpx
import pytest

from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import BrowserAction


pytestmark = pytest.mark.usefixtures('json_completion_transport')


def response(*, status=200, usage=1, finish='stop', content='{"kind":"finish"}', headers=None):
    body = {'choices': [{'finish_reason': finish, 'message': {'content': content}}]}
    if usage is not None:
        body['usage'] = {'total_tokens': usage}
    return httpx.Response(status, headers=headers, json=body)


async def test_http_retry_exhaustion_has_no_final_sleep(monkeypatch):
    errors, sleeps, calls = [], [], []

    async def post(*args, **kwargs):
        calls.append(1)
        return response(status=503, usage=2)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_attempts=2).generate(
            BrowserAction, {}, on_error=lambda exchange, error: errors.append(error))
    assert len(calls) == 2
    assert sleeps == [1]
    assert errors[-1]['will_retry'] is False
    assert raised.value.category == 'service_unavailable'


async def test_each_known_usage_attempt_is_reported(monkeypatch):
    usages, sleeps = [], []
    responses = [response(finish='length', usage=4), response(usage=7)]

    async def post(*args, **kwargs):
        return responses.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci', max_attempts=2).generate(
        BrowserAction, {}, on_usage=usages.append)
    assert result.value.kind == 'finish'
    assert usages == [{'total_tokens': 4}, {'total_tokens': 7}]
    assert sleeps == [1]


@pytest.mark.parametrize('finish', ['content_filter', 'refusal'])
async def test_non_length_finish_reason_is_terminal(monkeypatch, finish):
    errors, calls, sleeps = [], [], []

    async def post(*args, **kwargs):
        calls.append(1)
        return response(finish=finish, usage=3)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    with pytest.raises(ModelError):
        await Gateway(key='ci', max_attempts=3).generate(
            BrowserAction, {}, on_error=lambda exchange, error: errors.append(error))
    assert len(calls) == 1
    assert sleeps == []
    assert errors[0]['will_retry'] is False


@pytest.mark.parametrize('exception', [httpx.ConnectError('offline'), httpx.ConnectTimeout('connect'), httpx.PoolTimeout('pool')])
async def test_safe_transport_failures_are_bounded_waiting_network(monkeypatch, exception):
    calls, errors, sleeps = [], [], []

    async def post(*args, **kwargs):
        calls.append(1)
        raise exception

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_attempts=2).generate(
            BrowserAction, {}, on_error=lambda exchange, error: errors.append(error))
    assert len(calls) == 2
    assert raised.value.status == 'WAITING_NETWORK'
    assert errors[-1]['will_retry'] is False
    assert sleeps == [1]


async def test_read_timeout_is_unknown_without_retry(monkeypatch):
    calls, sleeps, errors = [], [], []

    async def post(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout('read')

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_attempts=3).generate(
            BrowserAction, {}, on_error=lambda exchange, error: errors.append(error))
    assert len(calls) == 1
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert errors[0]['will_retry'] is False
    assert sleeps == []


@pytest.mark.parametrize('value', [0, -1, 'x', 1.5, True])
def test_max_attempts_validation(value):
    with pytest.raises(ValueError):
        Gateway(key='ci', max_attempts=value)


def test_max_attempts_reads_environment(monkeypatch):
    monkeypatch.setenv('TRACEFIX_MODEL_MAX_ATTEMPTS', '4')
    assert Gateway(key='ci').max_attempts == 4
