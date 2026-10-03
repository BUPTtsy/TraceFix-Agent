"""R11 production evidence wiring, approval barriers and B12 checkpoints."""
import copy
import asyncio

import pytest

from tracefix.execution.browser import assertions
from tracefix.runtime.contracts import (Assertion, BehaviorScenario, BehaviorStep, BrowserAction,
    Locator, Outcome, Phase, RunState, RunStatus, TestSpec as FrozenSpec, Validation, digest)
from tracefix.runtime.smoke import FakeRunner, PNG, make_engine
from tracefix.runtime.verification import verification_binding


KINDS = ['static', 'unit', 'build', 'health', 'original', 'regression', 'behavior']
BINDING = ['scope_id', 'run_id', 'verifier_version', 'source_manifest', 'patch_hash',
           'environment_digest', 'test_spec_hash', 'plan_hash']


def add_behavior_spec(engine, state):
    check = Assertion(locator=Locator(role='checkbox', name='Complete task'), condition='checked')
    scenario = BehaviorScenario(id='B12-round-trip', steps=[
        BehaviorStep(action=BrowserAction(kind='observe'), assertions=[check]),
        BehaviorStep(action=BrowserAction(kind='navigate', value=state.url), assertions=[check])])
    spec = FrozenSpec(goal=state.goal, assertions=[check],
        regression_plan=[BrowserAction(kind='navigate', value=state.url), BrowserAction(kind='observe')],
        regression_assertions=[Assertion(locator=Locator(role='heading', name='Task board'))],
        behavior_scenarios=[scenario])
    state.test_spec_ref = engine.put(state, spec.model_dump(mode='json'))
    state.test_spec_hash = digest(spec)
    return spec


@pytest.fixture
async def verified_engine(tmp_path):
    engine, state = make_engine(tmp_path)
    add_behavior_spec(engine, state)
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.run_status == RunStatus.WAITING_APPROVAL, state.error
    assert state.phase == Phase.REVIEW and state.outcome == Outcome.FIX_VERIFIED
    assert engine.verification_passed(state)
    return engine, state


def result_for(engine, state, kind):
    validation_ref = next(ref for ref in reversed(state.validation_refs) if engine.get(state, ref)['kind'] == kind)
    validation = engine.get(state, validation_ref)
    return validation_ref, validation, engine.get(state, validation['artifact_ref'])


def replace_result(engine, state, kind, result):
    wrapper_ref, validation, _ = result_for(engine, state, kind)
    validation['artifact_ref'] = engine.put(state, result)
    state.validation_refs[state.validation_refs.index(wrapper_ref)] = engine.put(state, validation)
    engine.store.save(state)


async def finish_without_commit(engine, state):
    state.phase, state.run_status = Phase.FINALIZE, RunStatus.RUNNING
    engine.store.save(state)
    await engine.finalize(state, None)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['terminal_reason'] == 'final_verification_invalid'
    assert engine.get(finished, finished.report_ref)['patch_verification'] != 'verified'


