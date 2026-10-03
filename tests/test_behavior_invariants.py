import copy

import pytest
from pydantic import ValidationError

from tracefix.execution.browser import assertions
from tracefix.runtime.contracts import (Assertion, BehaviorScenario, BehaviorStep, BrowserAction,
    FileEdit, Locator, Outcome, PatchProposal, Phase, RunState, RunStatus, TestSpec as FrozenTestSpec,
    Validation, digest, reduce_state, verification_gate)
from tracefix.runtime.smoke import FakeBrowser, make_engine


CONTROL = Locator(role='checkbox', name='Complete task')


def scenario():
    return BehaviorScenario(id='round-trip', description='正向及逆向状态刷新后均保留', steps=[
        BehaviorStep(action=BrowserAction(kind='click', locator=CONTROL),
                     assertions=[Assertion(locator=CONTROL, condition='checked')]),
        BehaviorStep(action=BrowserAction(kind='navigate', value='http://app:3000'),
                     assertions=[Assertion(locator=CONTROL, condition='checked')]),
        BehaviorStep(action=BrowserAction(kind='click', locator=CONTROL),
                     assertions=[Assertion(locator=CONTROL, condition='unchecked')]),
        BehaviorStep(action=BrowserAction(kind='navigate', value='http://app:3000'),
                     assertions=[Assertion(locator=CONTROL, condition='unchecked')]),
    ])


class BehaviorBrowser(FakeBrowser):
    def __init__(self, workspace, failure=None):
        super().__init__(workspace)
        self.failure = failure
        self.checked = False
        self.persisted = False

    async def action(self, action):
        raw = await super().action(action)
        if action.kind == 'click':
            if self.failure != 'no-forward' and not (self.failure == 'no-reverse' and self.checked):
                self.checked = not self.checked
                if self.failure != 'no-reverse-persistence' or self.checked:
                    self.persisted = self.checked
        if action.kind == 'navigate':
            self.checked = self.persisted
        raw['snapshot'] = ('- heading "Task board" [ref=e1]\n'
                           '- checkbox "Complete task" [ref=e2]' + (' [checked]' if self.checked else ''))
        return raw


async def verification_engine(tmp_path, failure=None):
    engine, state = make_engine(tmp_path)
    # 通过真实初始化采样并启动环境，不能用摘要缓存或手写启动状态冒充实时环境。
    engine.store.save(state)
    output = await engine.prepare(state, None)
    state = RunState(**output['data'])
    assert engine.runner.started and not engine.runner.closed
    assert state.source_aligned and state.environment_digest == engine.runner.environment_digest
    spec = engine.spec(state).model_copy(update={'behavior_scenarios': [scenario()]})
    state.test_spec_ref = engine.put(state, spec.model_dump())
    state.test_spec_hash = digest(spec)
    state.reproduction_plan_frozen = True
    state.replay_plan_ref = engine.put(state, [step.action.model_dump() for step in scenario().steps[:2]])
    state.phase = Phase.VERIFY
    state.reproduced = True
    state.execution_mode = 'batch'
    proposal = PatchProposal(summary='有效或无效业务修复候选', evidence_refs=['baseline.json'], edits=[
        FileEdit(path='src/value.ts', before_hash=digest(engine.workspace.read('src/value.ts').encode()),
                 content='export const persisted = true;\n')])
    state.patch_hash = engine.workspace.apply(proposal)['patch_hash']
    engine.browser = BehaviorBrowser(engine.workspace, failure)
    original_command = engine.runner.command

    async def command(kind):
        if kind == 'reset':
            engine.browser.checked = engine.browser.persisted = False
        return await original_command(kind)

    engine.runner.command = command
    engine.store.save(state)
    return engine, state


async def verify_until_transition(engine, state):
    for attempt in range(50):
        output = await engine.verify(state, None)
        state = RunState(**output['data'])
        if state.phase != Phase.VERIFY:
            return state
    pytest.fail('验证阶段未在预期步数内完成')


