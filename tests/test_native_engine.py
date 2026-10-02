import base64
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tracefix.execution.browser import MCPActionUnknown, MCPBrowser
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import BrowserAction, Decision, Phase
from tracefix.runtime.smoke import PNG, make_engine


pytestmark = pytest.mark.usefixtures('json_completion_transport')


async def prepare_engine(root):
    engine, state = make_engine(root)
    state.phase = Phase.EXPLORE
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    engine.browser.calls.clear()
    engine.store.save(state)
    engine.model = Gateway(key='ci', vision_model='')
    return engine, state


def completion(call_id=None, name='BrowserSnapshot', arguments=None):
    message = {'content': '{"action":{"kind":"finish"}}'}
    if call_id is not None:
        message = {'content': None, 'reasoning_content': 'audit-reasoning', 'tool_calls': [{
            'id': call_id, 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments or {})}}]}
    return httpx.Response(200, json={'usage': {'total_tokens': 2}, 'choices': [{
        'finish_reason': 'tool_calls' if call_id is not None else 'stop', 'message': message}]})


async def test_native_actions_update_observations_receipts_replay_and_audit(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    requests = []

    async def post(client, url, **kwargs):
        request = copy.deepcopy(kwargs['json'])
        requests.append(request)
        if len(requests) == 1:
            return completion('observe-first')
        if len(requests) == 2:
            observed = json.loads(request['messages'][-1]['content'])['observation']
            assert observed == engine.get(state, state.observation_ref)
            return completion('click-fresh', 'BrowserClick', {
                'observation_id': observed['id'], 'element_ref': 'e2',
                'locator': {'role': 'checkbox', 'name': 'Complete task'}})
        return completion()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = await engine.model_call(state, Decision, {})
    assert result.action.kind == 'finish'
    assert [call['kind'] for call in engine.browser.calls] == ['observe', 'click']
    assert state.step == 2 and state.budget.browser_actions == 2
    assert state.budget.model_calls == 3 and state.budget.tokens == 6
    plan = engine.get(state, state.replay_plan_ref)
    assert [action['kind'] for action in plan] == ['observe', 'click']
    assert all(action['observation_id'] is None and action['element_ref'] is None for action in plan)
    observation = engine.get(state, state.observation_ref)
    assert engine.artifacts.exists(state.scope_id, state.run_id, observation['screenshot_ref'])
    trace = engine.store.trace(state.run_id, state.scope_id)
    started = [event['payload'] for event in trace if event['type'] == 'tool.started']
    assert [event['intent']['tool_call_id'] for event in started] == ['observe-first', 'click-fresh']
    assert len([event for event in trace if event['type'] == 'tool.completed']) == 2
    records = [engine.get(state, ref) for ref in state.model_exchange_refs]
    assert len([record for record in records if 'tool_result' in record]) == 2
    assert [record['tool_round'] for record in records if 'request' in record] == [0, 1, 2]


@pytest.mark.parametrize('violation', ['stale', 'unauthorized', 'origin', 'missing-locator'])
async def test_policy_rejects_tools_before_browser_side_effects(tmp_path, monkeypatch, violation):
    engine, state = await prepare_engine(tmp_path)
    observation = engine.get(state, state.observation_ref)
    arguments = {'observation_id': 'stale' if violation == 'stale' else observation['id'],
        'element_ref': 'e2', 'locator': {'role': 'checkbox', 'name': 'Complete task'}}
    name = 'BrowserClick'
    if violation == 'missing-locator':
        arguments['locator']['name'] = 'No such checkbox'
    if violation == 'unauthorized':
        spec = engine.spec(state)
        spec.authorized_actions.remove('click')
        state.test_spec_ref = engine.put(state, spec.model_dump())
    if violation == 'origin':
        name, arguments = 'BrowserNavigate', {'value': 'https://unauthorized.test'}
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        return completion('rejected-call', name, arguments) if len(requests) == 1 else completion()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await engine.model_call(state, Decision, {})
    assert engine.browser.calls == []
    assert state.step == 0 and state.budget.browser_actions == 0
    result = json.loads(requests[1]['messages'][-1]['content'])
    assert result['isError'] is True and result['error']['executed'] is False
    assert result['observation']['id'] == observation['id']


async def test_unknown_mcp_result_stops_model_without_completed_receipt(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    requests = []
    failure = MCPActionUnknown('browser-action', 'observe', RuntimeError('lost acknowledgement'))

    async def action(proposed):
        raise failure

    async def post(client, url, **kwargs):
        requests.append(1)
        return completion('unknown-call')

    monkeypatch.setattr(engine.browser, 'action', action)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(MCPActionUnknown) as raised:
        await engine.model_call(state, Decision, {})
    assert raised.value is failure
    assert len(requests) == 1
    trace = engine.store.trace(state.run_id, state.scope_id)
    assert any(event['type'] == 'tool.started' for event in trace)
    assert not any(event['type'] == 'tool.completed' for event in trace)
    assert any(event['type'] == 'model.error.persisted' for event in trace)
    assert state.replay_plan_ref is None


async def test_revoked_scope_stops_before_reading_or_returning_observation(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        engine.scopes.projects[state.scope_id].access_epoch += 1
        return completion('revoked-scope')

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(ModelError, match='作用域授权已变更'):
        await engine.model_call(state, Decision, {})
    assert len(requests) == 1
    assert engine.browser.calls == []
    assert not any(event['type'] == 'tool.rejected'
                   for event in engine.store.trace(state.run_id, state.scope_id))


@pytest.mark.parametrize('status', ['UNKNOWN_OPERATION', 'WAITING_NETWORK'])
async def test_policy_unknown_status_is_never_converted_to_tool_rejection(tmp_path, monkeypatch, status):
    engine, state = await prepare_engine(tmp_path)
    failure = ModelOutputError('policy needs reconciliation', status=status,
                               details={'requires_manual_review': True})
    requests = []

    def reject(*args):
        raise failure

    async def post(client, url, **kwargs):
        requests.append(1)
        return completion('unknown-policy')

    monkeypatch.setattr(engine.browser.policy, 'browser', reject)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(ModelOutputError) as raised:
        await engine.model_call(state, Decision, {})
    assert raised.value is failure
    assert len(requests) == 1 and engine.browser.calls == []


async def test_stale_native_press_is_rejected_before_operation_and_mcp(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    observation = engine.get(state, state.observation_ref)
    browser = MCPBrowser(['unused'], engine.browser.policy)
    browser.observation = observation
    engine.browser = browser
    requests, mcp_calls = [], []

    async def call(kind, arguments=None):
        mcp_calls.append(kind)
        pytest.fail('stale press must be rejected before reaching MCP')

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        if len(requests) == 1:
            return completion('stale-press', 'BrowserPress', {
                'observation_id': 'previous-observation', 'value': 'Enter'})
        return completion()

    monkeypatch.setattr(browser, 'call', call)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await engine.model_call(state, Decision, {})
    result = json.loads(requests[-1]['messages'][-1]['content'])
    assert result['isError'] is True
    assert result['error']['executed'] is False
    assert result['observation']['id'] == observation['id']
    assert mcp_calls == [] and state.budget.browser_actions == 0
    assert not engine.store.operations
    assert not any(event['type'] == 'tool.started'
                   for event in engine.store.trace(state.run_id, state.scope_id))


async def test_frozen_press_rebinds_current_observation_before_real_mcp_guard(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    state.phase = Phase.REPRODUCE
    engine.store.save(state)
    observation = engine.get(state, state.observation_ref)
    browser = MCPBrowser(['unused'], engine.browser.policy)
    browser.observation = observation
    engine.browser = browser
    mcp_calls = []

    async def call(kind, arguments=None):
        mcp_calls.append((kind, arguments))
        if kind == 'press':
            return []
        if kind == 'snapshot':
            return [SimpleNamespace(type='text', text=observation['snapshot'])]
        if kind == 'screenshot':
            return [SimpleNamespace(type='image', data=base64.b64encode(PNG).decode())]
        pytest.fail('unexpected MCP call: ' + kind)

    monkeypatch.setattr(browser, 'call', call)
    action = BrowserAction(kind='press', value='Enter')
    result_ref = await engine.act(state, action, frozen=True)
    assert action.observation_id is None
    assert browser.last_action['status'] == 'DONE'
    assert browser.last_action['observation_id'] == observation['id']
    assert mcp_calls == [('press', {'key': 'Enter'}), ('snapshot', None),
                         ('screenshot', {'type': 'png'})]
    result = engine.get(state, result_ref)
    assert result['id'] != observation['id']
    assert engine.artifacts.exists(state.scope_id, state.run_id, result['screenshot_ref'])
    events = engine.store.trace(state.run_id, state.scope_id)
    intent = next(event['payload']['intent'] for event in events if event['type'] == 'tool.started')
    assert intent['observation_id'] == observation['id']
    assert len([event for event in events if event['type'] == 'tool.completed']) == 1