async def test_producers_bind_actual_evidence_and_distinct_execution_plans(verified_engine):
    engine, state = verified_engine
    spec = engine.spec(state)
    frozen = engine.get(state, state.replay_plan_ref)
    assert frozen and all(action['observation_id'] is None and action['element_ref'] is None for action in frozen)
    binding = verification_binding(state, digest(frozen))
    scenario = spec.behavior_scenarios[0]
    behavior_hash = digest([step.action.model_dump(mode='json') for step in scenario.steps])
    regression_hash = digest([action.model_dump(mode='json') for action in spec.regression_plan])
    assert regression_hash != digest(frozen)
    for kind in KINDS:
        _, wrapper, result = result_for(engine, state, kind)
        assert wrapper['scope_id'] == state.scope_id and wrapper['run_id'] == state.run_id
        assert wrapper['replay_plan_hash'] == digest(frozen)
        assert wrapper['type'] == 'validation' and wrapper['passed'] is True
        assert result['type'] == 'validation_result' and result['kind'] == kind and result['passed'] is True
        assert {field: result[field] for field in BINDING} == binding
        if kind in {'static', 'unit', 'build', 'health'}:
            assert type(result['exit_code']) is int and result['exit_code'] == 0
            assert result['output'] == 'CI 模拟命令：' + kind
            continue
        observation = engine.get(state, result['observation_ref'])
        assert observation['type'] == 'gui_observation'
        assert {field: observation[field] for field in BINDING} == binding
        png = engine.artifacts.read(state.scope_id, state.run_id, observation['screenshot_ref'])
        assert png == PNG and observation['screenshot_hash'] == digest(png)
        assert result['observation_hash'] == digest(observation)
        if kind != 'behavior':
            checks = spec.assertions if kind == 'original' else spec.regression_assertions
            assert result['assertions'] == assertions(observation['snapshot'], checks)['assertions']
            assert result['execution_plan_hash'] == (digest(frozen) if kind == 'original' else regression_hash)
            continue
        assert result['execution_plan_hash'] == behavior_hash
        checkpoints = [engine.get(state, ref) for ref in result['checkpoint_refs']]
        assert len(checkpoints) == len(scenario.steps)
        for index, (checkpoint, step) in enumerate(zip(checkpoints, scenario.steps), 1):
            assert checkpoint['type'] == 'assertion_checkpoint' and checkpoint['kind'] == 'behavior'
            assert checkpoint['scenario_step'] == index and checkpoint['scenario_hash'] == digest(scenario)
            assert checkpoint['action_hash'] == digest(step.action)
            assert checkpoint['execution_plan_hash'] == behavior_hash
            assert {field: checkpoint[field] for field in BINDING} == binding
            observed = engine.get(state, checkpoint['observation_ref'])
            assert checkpoint['observation_hash'] == digest(observed)
            assert checkpoint['assertions'] == assertions(observed['snapshot'], step.assertions)['assertions']
        assert result['observation_ref'] == checkpoints[-1]['observation_ref']
        assert result['observation_hash'] == checkpoints[-1]['observation_hash']


async def test_approved_local_commit_and_finalize_keep_b12_verified(verified_engine):
    engine, state = verified_engine
    base = state.patch_base_commit
    engine.store.decide_approval(state.approval_ref, state, 'approve')
    await engine.run(resume='approve')
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED and finished.outcome == Outcome.FIX_VERIFIED
    assert finished.patch_base_commit == base and engine.workspace.head() != base
    assert engine.workspace.diff() == ''
    assert digest(engine.workspace.diff(base).encode()) == finished.patch_hash
    assert engine.verification_passed(finished)
    report = engine.get(finished, finished.report_ref)
    assert report['patch_verification'] == 'verified'
    assert report['behavior_scenarios'] == ['B12-round-trip']


@pytest.mark.parametrize('kind', KINDS)
async def test_failed_result_never_inherits_successful_wrapper(verified_engine, kind):
    engine, state = verified_engine
    _, _, result = result_for(engine, state, kind)
    result['passed'] = False
    replace_result(engine, state, kind, result)
    assert not engine.verification_passed(state)
    await finish_without_commit(engine, state)


@pytest.mark.parametrize('field', BINDING + ['type', 'kind', 'passed', 'exit_code', 'output'])
async def test_missing_command_fields_are_not_fabricated(verified_engine, field):
    engine, state = verified_engine
    _, _, result = result_for(engine, state, 'unit')
    result.pop(field)
    replace_result(engine, state, 'unit', result)
    assert not engine.verification_passed(state)


@pytest.mark.parametrize('field', ['scope_id', 'run_id', 'replay_plan_hash', 'passed'])
async def test_corrupt_wrapper_rejected_at_approval_before_commit(verified_engine, field):
    engine, state = verified_engine
    wrapper_ref, wrapper, _ = result_for(engine, state, 'unit')
    wrapper[field] = 'wrong' if field != 'passed' else False
    state.validation_refs[state.validation_refs.index(wrapper_ref)] = engine.put(state, wrapper)
    engine.store.save(state)
    baseline_head = engine.workspace.head()
    engine.store.decide_approval(state.approval_ref, state, 'approve')
    await engine.run(resume='approve')
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.outcome != Outcome.FIX_VERIFIED
    assert engine.workspace.head() == baseline_head
    assert not any(event['type'] == 'tool.started' and event['payload'].get('name') == 'local.commit'
                   for event in engine.store.trace(state.run_id, state.scope_id))


