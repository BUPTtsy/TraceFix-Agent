import asyncio
import copy
from types import SimpleNamespace

import pytest

from tracefix.execution.browser import MCPActionUnknown
from tracefix.knowledge.assembler import ContextAssembler
from tracefix.runtime.contracts import (BrowserAction, Decision, FindingRepairSource, Outcome,
    Phase, RunState, RunStatus)
from tracefix.runtime.recovery import RecoveryCause, has_real_progress
from tracefix.runtime.smoke import make_engine
from tracefix.service.resources import RunServices
from tracefix.storage.store import UnknownOperation


@pytest.fixture
async def observed_engine(tmp_path):
    engine, state = make_engine(tmp_path)
    state.phase = Phase.EXPLORE
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    state.loop_no_progress_steps = engine.LOOP_NO_PROGRESS_LIMIT
    engine.store.save(state)
    return engine, state


async def reference_services(engine, state, documents=()):
    request = SimpleNamespace(run_id=state.run_id, repo_id=state.scope_id, github_reads=[])
    services = RunServices(request, SimpleNamespace())
    services.own(engine.runner)
    services.own(engine.browser)
    await services.prepare()
    services._documents = list(documents)
    engine.runtime_services = services
    return services


@pytest.mark.parametrize('wrapped', [False, True])
def test_semantic_progress_ignores_reference_and_transport_churn(wrapped):
    before = '- checkbox "Complete task" [ref=e2]'
    after = '- checkbox "Complete task" [ref=e99]'
    if wrapped:
        before = {'snapshot': before, 'screenshot_ref': 'first', 'observed_at': 1,
                  'error': 'stale', 'strategy': 'first', 'attempt': 1}
        after = {'snapshot': after, 'screenshot_ref': 'next', 'observed_at': 20,
                 'error': None, 'strategy': 'next', 'attempt': 9}
    assert not has_real_progress(before, after)


async def test_refresh_clear_error_and_changed_strategy_still_end_with_finite_budget(observed_engine):
    engine, state = observed_engine
    refreshes = []

    async def observe():
        refreshes.append(True)
        raw = await engine.browser.action(BrowserAction(kind='observe'))
        raw.update(observed_at=len(refreshes), page_generation=len(refreshes))
        raw['snapshot'] = raw['snapshot'].replace('[ref=e2]', '[ref=refresh]')
        return raw

    engine.browser.observe = observe
    state.error = 'stale observation'
    state.last_error_signature = 'old-error'
    engine.store.save(state)
    state, stopped = await engine._record_loop_boundary(state)
    assert not stopped and refreshes == [True]
    assert state.error is None and state.last_error_signature is None
    assert state.loop_no_progress_steps >= engine.LOOP_NO_PROGRESS_LIMIT
    assert state.recovery_episodes[0]['progress'] is False
    assert state.loop_evidence['recovery']['business_changed'] is False
    grace_state, stopped = await engine._record_loop_boundary(state)
    assert not stopped and not grace_state.loop_evidence['recovery']['business_changed']
    state = grace_state
    attempts = 0
    for attempts in range(1, 16):
        state = engine.changed(state, pending_action={'kind': 'observe', 'strategy': attempts},
                               error='stale observation', last_error_signature=f'cleared-{attempts}')
        state, stopped = await engine._record_loop_boundary(state)
        if stopped:
            break
    assert stopped and attempts < 16
    assert state.outcome == Outcome.LOOP_DETECTED
    assert state.loop_no_progress_steps > engine.LOOP_NO_PROGRESS_LIMIT
    assert len(refreshes) == 1
    stale = [entry for entry in state.recovery_episodes if entry['cause'] == RecoveryCause.STALE]
    assert [entry['attempt'] for entry in stale] == [1, 2]
    assert len({entry['episode_id'] for entry in stale}) == 1


async def test_correct_strategy_does_not_change_episode_or_reset_progress(observed_engine):
    engine, state = observed_engine
    state.phase = Phase.VERIFY
    state.last_error_signature = 'first-error'
    engine.store.save(state)
    signals = [{'kind': 'error_repeated', 'signature': 'first-error', 'count': 5}]
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [], signals)
    assert state.phase == Phase.DIAGNOSE
    episode = state.recovery_episodes[-1]['episode_id']
    assert not state.recovery_episodes[-1]['progress']
    assert state.loop_no_progress_steps == engine.LOOP_NO_PROGRESS_LIMIT + 1
    state = engine.changed(state, pending_action={'kind': 'observe'}, last_error_signature='next-error')
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [],
                                   [{'kind': 'no_progress', 'steps': state.loop_no_progress_steps}])
    assert state.outcome == Outcome.LOOP_DETECTED
    assert state.recovery_episodes[-1]['attempt'] == 2
    assert state.recovery_episodes[-1]['episode_id'] == episode


