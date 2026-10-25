import asyncio

import pytest

from tracefix.runtime.contracts import (BrowserAction, Locator, Outcome, Phase, ReplayUnbound,
    RunStatus, digest)
from tracefix.model.gateway import ModelError, ModelOutputError, ModelResult
from tracefix.runtime.contracts import Decision, FileEdit, PatchProposal
from tracefix.tools.effects import operation_identity
from tracefix.runtime.engine import normalize_decision_evidence_refs, repeated_action, stable_snapshot
from tracefix.runtime.smoke import make_engine as make_smoke_engine
from tracefix.storage.store import UnknownOperation
from tests.support_sdk_candidate import verified_sdk_candidate, finalize_sdk_candidate


def make_engine(*args, **kwargs):
    engine, state = make_smoke_engine(*args, **kwargs)
    state.mode = 'test'
    return engine, state


async def test_operation_notifies_committed_redacted_events_once(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    received = []

    def notify(event):
        assert event in engine.store.trace(state.run_id, state.scope_id)
        received.append(event)

    async def perform():
        assert [event['type'] for event in received] == ['tool.started']
        return {'passed': True}

    engine.notify = notify
    intent = {'Authorization': 'Bearer private-token', 'action': '中文任务'}
    result = await engine.operation(state, 'test', intent, perform)
    assert result == {'passed': True}
    assert [event['type'] for event in received] == ['tool.started', 'tool.completed']
    assert 'private-token' not in str(received)
    assert '中文任务' in str(received)
    assert await engine.operation(state, 'test', intent, perform) == result
    assert received == engine.store.trace(state.run_id, state.scope_id)
    assert len(received) == 2


@pytest.mark.parametrize('execution_mode', ['interactive', 'batch'])
@pytest.mark.parametrize('failure_stage', ['perform', 'finish'])
@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
async def test_operation_failure_stays_unknown_until_reconciled(
        tmp_path, monkeypatch, execution_mode, failure_stage, error_type):
    engine, state = make_engine(tmp_path)
    state.execution_mode = execution_mode
    engine.store.save(state)
    received = []
    attempts = []
    automatic_reviews = []
    engine.notify = received.append

    async def fail():
        attempts.append('called')
        if failure_stage == 'perform':
            raise error_type('tool failed')
        return {'executed': True}

    if failure_stage == 'finish':
        def failed_finish(*args, **kwargs):
            raise error_type('tool failed')

        monkeypatch.setattr(engine.store, 'finish', failed_finish)

    with pytest.raises(error_type, match='tool failed'):
        await engine.operation(state, 'test', {}, fail)
    op_id = received[0]['payload']['operation_id']
    record = engine.store.operations[op_id]
    assert record['status'] == 'UNKNOWN'
    assert record['receipt'] is None
    assert record['resolution'] == {'reason': error_type.__name__}
    assert [event['type'] for event in received] == ['tool.started', 'tool.unknown']
    assert received[-1]['payload'] == {
        'operation_id': op_id, 'reason': error_type.__name__, 'resources': record['resources']}

    with pytest.raises(UnknownOperation):
        await engine.operation(state, 'test', {}, fail)

    def automatic_review():
        automatic_reviews.append('sync')
        return {'recovered': True}

    async def asynchronous_review():
        automatic_reviews.append('async')
        return {'recovered': True}

    for callback in (automatic_review, asynchronous_review):
        with pytest.raises(UnknownOperation) as failure:
            await engine.operation(state, 'test', {}, fail, reconcile=callback)
        assert failure.value.details['requires_manual_review'] is True
        assert failure.value.details['retryable'] is False
        assert 'store.reconcile' in failure.value.details['recovery']
        assert record['status'] == 'UNKNOWN'
    for update in ({'revision': state.revision + 1},
                   {'continuation_count': state.continuation_count + 1}):
        with pytest.raises(UnknownOperation) as failure:
            await engine.operation(state.model_copy(update=update), 'test', {}, fail)
        assert failure.value.details['operation_id'] == op_id
    assert automatic_reviews == []
    assert attempts == ['called']
    assert len(engine.store.operations) == 1

    review = dict(operation_id=op_id, scope_id=state.scope_id, run_id=state.run_id,
        intent_hash=record['intent_hash'], resources=record['resources'], outcome='completed',
        execution_stopped=True, evidence='operator verified process exit and completed result',
        result={'recovered': True, 'executed': True})
    for invalid in ({'operation_id': 'other'}, {'scope_id': 'other'}, {'run_id': 'other'},
                    {'intent_hash': 'other'}, {'resources': ['other']},
                    {'execution_stopped': False}, {'evidence': ''}, {'result': None}):
        with pytest.raises(PermissionError):
            engine.store.reconcile(state, op_id, {**review, **invalid}, reviewer='operator')
        assert record['status'] == 'UNKNOWN'
        assert record['receipt'] is None
    with pytest.raises(PermissionError):
        engine.store.reconcile(state, op_id, review, reviewer='')
    assert [event['type'] for event in received] == ['tool.started', 'tool.unknown']
    assert engine.store.reconcile(state, op_id, review, reviewer='operator',
                                  notify=received.append) == review['result']
    assert record['status'] == 'DONE'
    assert record['receipt'] == review['result']
    assert record['resolution'] == {**review, 'reviewer': 'operator'}
    # 人工审核后的重试只能复用持久回执，不得重放 perform 或自动核对回调。
    assert await engine.operation(state, 'test', {}, fail, reconcile=automatic_review) == review['result']
    assert attempts == ['called']
    assert automatic_reviews == []
    assert [event['type'] for event in received] == ['tool.started', 'tool.unknown', 'tool.reconciled']
    assert received[-1]['payload'] == {
        'operation_id': op_id, 'reviewer': 'operator', 'receipt': review}
    assert received == engine.store.trace(state.run_id, state.scope_id)


@pytest.mark.parametrize('asynchronous', [False, True])
async def test_operation_started_rejects_automatic_recovery_without_borrowing_owner(tmp_path, asynchronous):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    intent = engine.operation_intent(state, 'test', {})
    op_id = operation_identity(state, 'test', intent)
    engine.store.begin(state, op_id, intent, owner='original-owner')
    record = engine.store.operations[op_id]
    original = dict(record)
    events = engine.store.trace(state.run_id, state.scope_id)
    attempts = []
    automatic_reviews = []

    async def perform():
        attempts.append('called')
        return {'executed': True}

    def automatic_review():
        automatic_reviews.append('called')
        return {'executed': True}

    async def asynchronous_review():
        return automatic_review()

    callback = asynchronous_review if asynchronous else automatic_review
    for candidate in (state, state.model_copy(update={'revision': state.revision + 1}),
                      state.model_copy(update={'continuation_count': state.continuation_count + 1})):
        with pytest.raises(UnknownOperation) as failure:
            await engine.operation(candidate, 'test', {}, perform, reconcile=callback)
        assert failure.value.details['operation_id'] == op_id
        assert failure.value.details['requires_manual_review'] is True
        assert failure.value.details['retryable'] is False
        assert 'store.reconcile' in failure.value.details['recovery']
    assert attempts == []
    assert automatic_reviews == []
    assert record == original
    assert record['status'] == 'STARTED'
    assert len(engine.store.operations) == 1
    assert engine.store.trace(state.run_id, state.scope_id) == events


async def test_display_failure_does_not_interrupt_or_repeat_operation(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    attempts = []

    def broken_display(event):
        raise OSError('closed terminal')

    async def perform():
        attempts.append('called')
        return {'passed': True}

    engine.notify = broken_display
    assert await engine.operation(state, 'test', {}, perform) == {'passed': True}
    assert await engine.operation(state, 'test', {}, perform) == {'passed': True}
    engine.event(state, 'run.error', {'error': 'display-independent'})
    assert attempts == ['called']
    assert [event['type'] for event in engine.store.trace(state.run_id, state.scope_id)] == [
        'tool.started', 'tool.completed', 'run.error']


async def test_sdk_complete_flow_gate_and_artifacts(tmp_path):
    e, state = await verified_sdk_candidate(tmp_path)
    assert state.run_status==RunStatus.RUNNING, state.error
    assert state.outcome==Outcome.FIX_VERIFIED
    assert state.trial==0 and state.reproduced is False and len(state.validation_refs)==6
    count=len(e.browser.calls)
    # Audit replay is read-only.
    trace=e.store.trace(state.run_id,'b')
    assert trace and len(e.browser.calls)==count
    state = await finalize_sdk_candidate(e, state)
    assert state.run_status==RunStatus.COMPLETED
    assert e.artifacts.exists('b',state.run_id,state.report_ref)
    assert e.runner.closed and e.browser.closed


async def test_model_requests_responses_and_reasoning_are_persisted(tmp_path):
    e,s=make_engine(tmp_path,bugfree=True)
    instructions='Use the smallest safe patch.'
    s.agent_instructions_ref=e.artifacts.put('b',s.run_id,instructions,'txt')
    s.agent_instructions_hash=digest(instructions.encode())
    await e.run(s)
    state=e.store.load(s.run_id,'b')
    records=[e.artifacts.json('b',s.run_id,ref) for ref in state.model_exchange_refs]
    assert any('request' in record for record in records)
    responses=[record for record in records if 'response' in record]
    assert responses
    assert responses[0]['response']['body']['choices'][0]['message']['reasoning_content']=='FAKE private reasoning'
    events=e.store.trace(s.run_id,'b')
    assert any(event['type']=='model.request.persisted' for event in events)
    assert any(event['type']=='model.response.persisted' for event in events)


async def test_bugfree_stops_without_patch(tmp_path):
    e,s=make_engine(tmp_path,bugfree=True)
    await e.run(s)
    state=e.store.load(s.run_id,'b')
    assert state.outcome==Outcome.NO_BUG_FOUND, state.error
    assert state.budget.patches==0


async def test_failed_build_never_claims_fix(tmp_path):
    e,s=make_engine(tmp_path,runner_fail='build')
    await e.run(s)
    state=e.store.load(s.run_id,'b')
    assert state.outcome!=Outcome.FIX_VERIFIED
    assert not state.approval_ref


async def test_pause_resume_cancel_at_safe_boundary(tmp_path):
    e,s=make_engine(tmp_path)
    e.control='pause'
    await e.run(s)
    assert e.store.load(s.run_id,'b').run_status==RunStatus.PAUSED
    assert not e.browser.calls
    await e.run(resume='cancel')
    assert e.store.load(s.run_id,'b').run_status==RunStatus.CANCELLED
    assert not e.browser.calls


async def test_worker_role_denies_write_and_depth(tmp_path):
    from tracefix.workers.contracts import WorkerTask
    from tracefix.workers.runtime import SubAgentRuntime

    runtime = SubAgentRuntime()
    try:
        task = WorkerTask(run_id='parent', phase='DIAGNOSE', role='code-reader',
                          goal='Read the authorized source without modifying any files',
                          prompt='Inspect the authorized source and report findings without any writes.', tools=['Read'])
        with pytest.raises(PermissionError):
            runtime.validate(task, parent_run_id='parent', parent_step_id='step', depth=2)
        with pytest.raises(PermissionError):
            runtime.validate(task.model_copy(update={'tools': ['Write']}),
                             parent_run_id='parent', parent_step_id='step')
    finally:
        runtime.close()


def test_decision_evidence_normalizes_current_observation_aliases():
    observation = {'id': 'obs_current', 'screenshot_ref': 'screen.png'}
    assert normalize_decision_evidence_refs(
        ['obs_current', 'screen.png', 'prior.json'], ['prior.json'], 'current.json', observation
    ) == ['current.json', 'prior.json']


def test_decision_evidence_discards_unknown_refs_without_trusting_them():
    assert normalize_decision_evidence_refs(
        ['invented.json', 'obs_current'], [], 'current.json', {'id': 'obs_current'}
    ) == ['current.json']


def test_decision_evidence_deduplicates_aliases():
    assert normalize_decision_evidence_refs(
        ['obs_current', 'screen.png'], [], 'current.json',
        {'id': 'obs_current', 'screenshot_ref': 'screen.png'}
    ) == ['current.json']


def test_repeated_action_detects_consecutive_fingerprints_only():
    assert repeated_action(['first', 'same', 'same'])
    assert not repeated_action(['same', 'other'])
    assert not repeated_action(['same'])


async def test_multi_hop_run_never_trips_optimistic_concurrency(tmp_path):
    e, state = await verified_sdk_candidate(tmp_path)
    events=e.store.trace(state.run_id,'b')
    assert state.run_status==RunStatus.RUNNING, state.error
    assert '版本已过期' not in str(events)
    # 每次 changed() 恰好推进一个版本，说明多跳路径上传入的都是最新状态而非过期副本。
    commits=[x for x in events if x['type']=='state.changed']
    assert [x['revision'] for x in commits]==list(range(1,len(commits)+1))
    assert state.revision==len(commits)
    assert {x['phase'] for x in commits}>={'PREPARE','EXPLORE','DIAGNOSE','PATCH','VERIFY','REVIEW'}
    suites = [event for event in events if event['type'] == 'check.completed']
    assert {event['payload']['stage'] for event in suites} >= {'explore', 'verify'}


async def test_stale_in_memory_state_cannot_overwrite_a_newer_revision(tmp_path):
    e,s=make_engine(tmp_path)
    e.store.save(s)
    fresh=e.changed(s,phase=Phase.EXPLORE)
    assert fresh.revision==s.revision+1
    with pytest.raises(ValueError,match='版本已过期'):
        e.changed(s,step=1)
    assert e.store.load(s.run_id,'b').revision==fresh.revision


async def test_run_persists_state_before_the_first_state_change(tmp_path):
    e,s=make_engine(tmp_path)
    order=[];save,load=e.store.save,e.store.load
    e.store.save=lambda state:(order.append('save'),save(state))[1]
    e.store.load=lambda run_id,scope:(order.append('load'),load(run_id,scope))[1]
    await e.run(s)
    # 首次 changed() 的 load 永远不会撞上「未找到该 Run」。
    assert order[0]=='save' and 'load' in order


async def test_frozen_replay_of_unbindable_locator_is_not_infrastructure_failure(tmp_path):
    e,s=make_engine(tmp_path)
    e.store.save(s)
    s.observation_ref=e.artifacts.put('b',s.run_id,{'id':'obs_seed',
        'snapshot':'- button "Save" [ref=e2]\n- button "Save" [ref=e4]'})
    action=BrowserAction(kind='click',locator=Locator(role='button',name='Save'))
    with pytest.raises(ReplayUnbound):
        await e.act(s,action,frozen=True)
    assert not e.browser.calls


async def test_unreplayable_recorded_plan_requests_new_exploration(tmp_path):
    e,s=make_engine(tmp_path)
    s.observation_ref=e.artifacts.put('b',s.run_id,{'id':'obs_seed','url':'http://app:3000',
        'snapshot':'- heading "Task board" [level=1] [ref=e1]','screenshot_ref':'seed.png'})
    s.replay_plan_ref=e.artifacts.put('b',s.run_id,[BrowserAction(kind='click',
        locator=Locator(role='button',name='已被移除的按钮')).model_dump()])
    s.phase=Phase.REPRODUCE;s.source_aligned=True;s.reproduction_plan_frozen=True
    def pause_after_replay_failure(event):
        if event['type'] == 'run.error' and 'ReplayUnbound' in event['payload'].get('error', ''):
            e.control = 'pause'

    e.notify = pause_after_replay_failure
    await e.run(s)
    state=e.store.load(s.run_id,'b')
    assert state.run_status == RunStatus.PAUSED
    assert state.phase == Phase.EXPLORE
    assert state.replay_plan_ref is None and not state.reproduction_plan_frozen
    assert '无法在当前页面重放' in state.error


def test_stable_snapshot_ignores_diagnostic_counts_and_element_refs():
    before = '- Console: 1 errors, 0 warnings\n- checkbox "Task" [ref=e44]\n- alert [ref=e77]: Endpoint not found'
    after = '- Console: 2 errors, 0 warnings\n- checkbox "Task" [ref=e44]\n- alert [ref=e78]: Endpoint not found'
    assert stable_snapshot(before) == stable_snapshot(after)
    assert stable_snapshot(after) != stable_snapshot(after.replace('checkbox "Task"', 'checkbox "Task" [checked]'))


async def test_model_unknown_operation_is_paused_and_persisted(tmp_path):
    engine, state = make_engine(tmp_path)

    class UnknownModel:
        async def generate(self, schema, context, **kwargs):
            raise ModelError('provider response lost', status='UNKNOWN_OPERATION',
                             category='timeout', details={'billing_status': 'unknown',
                             'requires_manual_review': True})

    engine.model = UnknownModel()
    await engine.run(state)
    saved = engine.store.load(state.run_id, 'b')

    assert saved.run_status == RunStatus.PAUSED
    assert saved.outcome is None
    assert saved.error_details['status'] == 'UNKNOWN_OPERATION'
    assert saved.error_details['category'] == 'timeout'
    assert saved.error_details['requires_manual_review'] is True
    events = engine.store.trace(state.run_id, 'b')
    assert any(event['type'] == 'run.error' and
               event['payload']['error_details']['status'] == 'UNKNOWN_OPERATION'
               for event in events)


async def test_sdk_repair_never_requests_host_patch_proposals(tmp_path):
    from tracefix.runtime.smoke import FakeModel

    class ReadOnlyHostModel(FakeModel):
        async def generate(self, schema, context, **options):
            assert schema is not PatchProposal
            return await super().generate(schema, context, **options)

    def configure(engine, state):
        engine.model = ReadOnlyHostModel(engine.workspace)

    engine, state = await verified_sdk_candidate(tmp_path, configure=configure)
    assert state.outcome == Outcome.FIX_VERIFIED
    assert engine.verification_passed(state)


def test_noop_patch_is_rejected_as_model_output_error(tmp_path):
    engine, _ = make_engine(tmp_path)
    content = engine.workspace.read('src/value.ts')
    patch = PatchProposal(summary='no-op', evidence_refs=['evidence.json'], edits=[
        FileEdit(path='src/value.ts', before_hash=digest(content.encode()), content=content)
    ])

    with pytest.raises(ValueError, match='无实际改动'):
        engine.workspace.apply(patch)


async def test_engine_keeps_retry_attempts_in_one_logical_exchange(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)

    class MultiAttemptModel:
        async def generate(self, schema, context, **kwargs):
            first = kwargs['on_attempt']('FAKE', {'logical_exchange_id': 'logical-test', 'json': {}}, 1)
            second = kwargs['on_attempt']('FAKE', {'logical_exchange_id': 'logical-test', 'json': {}}, 2)
            usage = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
            kwargs['on_usage'](usage)
            response = {'http_status': 200, 'headers': {}, 'body': {
                'usage': usage, 'choices': [{'finish_reason': 'stop',
                'message': {'content': '{"action":{"kind":"finish"}}'}}]}}
            kwargs['on_response'](first, response)
            kwargs['on_response'](second, response)
            return ModelResult(Decision(action=BrowserAction(kind='finish')), usage, 'FAKE', 'stop')

    engine.model = MultiAttemptModel()
    await engine.model_call(state, Decision, {})
    records = [engine.artifacts.json('b', state.run_id, ref)
               for ref in state.model_exchange_refs]
    assert records
    assert {record['exchange_id'] for record in records} == {'logical-test'}


async def test_unsent_model_network_error_can_resume_without_completing_silently(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)

    class RetryModel:
        def __init__(self, workspace):
            from tracefix.runtime.smoke import FakeModel
            self.inner = FakeModel(workspace)
            self.calls = 0

        async def generate(self, schema, context, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ModelError('connection was not established', status='WAITING_NETWORK',
                                 category='network', details={'request_status': 'not_sent',
                                 'requires_manual_review': False})
            return await self.inner.generate(schema, context, **kwargs)

    model = RetryModel(engine.workspace)
    engine.model = model
    await engine.run(state)
    paused = engine.store.load(state.run_id, 'b')
    assert paused.run_status == RunStatus.PAUSED
    assert paused.error_details['status'] == 'WAITING_NETWORK'

    await engine.run(resume='resume')
    resumed = engine.store.load(state.run_id, 'b')
    assert model.calls >= 2
    assert resumed.run_status == RunStatus.COMPLETED
    assert resumed.outcome == Outcome.NO_BUG_FOUND


async def test_unknown_operation_pause_can_be_cancelled_without_resume_replay(tmp_path):
    engine, state = make_engine(tmp_path)

    class UnknownModel:
        calls = 0

        async def generate(self, schema, context, **kwargs):
            self.calls += 1
            raise ModelError('provider response lost', status='UNKNOWN_OPERATION',
                             category='timeout', details={'requires_manual_review': True})

    engine.model = UnknownModel()
    await engine.run(state)
    assert engine.store.load(state.run_id, 'b').run_status == RunStatus.PAUSED

    await engine.run(resume='resume')
    assert engine.store.load(state.run_id, 'b').run_status == RunStatus.PAUSED
    assert engine.model.calls == 1

    await engine.run(resume='cancel')
    cancelled = engine.store.load(state.run_id, 'b')
    assert cancelled.run_status == RunStatus.CANCELLED
    assert cancelled.error_details is None


@pytest.mark.parametrize('unknown_action', [False, True])
async def test_browser_network_error_pauses_without_replay_and_retains_intent(tmp_path, unknown_action):
    from tracefix.execution.browser import MCPActionUnknown, MCPConnectionError

    engine, state = make_engine(tmp_path)
    attempts = []
    failure = MCPConnectionError('browser connection lost')
    if unknown_action:
        failure = MCPActionUnknown('action-test', 'navigate', failure)

    async def action(browser_action):
        attempts.append(browser_action)
        raise failure

    engine.browser.action = action
    await engine.run(state)
    paused = engine.store.load(state.run_id, 'b')
    assert paused.run_status == RunStatus.PAUSED
    assert paused.error_details['status'] == failure.status
    assert paused.error_details['source'] == 'browser'
    assert paused.error_details['requires_manual_review'] is True
    events = engine.store.trace(state.run_id, 'b')
    failed = next(event for event in events if event['type'] == 'tool.error')
    op_id = failed['payload']['operation_id']
    assert engine.store.operations[op_id]['status'] == 'UNKNOWN'
    unknown = [event for event in events if event['type'] == 'tool.unknown'
               and event['payload']['operation_id'] == op_id]
    assert len(unknown) == 1
    assert unknown[0]['payload']['reason'] == type(failure).__name__
    assert unknown[0]['seq'] < failed['seq']

    await engine.run(resume='resume')
    assert engine.store.load(state.run_id, 'b').run_status == RunStatus.PAUSED
    assert len(attempts) == 1
    await engine.run(resume='cancel')
    assert engine.store.load(state.run_id, 'b').run_status == RunStatus.CANCELLED
    assert engine.store.operations[op_id]['status'] == 'UNKNOWN'
    with pytest.raises(UnknownOperation) as blocked:
        await engine.operation(paused.model_copy(update={'revision': paused.revision + 1}),
                               'browser.action', {'sequence': 'new'}, lambda: action(attempts[0]))
    assert blocked.value.details['operation_id'] == op_id
    assert len(attempts) == 1
