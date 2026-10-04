import pytest

from tracefix.model.gateway import ModelResult
from tracefix.runtime.contracts import (Assertion, BrowserAction, Locator, ObservableWait,
                                         Phase, ReproductionPlan)
from tracefix.runtime.smoke import PNG, FakeBrowser, make_engine


async def prepared_engine(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    state = type(state)(**(await engine.prepare(state, None))['data'])
    state.phase = Phase.REPRODUCE
    state.observation_ref = await engine.capture(
        state, await engine.browser.action(BrowserAction(kind='observe')))
    engine.store.save(state)
    return engine, state


async def test_observation_channels_keep_middle_and_expand_by_ref(tmp_path):
    engine, state = await prepared_engine(tmp_path)
    snapshot = '\n'.join([f'- text "line-{i}"' for i in range(300)])
    ref = await engine.capture(state, {
        'id': 'long-observation', 'url': state.url, 'snapshot': snapshot,
        'console': 'console-middle-' + ('x' * 500),
        'network': 'network-middle-' + ('y' * 500), 'png': PNG,
        'page_generation': 4, 'observed_at': 123.0,
        'content_version': 'build-4',
    })
    observation = engine.get(state, ref)
    assert 'line-150' in observation['snapshot']
    assert observation['raw_refs']['snapshot']
    assert 'line-150' in engine.read_observation_channel(state, ref, 'snapshot')
    view = engine.bounded_observation_view(observation, max_snapshot_chars=500)
    assert len(view['snapshot']) <= 500
    assert observation['snapshot'] == snapshot
    assert observation['page_generation'] == 4
    assert observation['content_version'] == 'build-4'


async def test_wrong_action_precondition_does_not_dispatch(tmp_path):
    engine, state = await prepared_engine(tmp_path)
    before = len(engine.browser.calls)
    action = BrowserAction(kind='click', locator=Locator(role='checkbox', name='Complete task'),
        observation_id=engine.get(state, state.observation_ref)['id'], element_ref='e2',
        preconditions=[Assertion(locator=Locator(role='checkbox', name='Complete task'), condition='checked')])
    with pytest.raises(ValueError, match='前置'):
        await engine.act(state, action)
    assert len(engine.browser.calls) == before


class WaitBrowser(FakeBrowser):
    def __init__(self, workspace):
        super().__init__(workspace, bugfree=False)
        self.observations = 0

    async def action(self, action):
        if action.kind == 'observe':
            self.observations += 1
            raw = await super().action(action)
            raw['snapshot'] = raw['snapshot'].replace(' [checked]', '') if self.observations == 1 else raw['snapshot'] + ' [checked]'
            return raw
        raw = await super().action(action)
        raw['snapshot'] = raw['snapshot'].replace(' [checked]', '')
        return raw


async def test_wait_reobserves_without_repeating_side_effect(tmp_path):
    engine, state = await prepared_engine(tmp_path)
    engine.browser = WaitBrowser(engine.workspace)
    observation = engine.get(state, state.observation_ref)
    action = BrowserAction(kind='click', locator=Locator(role='checkbox', name='Complete task'),
        observation_id=observation['id'], element_ref='e2',
        wait=ObservableWait(assertions=[Assertion(locator=Locator(role='checkbox', name='Complete task'), condition='checked')],
                            interval_seconds=0.001, max_observations=2))
    await engine.act(state, action)
    assert [item['kind'] for item in engine.browser.calls].count('click') == 1
    assert [item['kind'] for item in engine.browser.calls].count('observe') >= 1


async def test_frozen_plan_strips_live_handles_and_binds_environment(tmp_path):
    engine, state = await prepared_engine(tmp_path)
    stale = BrowserAction(kind='click', locator=Locator(role='checkbox', name='Complete task'),
                          observation_id='old-observation', element_ref='old-ref', page_generation=99)
    state.replay_plan_ref = engine.put(state, [stale.model_dump(mode='json')], name='操作重放计划')
    async def select(schema, context, **options):
        return ModelResult(ReproductionPlan(action_indices=[0], summary='保留核心动作'), {}, 'fake', 'stop')
    engine.model.generate = select
    frozen = await engine.freeze_reproduction_plan(state)
    selected = engine.get(frozen, frozen.replay_plan_ref)[0]
    assert selected['observation_id'] is None and selected['element_ref'] is None
    assert selected['page_generation'] is None
    assert frozen.reproduction_binding_ref
    binding = engine.get(frozen, frozen.reproduction_binding_ref)
    assert binding['source_manifest'] == frozen.source_manifest
    assert binding['environment_digest'] == frozen.environment_digest
    assert binding['test_spec_hash'] == frozen.test_spec_hash