@pytest.mark.parametrize('target', ['result', 'png', 'plan', 'observation'])
async def test_integrity_damage_rejected_after_commit_at_finalize(verified_engine, target):
    engine, state = verified_engine
    _, wrapper, result = result_for(engine, state, 'original')
    observation = engine.get(state, result['observation_ref'])
    ref = {'result': wrapper['artifact_ref'], 'png': observation['screenshot_ref'],
           'plan': state.replay_plan_ref, 'observation': result['observation_ref']}[target]
    original_finalize = engine.finalize

    async def damage_before_final_report(current, next_node):
        assert current.local_branch and engine.workspace.head() != current.patch_base_commit
        (engine.artifacts.root / current.scope_id / current.run_id / ref).write_bytes(b'corrupt')
        return await original_finalize(current, next_node)

    engine.finalize = damage_before_final_report
    engine.store.decide_approval(state.approval_ref, state, 'approve')
    await engine.run(resume='approve')
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['terminal_reason'] == 'final_verification_invalid'
    report = engine.get(finished, finished.report_ref)
    assert report['patch_verification'] == 'unverified'
    if target == 'observation':
        assert report['issues_error'] and '不能确认问题数量' in report['summary']
        events = engine.store.trace(state.run_id, state.scope_id)
        assert any(event['type'] == 'run.warning' and event['payload'].get('source') == 'report_issues'
                   for event in events)
        html_ref = next(event['payload']['html_ref'] for event in events if event['type'] == 'run.finished')
        page = engine.artifacts.read(state.scope_id, state.run_id, html_ref).decode('utf-8')
        assert '问题明细不可用' in page and '<p>未形成有证据的问题记录。</p>' not in page


@pytest.mark.parametrize('fault', ['observation_hash', 'screenshot_hash', 'execution_plan_hash',
    'empty_assertions', 'false_assertions', 'foreign_scope', 'foreign_run', 'invalid_png'])
async def test_gui_hash_and_assertion_forgery_rejected(verified_engine, fault):
    engine, state = verified_engine
    _, _, result = result_for(engine, state, 'regression')
    observation = engine.get(state, result['observation_ref'])
    if fault in {'observation_hash', 'execution_plan_hash'}:
        result[fault] = 'wrong'
    elif fault in {'empty_assertions', 'false_assertions'}:
        result['assertions'] = [] if fault == 'empty_assertions' else [{'passed': True, 'matches': 999}]
    else:
        if fault == 'screenshot_hash':
            observation['screenshot_hash'] = 'wrong'
        elif fault == 'invalid_png':
            observation['screenshot_ref'] = engine.put(state, b'not-a-png', 'png')
            observation['screenshot_hash'] = digest(b'not-a-png')
        else:
            observation['scope_id' if fault == 'foreign_scope' else 'run_id'] = 'foreign'
        result['observation_ref'] = engine.put(state, observation)
        result['observation_hash'] = digest(observation)
    replace_result(engine, state, 'regression', result)
    assert not engine.verification_passed(state)


@pytest.mark.parametrize('fault', ['empty', 'live_observation', 'live_element', 'plan_hash'])
async def test_frozen_plan_cannot_be_empty_or_live_bound_even_when_hashes_rebound(verified_engine, fault):
    engine, state = verified_engine
    plan = engine.get(state, state.replay_plan_ref)
    if fault == 'empty':
        plan = []
    elif fault != 'plan_hash':
        plan[0]['observation_id' if fault == 'live_observation' else 'element_ref'] = 'live'
    else:
        plan[0]['kind'] = 'navigate'
        plan[0]['value'] = state.url
    state.replay_plan_ref = engine.put(state, plan)
    if fault != 'plan_hash':
        binding = verification_binding(state, digest(plan))
        for ref in list(state.validation_refs):
            wrapper = engine.get(state, ref)
            result = engine.get(state, wrapper['artifact_ref'])
            result.update(binding)
            wrapper['replay_plan_hash'] = digest(plan)
            wrapper['artifact_ref'] = engine.put(state, result)
            state.validation_refs[state.validation_refs.index(ref)] = engine.put(state, wrapper)
    engine.store.save(state)
    assert not engine.verification_passed(state)


@pytest.mark.parametrize('fault', ['step', 'action_hash', 'plan_hash', 'order', 'final_observation'])
async def test_b12_checkpoint_binding_cannot_be_reordered_or_replaced(verified_engine, fault):
    engine, state = verified_engine
    _, _, result = result_for(engine, state, 'behavior')
    if fault == 'order':
        result['checkpoint_refs'].reverse()
    elif fault == 'final_observation':
        first = engine.get(state, result['checkpoint_refs'][0])
        result.update(observation_ref=first['observation_ref'], observation_hash=first['observation_hash'])
    else:
        checkpoint = engine.get(state, result['checkpoint_refs'][0])
        checkpoint[{'step': 'scenario_step', 'action_hash': 'action_hash',
                    'plan_hash': 'execution_plan_hash'}[fault]] = 2 if fault == 'step' else 'wrong'
        result['checkpoint_refs'][0] = engine.put(state, checkpoint)
    replace_result(engine, state, 'behavior', result)
    assert not engine.verification_passed(state)