def gate(engine, state, *, missing=None, transform=None, validations=None):
    def read(ref):
        value = copy.deepcopy(engine.get(state, ref))
        return transform(ref, value) if transform else value

    if validations is None:
        validations = [Validation(**engine.get(state, ref)) for ref in state.validation_refs]
    return verification_gate(state, validations,
        lambda ref: ref != missing and engine.bundle_exists(state, ref), artifact_read=read,
        artifact_read_bytes=lambda ref: engine.artifacts.read(state.scope_id, state.run_id, ref))


@pytest.mark.parametrize('snapshot,passed', [
    ('- checkbox "Complete task" [ref=e1]', True),
    ('- checkbox "Complete task" [checked] [ref=e1]', False),
    ('- checkbox "Complete task" [checked=mixed] [ref=e1]', False),
    ('- checkbox "Complete task" [ref=e1]\n- checkbox "Complete task" [ref=e2]', False),
    ('- heading "Complete task" [ref=e1]', False),
    ('', False),
])
def test_unchecked_requires_unique_checkable_control(snapshot, passed):
    assert assertions(snapshot, [Assertion(locator=CONTROL, condition='unchecked')])['passed'] is passed


def test_unchecked_does_not_accept_a_non_checkable_role():
    check = Assertion(locator=Locator(role='button', name='Save'), condition='unchecked')
    assert not assertions('- button "Save" [ref=e1]', [check])['passed']


@pytest.mark.parametrize('invalid', ['duplicate', 'untrusted-ref', 'unauthorized', 'finish', 'no-final', 'no-intermediate'])
def test_invalid_behavior_specs_are_rejected(invalid):
    raw = FrozenTestSpec(goal='业务行为验证', assertions=[Assertion(locator=CONTROL)],
                   regression_assertions=[Assertion(locator=CONTROL)], behavior_scenarios=[scenario()]).model_dump()
    steps = raw['behavior_scenarios'][0]['steps']
    if invalid == 'duplicate':
        raw['behavior_scenarios'].append(copy.deepcopy(raw['behavior_scenarios'][0]))
    elif invalid == 'untrusted-ref':
        steps[0]['action']['element_ref'] = 'stale-ref'
    elif invalid == 'unauthorized':
        raw['authorized_actions'] = ['navigate', 'finish']
    elif invalid == 'finish':
        steps[0]['action'] = {'kind': 'finish'}
    elif invalid == 'no-final':
        steps[-1]['assertions'] = []
    elif invalid == 'no-intermediate':
        for step in steps[:-1]:
            step['assertions'] = []
    with pytest.raises(ValidationError):
        FrozenTestSpec.model_validate(raw)


async def test_valid_round_trip_passes_all_six_gates_and_behavior_checkpoints(tmp_path):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    assert state.outcome == Outcome.FIX_VERIFIED
    validations = [engine.get(state, ref) for ref in state.validation_refs]
    assert [value['kind'] for value in validations] == ['static', 'unit', 'build', 'health', 'original', 'regression', 'behavior']
    result = engine.get(state, validations[-1]['artifact_ref'])
    assert len(result['checkpoint_refs']) == 4
    assert [engine.get(state, ref)['scenario_step'] for ref in result['checkpoint_refs']] == [1, 2, 3, 4]
    assert gate(engine, state)


