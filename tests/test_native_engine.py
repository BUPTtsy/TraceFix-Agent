import base64
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tracefix.execution.browser import MCPActionUnknown, MCPBrowser
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import (Assertion, BehaviorScenario, BehaviorStep, BrowserAction,
    Decision, FileEdit, Locator, ObservableWait, PatchProposal, Phase, RunState, digest)
from tracefix.runtime.engine import ActionBusinessFailure
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
    called = [event['payload'] for event in trace
              if event['type'] == 'operation.called' and event['payload']['name'] == 'browser']
    call_ids = ['observe-first', 'click-fresh']
    assert [event['tool_call_id'] for event in called] == call_ids
    assert [event['execution'] for event in called] == [event['intent']['execution'] for event in started]
    assert all('tool_call_id' not in event['intent'] and 'call_id' not in event['intent'] for event in started)
    completed = [event['payload'] for event in trace if event['type'] == 'tool.completed']
    assert [event['operation_id'] for event in completed] == [event['operation_id'] for event in started]
    records = [engine.get(state, ref) for ref in state.model_exchange_refs]
    audits = [event['payload'] for event in trace if event['type'] == 'model.tool.result.persisted']
    assert [event['tool_call_id'] for event in audits] == call_ids
    assert [engine.get(state, event['result_ref'])['tool_result']['message']['tool_call_id']
            for event in audits] == call_ids
    assert [record['tool_round'] for record in records if 'request' in record] == [0, 1, 2]
    exchange_kinds = ['request' if 'request' in record else 'tool_result'
                      for record in records if 'request' in record or 'tool_result' in record]
    assert exchange_kinds == ['request', 'tool_result', 'request', 'tool_result', 'request']


async def test_action_business_failure_keeps_latest_observation_and_separates_receipt(tmp_path):
    engine, state = await prepare_engine(tmp_path)
    initial = engine.get(state, state.observation_ref)
    action = BrowserAction(
        kind='click',
        observation_id=initial['id'],
        element_ref='e2',
        locator=Locator(role='checkbox', name='Complete task'),
        postconditions=[Assertion(locator=Locator(role='checkbox', name='Complete task'),
                                  condition='checked')],
    )

    with pytest.raises(ActionBusinessFailure, match='后置断言'):
        await engine.act(state, action)

    persisted = engine.store.load(state.run_id, state.scope_id)
    assert persisted.observation_ref != initial['id']
    assert engine.get(persisted, persisted.observation_ref)['id'] != initial['id']
    trace = engine.store.trace(state.run_id, state.scope_id)
    completed = next(event for event in trace if event['type'] == 'tool.completed')
    business = next(event for event in trace if event['type'] == 'action.business.outcome')
    assert completed['payload']['receipt']['observation_ref'] == business['payload']['observation_ref']
    assert business['payload']['status'] == 'failed'
    assert business['payload']['passed'] is False


@pytest.mark.parametrize('check', ['postconditions', 'wait'])
async def test_settled_business_failure_returns_public_tool_result_and_continues(
        tmp_path, monkeypatch, check):
    engine, state = await prepare_engine(tmp_path)
    initial = engine.get(state, state.observation_ref)
    condition = {'locator': {'role': 'checkbox', 'name': 'Complete task'}, 'condition': 'checked'}
    arguments = {'observation_id': initial['id'], 'element_ref': 'e2',
        'locator': condition['locator']}
    if check == 'postconditions':
        arguments['postconditions'] = [condition]
    else:
        arguments['wait'] = {'assertions': [condition], 'timeout_seconds': 1,
            'interval_seconds': 0.01, 'max_observations': 1}
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        if len(requests) == 1:
            return completion('settled-failure', 'BrowserClick', arguments)
        return completion()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = await engine.model_call(state, Decision, {})
    feedback = json.loads(requests[1]['messages'][-1]['content'])
    assert result.action.kind == 'finish'
    assert feedback['isError'] is True and feedback['executed'] is True
    assert feedback['error']['executed'] is True
    assert feedback['error']['type'] == 'ActionBusinessFailure'
    assert feedback['business_outcome']['check'] == check
    assert feedback['business_outcome']['passed'] is False
    assert feedback['business_outcome']['assertions'][0]['passed'] is False
    assert feedback['observation_ref'] == state.observation_ref
    assert feedback['observation']['id'] != initial['id']
    assert state.step == 1 and len(state.action_fingerprints) == 1
    plan = engine.get(state, state.replay_plan_ref)
    assert len(plan) == 1 and plan[0]['kind'] == 'click'
    assert plan[0]['observation_id'] is None and plan[0]['element_ref'] is None
    assert [call['kind'] for call in engine.browser.calls].count('click') == 1
    trace = engine.store.trace(state.run_id, state.scope_id)
    assert len([event for event in trace if event['type'] == 'tool.completed']) == 1
    assert len([event for event in trace if event['type'] == 'model.tool.result.persisted']) == 1
    assert not any(event['type'] == 'model.error.persisted' for event in trace)
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())