@pytest.mark.parametrize('kind', KINDS)
async def test_latest_failure_blocks_production_verify_instead_of_old_success(verified_engine, kind):
    engine, state = verified_engine
    _, wrapper, result = result_for(engine, state, kind)
    latest = copy.deepcopy(wrapper)
    result['passed'] = False
    latest.update(passed=False, artifact_ref=engine.put(state, result))
    state.validation_refs.append(engine.put(state, latest))
    assert not engine.verification_passed(state)
    if kind == 'behavior':
        await finish_without_commit(engine, state)
        return
    scenario = engine.spec(state).behavior_scenarios[0]
    _, _, behavior = result_for(engine, state, 'behavior')
    state.phase, state.run_status = Phase.VERIFY, RunStatus.RUNNING
    state.validation_index, state.replay_index = 6, len(scenario.steps) + 1
    state.behavior_check_refs = behavior['checkpoint_refs']
    engine.store.save(state)
    output = await engine.verify(state, None)
    rejected = RunState(**output['data'])
    assert rejected.phase == Phase.VERIFY and rejected.validation_index == 0
    assert rejected.validation_refs == [] and rejected.error == '确定性验证门禁拒绝了该证据'


@pytest.mark.parametrize('kind', ['static', 'unit', 'build', 'health'])
async def test_fake_runner_owns_simulated_success_and_nonzero_exit_codes(kind):
    for fail, expected in [(None, 0), (kind, 1)]:
        runner = FakeRunner('source', fail=fail)
        call = runner.health if kind == 'health' else runner.rebuild if kind == 'build' else lambda: runner.command(kind)
        result = await call()
        assert type(result['exit_code']) is int and result['exit_code'] == expected
        assert result['passed'] is (expected == 0) and result['output'] == 'CI 模拟命令：' + kind


@pytest.mark.parametrize('exception', [PermissionError, RuntimeError, AttributeError, TypeError])
async def test_wrapper_reader_exceptions_are_closed(verified_engine, monkeypatch, exception):
    engine, state = verified_engine

    def unreadable(current, ref):
        raise exception('wrapper cannot be read')

    monkeypatch.setattr(engine, 'get', unreadable)
    assert not engine.verification_passed(state)


async def test_missing_behavior_checkpoints_rejects_without_indexing_empty_list(verified_engine):
    engine, state = verified_engine
    scenario = engine.spec(state).behavior_scenarios[0]
    state.phase, state.run_status = Phase.VERIFY, RunStatus.RUNNING
    state.validation_index, state.replay_index = 6, len(scenario.steps) + 1
    state.behavior_check_refs = []
    engine.store.save(state)
    output = await engine.verify(state, None)
    rejected = RunState(**output['data'])
    assert rejected.phase == Phase.DIAGNOSE
    _, wrapper, result = result_for(engine, rejected, 'behavior')
    assert wrapper['passed'] is False and result['checkpoint_refs'] == []
    assert result['observation_ref'] is None and result['observation_hash'] is None


async def test_engine_operation_repeats_reset_and_replay_only_in_new_epoch(verified_engine):
    engine, state = verified_engine
    performed = []
    previous_sequence = engine.store.trace(state.run_id, state.scope_id)[-1]['seq']

    async def reset_or_replay():
        performed.append('effect')
        return {'sequence': len(performed)}

    intent = {'trial': state.trial, 'validation': state.validation_index, 'tool_call_id': 'first'}
    assert await engine.operation(state, 'scenario.reset', {**intent, 'tool_call_id': 'second'}, reset_or_replay) == {'sequence': 1}
    assert await engine.operation(state, 'scenario.reset', intent, reset_or_replay) == {'sequence': 1}
    changed = state.model_copy(update={'revision': state.revision + 1, 'replay_index': 1})
    engine.store.save(changed)
    assert await engine.operation(changed, 'scenario.reset', intent, reset_or_replay) == {'sequence': 2}
    reset_records = [record for operation_id, record in engine.store.operations.items()
                     if ':scenario.reset:' in operation_id]
    record = next(record for record in reset_records if record['status'] == 'DONE')
    assert record['intent_hash']
    started = [event for event in engine.store.trace(state.run_id, state.scope_id)
               if event['seq'] > previous_sequence and event['type'] == 'tool.started'
               and ':scenario.reset:' in event['payload']['operation_id']]
    assert len(started) == 2
    assert all(not {'call_id', 'tool_call_id'} & event['payload']['intent'].keys() for event in started)
    assert all({'phase', 'step', 'replay_index', 'trial', 'validation_index', 'patch_hash'}
               <= set(event['payload']['intent']['execution']) for event in started)