@pytest.mark.parametrize('environment_change', ['stale-cache', 'drift', 'inspect-failure', 'closed'])
async def test_behavior_verification_requires_independent_live_environment(tmp_path, environment_change):
    engine, state = await verification_engine(tmp_path)
    frozen_environment = state.environment_digest
    frozen_plan = state.replay_plan_ref
    inspect_calls = engine.runner.inspect_calls
    if environment_change == 'stale-cache':
        engine.runner.actual_digest = 'stale-cache'
    elif environment_change == 'drift':
        engine.runner.environment_digest = 'new-live-environment'
    elif environment_change == 'inspect-failure':
        engine.runner.inspect_results = [RuntimeError('实时环境不可读取')]
    else:
        await engine.runner.close()

    state = await verify_until_transition(engine, state)
    assert engine.runner.inspect_calls == inspect_calls + 1
    assert state.environment_digest == frozen_environment
    assert state.replay_plan_ref == frozen_plan and state.reproduction_plan_frozen
    assert gate(engine, state)
    if environment_change == 'stale-cache':
        assert state.outcome == Outcome.FIX_VERIFIED
        assert engine.runner.actual_digest == frozen_environment
    else:
        assert state.outcome == Outcome.INFRA_FAILURE
        assert state.error_details['terminal_reason'] == 'runtime_environment_gate_failed'


async def test_each_behavior_scenario_starts_from_an_independent_reset(tmp_path):
    engine, state = await verification_engine(tmp_path)
    spec = engine.spec(state)
    spec.behavior_scenarios = [
        BehaviorScenario(id='forward', steps=[scenario().steps[0]]),
        BehaviorScenario(id='fresh-start', steps=[
            BehaviorStep(action=BrowserAction(kind='observe'),
                         assertions=[Assertion(locator=CONTROL, condition='unchecked')]),
            scenario().steps[0],
        ]),
    ]
    state.test_spec_ref = engine.put(state, spec.model_dump())
    state.test_spec_hash = digest(spec)
    engine.store.save(state)
    state = await verify_until_transition(engine, state)
    assert state.outcome == Outcome.FIX_VERIFIED
    assert len(state.validation_refs) == 8 and gate(engine, state)


@pytest.mark.parametrize('failure,step', [('no-forward', 1), ('no-reverse', 3), ('no-reverse-persistence', 4)])
async def test_business_regression_cannot_be_hidden_by_final_state(tmp_path, failure, step):
    engine, state = await verification_engine(tmp_path, failure)
    state.validation_index = 6
    engine.store.save(state)
    state = await verify_until_transition(engine, state)
    assert state.phase == Phase.DIAGNOSE and state.outcome != Outcome.FIX_VERIFIED
    validation = engine.get(state, state.validation_refs[-1])
    assert validation['kind'] == 'behavior' and not validation['passed']
    result = engine.get(state, validation['artifact_ref'])
    assert len(result['checkpoint_refs']) == step
    assert not engine.get(state, result['checkpoint_refs'][-1])['passed']
    assert len(engine.browser.calls) == step + 1


async def test_one_way_patch_passes_original_but_fails_independent_round_trip(tmp_path):
    engine, state = await verification_engine(tmp_path, 'no-reverse')
    state = await verify_until_transition(engine, state)
    values = [engine.get(state, ref) for ref in state.validation_refs]
    assert all(value['passed'] for value in values[:6])
    assert values[-1]['kind'] == 'behavior' and not values[-1]['passed']
    assert not gate(engine, state)


@pytest.mark.parametrize('damage', ['missing-validation', 'missing-checkpoint', 'missing-observation', 'missing-screenshot',
                                  'missing-step', 'wrong-assertion', 'wrong-patch', 'wrong-spec', 'wrong-scenario',
                                  'reordered-steps', 'stale-validation', 'latest-failure', 'forged-pass',
                                  'wrong-observation-hash', 'wrong-action-hash'])
