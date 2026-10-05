import pytest

from tracefix.runtime.continuation import continuation_state, derive_run
from tracefix.runtime.recovery import (
    RecoveryCause,
    RecoveryController,
    RecoveryAction,
    classify_cause,
    has_real_progress,
)
from tracefix.runtime.contracts import Outcome, RunStatus
from tracefix.runtime.smoke import make_engine


def test_recovery_classifies_terminal_boundaries_before_heuristics():
    assert classify_cause(error_details={'status': 'UNKNOWN_OPERATION'}) is RecoveryCause.UNKNOWN
    assert classify_cause(error_details={'status': 'CANCELLED'}) is RecoveryCause.CANCELLED
    assert classify_cause(child_status='running') is RecoveryCause.WAIT
    assert classify_cause(signals=[{'kind': 'state_repeated'}]) is RecoveryCause.REPEATED_NO_PROGRESS


def test_real_progress_ignores_technical_churn():
    before = {'phase': 'PATCH', 'ref': 'old', 'hash': 'a', 'heartbeat': 1, 'business': 'pending'}
    after = {'phase': 'VERIFY', 'ref': 'new', 'hash': 'b', 'heartbeat': 2, 'business': 'pending'}
    assert not has_real_progress(before, after)
    assert has_real_progress(before, {**after, 'business': 'passed'})
    assert has_real_progress({}, {}, {'verification_advanced': True})


def test_episode_budget_is_bound_to_same_semantic_episode():
    controller = RecoveryController()
    first = controller.decide(RecoveryCause.STALE, episode_id='same')
    history = controller.record([], first, status='continued')
    exhausted = controller.decide(RecoveryCause.STALE, history, episode_id='same')
    fresh = controller.decide(RecoveryCause.STALE, history, episode_id='different')
    assert first.can_continue and first.action is RecoveryAction.REFRESH_OBSERVATION
    assert not exhausted.can_continue and exhausted.action is RecoveryAction.STOP
    assert fresh.can_continue


def test_cancel_and_unknown_are_never_revival_actions():
    controller = RecoveryController()
    for cause in (RecoveryCause.CANCELLED, RecoveryCause.UNKNOWN):
        decision = controller.decide(cause, episode_id='same')
        assert not decision.can_continue and decision.action is RecoveryAction.STOP


def test_manual_continuation_preserves_episode_budget_but_new_run_does_not(tmp_path):
    engine, previous = make_engine(tmp_path)
    previous.run_status = RunStatus.ABNORMAL
    previous.outcome = Outcome.LOOP_DETECTED
    previous.recovery_episodes = [{'cause': 'stale', 'episode_id': 'same', 'attempt': 1}]
    continued = continuation_state(previous, '人工继续核对', [])
    derived = derive_run(previous)
    assert continued.recovery_episodes == previous.recovery_episodes
    assert derived.recovery_episodes == []


@pytest.mark.asyncio
async def test_wait_recovery_reports_unavailable_without_child_interface(tmp_path):
    engine, state = make_engine(tmp_path)
    state.pending_action = {'kind': 'child', 'child_status': 'running'}
    decision = engine.recovery.decide(RecoveryCause.WAIT, episode_id='wait')
    available, evidence = await engine._recovery_action(state, decision)
    assert not available
    assert evidence['status'] == 'unavailable'
    assert evidence['interface'] == 'child_wait'