async def test_engine_cancel_keeps_cross_epoch_fence_until_manual_recovery(verified_engine):
    engine, state = verified_engine
    entered = asyncio.Event()

    async def cancelled_reset():
        entered.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(engine.operation(state, 'scenario.reset', {'trial': 0}, cancelled_reset))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation_id, record = next((operation_id, record) for operation_id, record in engine.store.operations.items()
                                if ':scenario.reset:' in operation_id and record['status'] == 'UNKNOWN')
    assert record['status'] == 'UNKNOWN'
    changed = state.model_copy(update={'revision': state.revision + 1})
    with pytest.raises(Exception) as blocked:
        await engine.operation(changed, 'scenario.reset', {'trial': 0}, cancelled_reset)
    assert getattr(blocked.value, 'status', None) == 'UNKNOWN_OPERATION'
    receipt = {'operation_id': operation_id, 'scope_id': state.scope_id, 'run_id': state.run_id,
               'intent_hash': record['intent_hash'], 'resources': record['resources'],
               'outcome': 'not_applied', 'evidence': 'operator verified cancelled reset stopped',
               'execution_stopped': True, 'result': {'executed': False}}
    engine.store.reconcile(state, operation_id, receipt, reviewer='operator')
    performed = []

    async def recovered_reset():
        performed.append('new epoch')
        return {'sequence': 2}

    assert await engine.operation(changed, 'scenario.reset', {'trial': 0}, recovered_reset) == {'sequence': 2}
    assert performed == ['new epoch']


async def test_real_reset_and_replay_each_execute_in_new_engine_position(tmp_path):
    engine, state = make_engine(tmp_path)
    state.phase = Phase.REPRODUCE
    engine.store.save(state)
    resets = []
    command = engine.runner.command

    async def record_reset(kind):
        if kind == 'reset':
            resets.append(kind)
        return await command(kind)

    engine.runner.command = record_reset
    first = await engine.reset(state)
    assert await engine.reset(state) == first
    assert resets == ['reset'] and len(engine.browser.calls) == 1
    state = engine.changed(state, observation_ref=first['observation_ref'], trial=1)
    second = await engine.reset(state)
    assert second != first and resets == ['reset', 'reset']
    state = engine.changed(state, observation_ref=second['observation_ref'], replay_index=1)
    action = BrowserAction(kind='observe')
    observed = await engine.act(state, action, frozen=True)
    assert await engine.act(state, action, frozen=True) == observed
    assert len(engine.browser.calls) == 3
    state = engine.changed(state, observation_ref=observed, replay_index=2)
    assert await engine.act(state, action, frozen=True) != observed
    assert len(engine.browser.calls) == 4