async def test_incomplete_or_stale_behavior_evidence_rejects_fix(tmp_path, damage):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    values = [Validation(**engine.get(state, ref)) for ref in state.validation_refs]
    result_ref = values[-1].artifact_ref
    result = engine.get(state, result_ref)
    check_ref = result['checkpoint_refs'][0]
    check = engine.get(state, check_ref)
    observation_ref = check['observation_ref']
    forged_observation = copy.deepcopy(engine.get(state, observation_ref))
    forged_observation['snapshot'] = forged_observation['snapshot'].replace(' [checked]', '')
    missing = None
    if damage == 'missing-validation':
        values = values[:-1]
    elif damage == 'missing-checkpoint':
        missing = check_ref
    elif damage == 'missing-observation':
        missing = observation_ref
    elif damage == 'missing-screenshot':
        missing = engine.get(state, observation_ref)['screenshot_ref']
    elif damage == 'stale-validation':
        values[-1] = values[-1].model_copy(update={'patch_hash': 'stale'})
    elif damage == 'latest-failure':
        values.append(values[-1].model_copy(update={'passed': False}))

    def transform(ref, value):
        if ref == observation_ref and damage == 'forged-pass':
            return forged_observation
        if ref == result_ref and damage == 'missing-step':
            value['checkpoint_refs'] = value['checkpoint_refs'][1:]
        if ref == result_ref and damage == 'reordered-steps':
            value['checkpoint_refs'] = list(reversed(value['checkpoint_refs']))
        if ref == check_ref:
            if damage == 'forged-pass':
                value['observation_hash'] = digest(forged_observation)
            if damage == 'wrong-assertion':
                value['assertions'][0]['assertion']['condition'] = 'visible'
            for label, field in [('wrong-patch', 'patch_hash'), ('wrong-spec', 'test_spec_hash'), ('wrong-scenario', 'scenario_hash')]:
                if damage == label:
                    value[field] = 'different'
            if damage == 'wrong-observation-hash':
                value['observation_hash'] = 'different'
            if damage == 'wrong-action-hash':
                value['action_hash'] = 'different'
        return value

    if damage == 'missing-screenshot':
        original_exists = engine.bundle_exists
        engine.bundle_exists = lambda saved, ref, depth=0: ref != missing and original_exists(saved, ref, depth)
    assert not gate(engine, state, missing=missing, transform=transform, validations=values)


async def test_frozen_spec_and_post_patch_replay_cannot_be_replaced(tmp_path):
    engine, state = await verification_engine(tmp_path)
    for delta in [{'test_spec_hash': 'new'}, {'test_spec_ref': 'new.json'}, {'replay_plan_ref': 'new.json'},
                  {'reproduction_plan_frozen': False}, {'exploration_plan_ref': 'new.json'}]:
        with pytest.raises(ValueError):
            reduce_state(state, state.revision, **delta)


def test_spec_cannot_be_frozen_for_the_first_time_after_patch():
    state = RunState(scope_id='b', goal='冻结规范不能迟于源码修改', url='http://app:3000', patch_hash='existing')
    with pytest.raises(ValueError, match='修补后不能'):
        reduce_state(state, 0, test_spec_ref='late.json', test_spec_hash='late')


async def test_spec_and_plan_hash_changes_invalidate_behavior_verification(tmp_path):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    for target in (state.test_spec_ref, state.replay_plan_ref):
        def transform(ref, value):
            if ref == target:
                if isinstance(value, dict):
                    value['goal'] = '未经授权改变测试目标'
                else:
                    value.append(BrowserAction(kind='observe').model_dump())
            return value

        assert not gate(engine, state, transform=transform)


async def test_spec_reader_is_required_when_a_frozen_spec_is_present(tmp_path):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    values = [Validation(**engine.get(state, ref)) for ref in state.validation_refs]
    assert not verification_gate(state, values, lambda ref: engine.bundle_exists(state, ref))


async def test_legacy_spec_without_behavior_fields_keeps_original_six_gates(tmp_path):
    engine, state = make_engine(tmp_path)
    raw = engine.get(state, state.test_spec_ref)
    raw.pop('behavior_scenarios')
    raw['max_steps'] = 20
    state.test_spec_ref = engine.put(state, raw)
    state.test_spec_hash = digest(raw)
    assert engine.spec(state).behavior_scenarios == []
    await engine.run(state)
    saved = engine.store.load(state.run_id, state.scope_id)
    assert saved.outcome == Outcome.FIX_VERIFIED, saved.error
    assert len(saved.validation_refs) == 6
    assert gate(engine, saved)


