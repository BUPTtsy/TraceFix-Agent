import base64
from types import SimpleNamespace

import pytest

from tracefix.execution.browser import MCPBrowser
from tracefix.runtime.contracts import (Assertion, BehaviorScenario, BehaviorStep, BrowserAction,
    FileEdit, Locator, ObservableWait, PatchProposal, Phase, RunState, digest)
from tracefix.runtime.engine import ActionBusinessFailure
from tracefix.runtime.smoke import PNG, make_engine


async def prepare_engine(root):
    engine, state = make_engine(root)
    state.phase = Phase.EXPLORE
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    engine.browser.calls.clear()
    engine.store.save(state)
    return engine, state

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
