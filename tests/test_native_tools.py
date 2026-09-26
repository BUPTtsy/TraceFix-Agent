import copy
import json

import httpx
import pytest

from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import BrowserAction, Decision, PatchProposal


def tool_call(call_id='call-1', name='browser_snapshot', arguments='{}'):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def completion(calls=None, *, content='{"kind":"finish"}', reasoning=None, finish=None):
    message = {'role': 'assistant', 'content': None if calls is not None else content}
    if calls is not None:
        message['tool_calls'] = calls
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    return httpx.Response(200, json={'model': 'deepseek-chat', 'usage': {'total_tokens': 3},
        'choices': [{'finish_reason': finish or ('tool_calls' if calls is not None else 'stop'),
                     'message': message}]})


def mock_completions(monkeypatch, responses):
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        return responses.pop(0)

    async def sleep(delay):
        pass

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    return requests


async def test_native_preserves_reasoning_and_pairs_results(monkeypatch):
    requests = mock_completions(monkeypatch, [
        completion([tool_call()], reasoning='provider reasoning'), completion()])
    executed, records, usages = [], [], []

    async def execute(name, arguments, call_id):
        executed.append((name, arguments, call_id))
        return {'observation': {'id': 'fresh'}, 'screenshot_ref': 'evidence.png'}

    result = await Gateway(key='ci', vision_model='').generate(
        BrowserAction, {}, image=b'png-evidence', tool_executor=execute,
        on_tool_result=lambda exchange, record: records.append(record), on_usage=usages.append)
    assert result.value.kind == 'finish'
    assert executed == [('browser_snapshot', {}, 'call-1')]
    assert usages == [{'total_tokens': 3}, {'total_tokens': 3}]
    assert requests[0]['model'] == 'deepseek-chat'
    assert 'response_format' not in requests[0]
    assert requests[0]['parallel_tool_calls'] is False
    assert requests[0]['tool_choice'] == 'auto'
    assert len(requests[0]['tools']) == 7
    assert isinstance(requests[0]['messages'][1]['content'], str)
    assert '只返回一个符合' not in requests[0]['messages'][0]['content']
    assistant, tool = requests[1]['messages'][-2:]
    assert assistant['reasoning_content'] == 'provider reasoning'
    assert assistant['tool_calls'] == [tool_call()]
    assert tool['role'] == 'tool' and tool['tool_call_id'] == 'call-1'
    assert json.loads(tool['content'])['observation']['id'] == 'fresh'
    assert records[0]['message'] == tool
    assert 'provider reasoning' not in repr(result)


async def test_native_multiple_rounds_use_new_observation_and_deduplicate_ids(monkeypatch):
    click = tool_call('click-1', 'browser_click', json.dumps({
        'observation_id': 'fresh', 'element_ref': 'e1', 'locator': {'role': 'button', 'name': 'Save'}}))
    requests = mock_completions(monkeypatch, [completion([tool_call()]),
        completion([click]), completion([click]), completion()])
    executed, records = [], []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'observation': {'id': 'fresh' if name == 'browser_snapshot' else 'after-click'}}

    await Gateway(key='ci').generate(BrowserAction, {}, tool_executor=execute,
        on_tool_result=lambda exchange, record: records.append(record))
    assert executed == ['call-1', 'click-1']
    assert [record['reused'] for record in records] == [False, False, True]
    assert requests[-1]['messages'][-1]['content'] == requests[-2]['messages'][-1]['content']


@pytest.mark.parametrize('bad_call', [
    {'id': 'call-bad', 'type': 'not-function', 'function': {'name': 'browser_snapshot', 'arguments': '{}'}},
    tool_call('', 'browser_snapshot'),
    tool_call('call-bad', 'browser_evaluate'),
    tool_call('call-bad', 'browser_snapshot', {}),
    tool_call('call-bad', 'browser_snapshot', 'null'),
    tool_call('call-bad', 'browser_snapshot', '[]'),
    tool_call('call-bad', 'browser_snapshot', '{broken'),
    tool_call('call-bad', 'browser_snapshot', '{"extra":1}'),
    tool_call('call-bad', 'browser_navigate', '{"value":1}'),
    tool_call('call-bad', 'browser_click', '{"observation_id":"fresh","element_ref":"e1","locator":{"role":"button","name":1}}'),
    tool_call(),
])
async def test_entire_batch_is_validated_before_any_execution(monkeypatch, bad_call):
    requests = mock_completions(monkeypatch, [completion([tool_call(), bad_call])])
    executed, errors = [], []

    async def execute(name, arguments, call_id):
        executed.append(call_id)

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {}, tool_executor=execute,
            on_error=lambda exchange, error: errors.append(error))
    assert raised.value.category == 'tool_protocol'
    assert executed == []
    assert len(requests) == 1
    assert errors[0]['will_retry'] is False


async def test_reused_id_with_different_arguments_never_executes(monkeypatch):
    mock_completions(monkeypatch, [completion([tool_call()]),
        completion([tool_call(name='browser_take_screenshot')])])
    executed = []

    async def execute(name, arguments, call_id):
        executed.append(name)
        return {'ok': True}

    with pytest.raises(ModelOutputError, match='不可复用'):
        await Gateway(key='ci').generate(BrowserAction, {}, tool_executor=execute)
    assert executed == ['browser_snapshot']