async def test_no_progress_grace_allows_real_engine_action_before_stop(observed_engine):
    engine, state = observed_engine
    state, stopped = await engine._record_loop_boundary(state)
    assert not stopped
    assert state.recovery_episodes[-1]['cause'] == RecoveryCause.REPEATED_NO_PROGRESS
    assert state.loop_no_progress_steps > engine.LOOP_NO_PROGRESS_LIMIT
    ref = await engine.act(state, BrowserAction(kind='observe'))
    state = engine.changed(state, observation_ref=ref, step=state.step + 1)
    state, stopped = await engine._record_loop_boundary(state)
    assert not stopped
    state = engine.changed(state, pending_action=None)
    state, stopped = await engine._record_loop_boundary(state)
    assert stopped
    assert state.recovery_episodes[-1]['attempt'] == 2


async def test_batch_graph_no_progress_recovery_executes_once_then_finishes(observed_engine):
    engine, state = observed_engine
    state.execution_mode = 'batch'
    initial_actions = len(engine.browser.calls)
    generate = engine.model.generate

    async def observe_forever(schema, context, **options):
        engine.model.decision_calls = 0
        return await generate(schema, context, **options)

    engine.model.generate = observe_forever
    await engine.run(state)
    final = engine.store.load(state.run_id, state.scope_id)
    assert final.run_status == RunStatus.ABNORMAL
    assert final.outcome == Outcome.LOOP_DETECTED
    assert final.step == 1 and final.budget.model_calls == 1
    assert len(engine.browser.calls) == initial_actions + 1
    assert [entry['attempt'] for entry in final.recovery_episodes] == [1, 2]
    assert all(entry['progress'] is False for entry in final.recovery_episodes)
    assert engine.browser.closed and engine.runner.closed


async def test_refresh_with_changed_business_assertion_records_progress_and_new_episode(observed_engine):
    engine, state = observed_engine
    raw = await engine.browser.action(BrowserAction(kind='observe'))
    raw['assertions'] = [{'condition': 'checked', 'passed': False}]
    state.observation_ref = await engine.capture(state, raw)
    state.error = 'stale observation'
    engine.store.save(state)

    async def observe():
        current = await engine.browser.action(BrowserAction(kind='observe'))
        current['assertions'] = [{'condition': 'checked', 'passed': True}]
        return current

    engine.browser.observe = observe
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [],
                                   [{'kind': 'no_progress', 'steps': state.loop_no_progress_steps}])
    assert state.loop_no_progress_steps == 0
    assert state.recovery_episodes[-1]['progress'] is True
    assert state.recovery_episodes[-1]['status'] == 'progressed'
    assert state.loop_evidence['recovery']['business_changed'] is True
    previous_episode = state.recovery_episodes[-1]['episode_id']
    state = engine.changed(state, error='stale observation')
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [],
                                   [{'kind': 'state_repeated', 'count': 3}])
    assert state.recovery_episodes[-1]['attempt'] == 1
    assert state.recovery_episodes[-1]['episode_id'] != previous_episode
    assert state.recovery_episodes[-1]['progress'] is False
    assert state.loop_no_progress_steps == 2


async def test_verification_reference_churn_is_not_new_business_progress(observed_engine):
    engine, state = observed_engine
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [],
                                   [{'kind': 'no_progress', 'steps': state.loop_no_progress_steps}])
    first = engine.put(state, {'kind': 'original', 'passed': False, 'artifact_ref': 'first'})
    state = engine.changed(state, validation_refs=[first])
    assert state.loop_no_progress_steps == 0
    second = engine.put(state, {'kind': 'original', 'passed': False, 'artifact_ref': 'next'})
    state = engine.changed(state, validation_refs=[second])
    assert state.loop_no_progress_steps == 1
    state = engine.changed(state, validation_refs=[])
    assert state.loop_no_progress_steps == 2
    state = engine.changed(state, validation_refs=[second], check_suite_completed=True)
    assert state.loop_no_progress_steps == 3
    third = engine.put(state, {'kind': 'original', 'passed': True})
    state = engine.changed(state, validation_refs=[third])
    assert state.loop_no_progress_steps == 0