async def test_failed_capture_after_browser_dispatch_remains_unknown_without_replay(tmp_path, monkeypatch):
    engine, state = await prepare_engine(tmp_path)
    initial_ref = state.observation_ref
    initial = engine.get(state, state.observation_ref)
    requests = []
    capture_failure = ValueError('lost capture after dispatch')

    async def capture(*args, **kwargs):
        raise capture_failure

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        return completion('capture-unknown', 'BrowserClick', {
            'observation_id': initial['id'], 'element_ref': 'e2',
            'locator': {'role': 'checkbox', 'name': 'Complete task'}})

    monkeypatch.setattr(engine, 'capture', capture)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(ModelError) as raised:
        await engine.model_call(state, Decision, {})
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.__cause__ is capture_failure
    assert len(requests) == 1
    assert [call['kind'] for call in engine.browser.calls] == ['click']
    assert state.step == 0 and state.replay_plan_ref is None
    assert state.observation_ref == initial_ref
    assert all(record['status'] == 'UNKNOWN' for record in engine.store.operations.values())
    trace = engine.store.trace(state.run_id, state.scope_id)
    assert not any(event['type'] == 'tool.completed' for event in trace)
    assert not any(event['type'] == 'model.tool.result.persisted' for event in trace)


def failing_action(check):
    condition = Assertion(locator=Locator(role='checkbox', name='Complete task'), condition='checked')
    return BrowserAction(kind='click', locator=condition.locator,
        postconditions=[condition] if check == 'postconditions' else [],
        wait=ObservableWait(assertions=[condition], timeout_seconds=1,
            interval_seconds=0.01, max_observations=1) if check == 'wait' else None)


@pytest.mark.parametrize('check', ['postconditions', 'wait'])
async def test_frozen_business_failure_counts_real_reproduction_trials(tmp_path, check):
    engine, state = await prepare_engine(tmp_path)
    action = failing_action(check)
    state.phase = Phase.REPRODUCE
    state.reproduction_plan_frozen = True
    state.replay_plan_ref = engine.put(state, [action.model_dump(mode='json')])
    state.replay_index = 1
    engine.store.save(state)
    for trial in range(1, 4):
        output = await engine.reproduce(state, None)
        state = RunState(**output['data'])
        assert state.trial == trial and state.replay_index == 0
        assert len(state.failure_signatures) == trial
        failure = engine.get(state, state.evidence_refs[-1])
        assert failure['passed'] is False
        assert failure['business_check'] == check
        assert failure['assertions'][0]['passed'] is False
        assert failure['observation_ref'] == state.observation_ref
        if trial < 3:
            output = await engine.reproduce(state, None)
            state = RunState(**output['data'])
            assert state.replay_index == 1
    assert state.reproduced and state.phase == Phase.DIAGNOSE
    assert len(set(state.failure_signatures)) == 1
    assert engine.get(state, state.replay_plan_ref)[0] == action.model_dump(mode='json')
    assert [call['kind'] for call in engine.browser.calls].count('click') == 3
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())


@pytest.mark.parametrize('kind', ['original', 'regression', 'behavior'])
@pytest.mark.parametrize('check', ['postconditions', 'wait'])
async def test_frozen_business_failure_becomes_failed_public_validation(tmp_path, kind, check):
    engine, state = await prepare_engine(tmp_path)
    action = failing_action(check)
    visible = Assertion(locator=Locator(role='heading', name='Task board'))
    scenario = BehaviorScenario(id='settled-failure', steps=[
        BehaviorStep(action=action, assertions=[visible])])
    spec = engine.spec(state).model_copy(update={
        'assertions': [visible], 'regression_assertions': [visible],
        'regression_plan': [action], 'behavior_scenarios': [scenario]})
    state.test_spec_ref = engine.put(state, spec.model_dump(mode='json'))
    state.test_spec_hash = digest(spec)
    state.phase = Phase.VERIFY
    state.reproduction_plan_frozen = True
    state.reproduced = state.source_aligned = True
    state.environment_digest = 'fixture-environment'
    state.replay_plan_ref = engine.put(state, [action.model_dump(mode='json')])
    proposal = PatchProposal(summary='保留业务缺陷的失败候选', evidence_refs=[state.observation_ref],
        edits=[FileEdit(path='src/value.ts',
            before_hash=digest(engine.workspace.read('src/value.ts').encode()),
            content='export const persisted = false;\nexport const probe = false;\n')])
    state.patch_ref = engine.put(state, proposal.model_dump(mode='json'))
    state.patch_base_commit = engine.workspace.head()
    state.patch_hash = engine.workspace.apply(proposal)['patch_hash']
    state.validation_index = {'original': 4, 'regression': 5, 'behavior': 6}[kind]
    state.replay_index = 0
    engine.store.save(state)
    output = await engine.verify(state, None)
    state = RunState(**output['data'])
    output = await engine.verify(state, None)
    state = RunState(**output['data'])
    assert state.phase == Phase.DIAGNOSE
    validation = engine.get(state, state.validation_refs[-1])
    assert validation['kind'] == kind and validation['passed'] is False
    feedback = engine.get(state, state.diagnosis_feedback_refs[-1])
    assert feedback['status'] == 'failed'
    assert feedback['failure_class'] == 'counterevidence'
    assert feedback['next_phase'] == 'diagnose'
    assert feedback['failed_assertions'][0]['assertion']['condition'] == 'checked'
    assert feedback['failed_candidate'] is True
    result = engine.get(state, validation['artifact_ref'])
    assert result['passed'] is False
    assert result['observation_ref'] == state.observation_ref
    if kind == 'behavior':
        result = engine.get(state, result['checkpoint_refs'][0])
    assert result['business_check'] == check
    assert result['assertions'][0]['passed'] is False
    assert engine.get(state, state.replay_plan_ref)[0] == action.model_dump(mode='json')
    assert [call['kind'] for call in engine.browser.calls].count('click') == 1
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())


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
        state.test_spec_hash = digest(spec)
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