@pytest.mark.parametrize('finish', ['stop', 'length', 'refusal'])
async def test_inconsistent_tool_finish_never_executes(monkeypatch, finish):
    mock_completions(monkeypatch, [completion([tool_call()], finish=finish)])

    async def execute(*args):
        pytest.fail('inconsistent completion cannot execute tools')

    with pytest.raises(ModelOutputError):
        await Gateway(key='ci').generate(BrowserAction, {}, tool_executor=execute)


@pytest.mark.parametrize('schema,mode', [(PatchProposal, 'native'), (BrowserAction, 'json')])
async def test_tools_are_rejected_when_not_offered(monkeypatch, schema, mode):
    requests = mock_completions(monkeypatch, [completion([tool_call()])])

    async def execute(*args):
        pytest.fail('unoffered tools cannot execute')

    with pytest.raises(ModelOutputError):
        await Gateway(key='ci', tool_mode=mode).generate(schema, {}, tool_executor=execute)
    assert 'tools' not in requests[0]
    assert requests[0]['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('failure', [
    ModelOutputError('unknown', status='UNKNOWN_OPERATION', details={'requires_manual_review': True}),
    RuntimeError('tool may have executed'),
    ValueError('capture failed after tool executed'),
])
async def test_executor_failure_cannot_be_retried_as_recoverable_tool_error(monkeypatch, failure):
    requests = mock_completions(monkeypatch, [completion([tool_call()]), completion()])
    errors = []

    async def execute(*args):
        raise failure

    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {}, tool_executor=execute,
            on_error=lambda exchange, error: errors.append(error))
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == 1
    assert errors[0]['requires_manual_review'] is True
    assert errors[0]['will_retry'] is False


async def test_native_rejects_json_action_even_before_any_tool(monkeypatch):
    mock_completions(monkeypatch, [completion(content='{"kind":"navigate","value":"https://example.test"}')])
    with pytest.raises(ModelOutputError, match='最终 JSON 只能使用 finish'):
        await Gateway(key='ci', max_attempts=1).generate(BrowserAction, {})


async def test_json_action_requires_explicit_mode(monkeypatch):
    requests = mock_completions(monkeypatch, [completion(content='{"kind":"navigate","value":"https://example.test"}')])
    result = await Gateway(key='ci', tool_mode='json').generate(BrowserAction, {})
    assert result.value.kind == 'navigate'
    assert 'tools' not in requests[0]
    assert requests[0]['response_format'] == {'type': 'json_object'}


async def test_final_correction_keeps_tool_results_and_does_not_repeat_actions(monkeypatch):
    rejected = '{"action":{"kind":"observe"}}'
    requests = mock_completions(monkeypatch, [completion([tool_call()]),
        completion(content=rejected), completion(content='{"action":{"kind":"finish"}}')])
    executed = []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'observation': {'id': 'fresh'}}

    await Gateway(key='ci').generate(Decision, {}, tool_executor=execute)
    assert executed == ['call-1']
    assert requests[-1]['messages'][-2] == {'role': 'assistant', 'content': rejected}
    assert '已有工具结果仍然有效' in requests[-1]['messages'][-1]['content']


async def test_complete_history_reuses_recorded_tool_result(monkeypatch):
    requests = mock_completions(monkeypatch, [completion([tool_call()]), completion()])
    history = [{'role': 'assistant', 'content': None, 'reasoning_content': 'retained',
                'tool_calls': [tool_call()]},
               {'role': 'tool', 'tool_call_id': 'call-1', 'name': 'browser_snapshot',
                'content': '{"observation":{"id":"saved"}}'}]

    async def execute(*args):
        pytest.fail('completed call must use recorded result')

    await Gateway(key='ci').generate(BrowserAction, {}, messages=history, tool_executor=execute)
    assert requests[0]['messages'] == history
    assert requests[1]['messages'][-1]['content'] == history[-1]['content']


@pytest.mark.parametrize('history', [
    [{'role': 'assistant', 'tool_calls': [tool_call()]}],
    [{'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'}],
    [{'role': 'assistant', 'tool_calls': [tool_call()]}, {'role': 'user', 'content': 'skip'}],
    [{'role': 'assistant', 'tool_calls': [tool_call()]},
     {'role': 'tool', 'tool_call_id': 'wrong-id', 'content': '{}'}],
])
async def test_unpaired_history_is_rejected_before_request(monkeypatch, history):
    requests = mock_completions(monkeypatch, [])
    with pytest.raises(ModelOutputError, match='工具历史无效'):
        await Gateway(key='ci').generate(BrowserAction, {}, messages=history)
    assert requests == []


async def test_explicit_vision_model_receives_image(monkeypatch):
    requests = mock_completions(monkeypatch, [completion()])
    await Gateway(key='ci', vision_model='configured-vision').generate(BrowserAction, {}, image=b'png')
    assert requests[0]['model'] == 'configured-vision'
    assert requests[0]['messages'][1]['content'][1]['type'] == 'image_url'