async def test_post_patch_unbound_replay_preserves_frozen_plan_and_finishes_inconclusive(tmp_path):
    engine, state = await verification_engine(tmp_path)
    spec = engine.spec(state)
    spec.behavior_scenarios[0].steps[0].action.locator = Locator(role='button', name='Missing control')
    state.test_spec_ref = engine.put(state, spec.model_dump())
    state.test_spec_hash = digest(spec)
    state.validation_index = 6
    replay_ref = state.replay_plan_ref
    engine.store.save(state)
    await engine.run(state)
    saved = engine.store.load(state.run_id, state.scope_id)
    assert saved.outcome == Outcome.INCONCLUSIVE, saved.error
    assert saved.run_status == RunStatus.COMPLETED
    assert saved.replay_plan_ref == replay_ref and saved.reproduction_plan_frozen


async def test_finalization_rechecks_behavior_evidence(tmp_path):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    validation = engine.get(state, state.validation_refs[-1])
    result = engine.get(state, validation['artifact_ref'])
    missing = result['checkpoint_refs'][0]
    original_exists = engine.bundle_exists
    engine.bundle_exists = lambda saved, ref, depth=0: ref != missing and original_exists(saved, ref, depth)
    output = await engine.finalize(state, None)
    assert output['data']['outcome'] != Outcome.FIX_VERIFIED
    report = engine.get(RunState(**output['data']), output['data']['report_ref'])
    assert report['patch_verification'] == 'unverified'


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'hash-mismatch'])
@pytest.mark.parametrize('outcome', [Outcome.FIX_VERIFIED, Outcome.INFRA_FAILURE])
async def test_finalization_exports_failure_report_when_frozen_spec_is_unavailable(tmp_path, damage, outcome):
    engine, state = await verification_engine(tmp_path)
    state = await verify_until_transition(engine, state)
    expected_diff = engine.workspace.diff()
    spec_path = engine.artifacts.root / state.scope_id / state.run_id / state.test_spec_ref
    if damage == 'missing':
        spec_path.unlink()
    elif damage == 'corrupt':
        spec_path.write_text('{"goal":"tampered"}', encoding='utf-8')
    else:
        state.test_spec_hash = digest({'unexpected': 'spec-hash'})
    state.outcome = outcome
    if outcome == Outcome.INFRA_FAILURE:
        state.error = '冻结规范读取失败后导出失败报告'
    engine.store.save(state)

    output = await engine.finalize(state, None)
    saved = engine.store.load(state.run_id, state.scope_id)
    assert output['next_node'] == 'end'
    assert saved.run_status == RunStatus.FAILED
    assert saved.outcome == Outcome.INFRA_FAILURE
    assert engine.runner.closed and engine.browser.closed
    report = engine.get(saved, saved.report_ref)
    assert report['run_status'] == RunStatus.FAILED
    assert report['outcome'] == Outcome.INFRA_FAILURE
    assert report['patch_available'] and report['patch_verification'] == 'unverified'
    assert report['behavior_scenarios'] is None
    assert report['behavior_scenarios_error']
    assert engine.artifacts.read(saved.scope_id, saved.run_id, report['patch_diff_ref']).decode('utf-8') == expected_diff
    events = engine.store.trace(saved.run_id, saved.scope_id)
    finished = next(event for event in reversed(events) if event['type'] == 'run.finished')
    page = engine.artifacts.read(saved.scope_id, saved.run_id, finished['payload']['html_ref']).decode('utf-8')
    assert '业务场景覆盖不可用' in page and '候选补丁差异' in page
    warning = next(event for event in events if event['type'] == 'run.warning'
                   and event['payload'].get('source') == 'report_behavior_scenarios')
    assert warning['payload']['error'] == report['behavior_scenarios_error']