@pytest.mark.parametrize('boundary', ['unknown', 'cancelled'])
async def test_unknown_and_cancelled_operations_keep_receipt_identity_and_are_not_replayed(
        observed_engine, boundary):
    engine, state = observed_engine
    performed = []

    async def uncertain():
        performed.append(True)
        if boundary == 'cancelled':
            raise asyncio.CancelledError
        raise MCPActionUnknown('unknown-action', 'click', RuntimeError('unknown observation result'))

    error_type = asyncio.CancelledError if boundary == 'cancelled' else MCPActionUnknown
    with pytest.raises(error_type) as caught:
        await engine.operation(state, 'browser', {'kind': 'click'}, uncertain)
    operation_id = caught.value.tracefix_operation_id
    record = copy.deepcopy(engine.store.operations[operation_id])
    state.error = '用户已取消' if boundary == 'cancelled' else 'UNKNOWN_OPERATION'
    state.error_details = {'status': 'CANCELLED' if boundary == 'cancelled' else 'UNKNOWN_OPERATION',
                           'operation_id': operation_id}
    engine.store.save(state)
    state = await engine._mark_loop(state, engine._loop_state_fingerprint(state), [],
                                   [{'kind': 'no_progress', 'steps': state.loop_no_progress_steps}])
    assert state.recovery_episodes[-1]['action'] == 'stop'
    assert state.outcome == (Outcome.INCONCLUSIVE if boundary == 'cancelled' else Outcome.INFRA_FAILURE)
    assert state.error == ('用户已取消' if boundary == 'cancelled' else 'UNKNOWN_OPERATION')
    assert state.error_details['operation_id'] == operation_id
    with pytest.raises(UnknownOperation) as blocked:
        await engine.operation(state, 'browser', {'kind': 'click'}, uncertain)
    assert blocked.value.details['operation_id'] == operation_id
    assert performed == [True]
    assert engine.store.operations == {operation_id: record}


async def test_services_context_uses_reference_budget_and_preserves_protected_state(observed_engine):
    engine, state = observed_engine
    state.repair_source = FindingRepairSource(
        source_run_id='source-run', source_finding_ids=['source-finding'], source_scope_id=state.scope_id,
        source_base_commit='a' * 40, source_manifest='source-manifest', source_repo_snapshot_ref='source-repo',
        source_repo_snapshot={}, source_rule_snapshot_ref='source-rules', source_rule_snapshot_hash='rules-hash',
        source_rule_snapshot={}, source_evidence_refs=['source-evidence'], findings=[{'id': 'source-finding'}])
    engine.store.save(state)
    documents = [{'id': 'github-small', 'kind': 'github_context', 'content': 'Task board', 'untrusted': True},
                 {'id': 'github-large', 'content': 'content ' * 6000, 'test_spec': {'goal': 'override'},
                  'detection_rules': {'rule_ids': ['foreign']}, 'allowed_urls': ['https://outside.invalid']}]
    services = await reference_services(engine, state, documents)
    engine.assembler = ContextAssembler(context_window=7000, output_tokens=1000, overhead_tokens=300)
    captured = []
    generate = engine.model.generate

    async def capture_context(schema, context, **options):
        captured.append(copy.deepcopy(context))
        return await generate(schema, context, **options)

    engine.model.generate = capture_context
    context = {'test_spec': engine.spec(state).model_dump(mode='json'),
               'detection_rules': {'rule_ids': ['frozen']}, 'allowed_urls': [state.url],
               'authorized_actions': ['observe', 'finish'],
               'reference_documents': [{'id': 'existing', 'content': 'local rule context'}]}
    await engine.model_call(state, Decision, context)
    assert services._context_consumed
    sent = captured[0]
    for field in ('test_spec', 'detection_rules', 'allowed_urls', 'authorized_actions'):
        assert sent[field] == context[field]
    assert sent['repair_source'] == state.repair_source.model_dump(mode='json')
    assert {item['id'] for item in sent['reference_documents']} == {'existing', 'github-small'}
    assert services._documents == documents
    manifest = engine.get(state, state.context_manifest_refs[-1])
    block = next(item for item in manifest['blocks'] if item['name'] == 'reference_documents')
    assert not block['protected'] and 'github-large' in block['trimmed_ids']
    await services.aclose()


@pytest.mark.parametrize('result', ['success', 'failure', 'cancelled', 'paused', 'partial_failure'])
async def test_engine_services_lifecycle_covers_run_boundaries(observed_engine, result):
    engine, state = observed_engine
    services = await reference_services(engine, state)
    closes = []

    class Resource:
        async def close(self):
            closes.append(True)

    services.own(Resource())

    async def invoke(invocation, config):
        if result in {'failure', 'partial_failure'}:
            raise ValueError('initialization failed')
        if result == 'cancelled':
            raise asyncio.CancelledError
        latest = engine.store.load(state.run_id, state.scope_id)
        latest.run_status = RunStatus.PAUSED if result == 'paused' else RunStatus.COMPLETED
        engine.store.save(latest)
        return invocation

    engine.graph = SimpleNamespace(ainvoke=invoke)
    if result == 'partial_failure':
        engine.store.writer = lambda run_id: (_ for _ in ()).throw(ValueError('writer failed'))
    expected_error = asyncio.CancelledError if result == 'cancelled' else ValueError
    if result in {'failure', 'cancelled', 'partial_failure'}:
        with pytest.raises(expected_error):
            await engine.run(state)
    else:
        await engine.run(state)
    if result == 'paused':
        assert closes == [] and services._close_task is None
        assert await services.reference_documents(state) == []
    else:
        assert closes == [True]
        assert engine.browser.closed and engine.runner.closed
        assert services._documents == []
    await services.aclose()
    assert closes == [True]