async def test_explicit_operation_key_reuses_receipt_across_phase_epoch_and_audit_call(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    performed = []

    async def perform():
        performed.append('effect')
        return {'executed': True}

    original = {'value': 'same', 'call_id': 'first-call', 'tool_call_id': 'first-tool'}
    assert await engine.operation(state, 'workspace.patch', original, perform, idempotency_key='stable') == {'executed': True}
    changed = state.model_copy(update={'revision': 4, 'phase': Phase.REVIEW, 'step': 8,
                                      'continuation_count': 2, 'patch_hash': 'new-patch'})
    engine.store.save(changed)
    rebound = {**original, 'call_id': 'next-call', 'tool_call_id': 'next-tool'}
    assert await engine.operation(changed, 'workspace.patch', rebound, perform, idempotency_key='stable') == {'executed': True}
    assert performed == ['effect'] and len(engine.store.operations) == 1
    calls = [event['payload'] for event in engine.store.trace(state.run_id, state.scope_id)
             if event['type'] == 'operation.called']
    assert [(call['call_id'], call['tool_call_id']) for call in calls] == [
        ('first-call', 'first-tool'), ('next-call', 'next-tool')]
    started = next(event for event in engine.store.trace(state.run_id, state.scope_id)
                   if event['type'] == 'tool.started')
    assert 'execution' not in started['payload']['intent']


@pytest.mark.parametrize('fault', ['digest', 'artifact', 'legacy', 'scope', 'repo_id', 'missing_mount'])
async def test_prepare_rejects_snapshot_mismatch_before_runner(tmp_path, fault):
    engine, state = make_engine(tmp_path)
    if fault == 'digest':
        state.source_manifest = 'wrong-source-manifest'
    elif fault == 'artifact':
        altered = copy.deepcopy(engine.source)
        altered['repo_id'] = 'other'
        state.repo_snapshot_ref = engine.put(state, altered)
    elif fault == 'legacy':
        engine.source = {'commit': engine.source['commit'], 'files': engine.source['files']}
        state.source_manifest = digest(engine.source)
        state.repo_snapshot_ref = engine.put(state, engine.source)
    elif fault == 'scope':
        state.scope_id = 'other'
    elif fault == 'repo_id':
        engine.source = {**engine.source, 'repo_id': 'other'}
        state.source_manifest = digest(engine.source)
        state.repo_snapshot_ref = engine.put(state, engine.source)
    else:
        (engine.workspace.root / 'dist').rmdir()
    engine.store.save(state)
    calls = []
    original_inspect = engine.runner.inspect_images
    original_start = engine.runner.start

    async def inspect():
        calls.append('inspect')
        return await original_inspect()

    async def start(source):
        calls.append('start')
        return await original_start(source)

    engine.runner.inspect_images = inspect
    engine.runner.start = start
    with pytest.raises(PermissionError):
        await engine.prepare(state, None)
    assert calls == []


async def test_behavior_intermediate_failure_stops_before_next_action(verified_engine):
    engine, state = verified_engine
    state.phase, state.run_status = Phase.VERIFY, RunStatus.RUNNING
    state.validation_index, state.replay_index = 6, 1
    state.behavior_check_refs = []
    engine.store.save(state)
    original_action = engine.browser.action
    attempted = []

    async def fail_first_checkpoint(action):
        attempted.append(action.kind)
        raw = await original_action(action)
        raw['snapshot'] = raw['snapshot'].replace(' [checked]', '')
        return raw

    engine.browser.action = fail_first_checkpoint
    output = await engine.verify(state, None)
    failed = RunState(**output['data'])
    assert failed.phase == Phase.DIAGNOSE and attempted == ['observe']
    _, wrapper, result = result_for(engine, failed, 'behavior')
    assert wrapper['passed'] is False and result['passed'] is False
    assert len(result['checkpoint_refs']) == 1
    checkpoint = engine.get(failed, result['checkpoint_refs'][0])
    assert checkpoint['scenario_step'] == 1 and checkpoint['passed'] is False
    assert result['observation_ref'] == checkpoint['observation_ref']


async def test_owned_started_requires_manual_exit_recovery_before_engine_retry(tmp_path):
    from tracefix.storage.store import UnknownOperation

    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    intent = engine.operation_intent(state, 'workspace.patch', {'summary': 'crashed patch'})
    engine.store.begin(state, 'crashed-owned', intent, owner='crashed-owner')
    record = engine.store.operations['crashed-owned']
    changed = state.model_copy(update={'revision': 1})
    attempted = []

    async def patch():
        attempted.append('patch')
        return {'executed': True}

    with pytest.raises(UnknownOperation):
        await engine.operation(changed, 'workspace.patch', {'summary': 'new patch'}, patch)
    recovery = {'operation_id': 'crashed-owned', 'scope_id': state.scope_id, 'run_id': state.run_id,
        'intent_hash': record['intent_hash'], 'resources': record['resources'],
        'owner_token': record['owner_token'], 'epoch': record['epoch'],
        'execution_stopped': True, 'evidence': 'operator verified process exit and unchanged files'}
    with pytest.raises(PermissionError):
        engine.store.recover_started(changed, 'crashed-owned', {**recovery, 'execution_stopped': False},
                                     reviewer='operator')
    engine.store.recover_started(changed, 'crashed-owned', recovery, reviewer='operator')
    with pytest.raises(UnknownOperation):
        await engine.operation(changed, 'workspace.patch', {'summary': 'new patch'}, patch)
    engine.store.reconcile(changed, 'crashed-owned', {**recovery, 'outcome': 'not_applied',
        'result': {'executed': False}}, reviewer='operator')
    assert await engine.operation(changed, 'workspace.patch', {'summary': 'new patch'}, patch) == {'executed': True}
    assert attempted == ['patch']
