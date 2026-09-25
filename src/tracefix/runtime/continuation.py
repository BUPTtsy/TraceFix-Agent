import time

from tracefix.runtime.contracts import Budget, Phase, RunState, RunStatus


def can_continue(status, outcome=None):
    status = str(status).lower()
    return status in {'failed', 'cancelled', 'completed'} and not (
        status == 'completed' and outcome in {'FIX_VERIFIED', 'NO_BUG_FOUND'})


def continuation_state(previous, instruction, markers, *, process_ended=False):
    if not can_continue(previous.run_status, previous.outcome) and not process_ended:
        raise ValueError('只有非成功结束的任务可以继续')
    allocation = markers[0].get('budget_allocation') or {
        name: value for name, value in previous.budget.model_dump().items() if name.startswith('max_')}
    budget = previous.budget.model_dump()
    for name, value in allocation.items():
        budget[name] = budget[name.removeprefix('max_')] + value
    budget.update(deadline=time.time() + 1800, last_progress=time.time())
    keep_patch = bool(previous.patch_hash and previous.reproduced and previous.replay_plan_ref)
    return RunState.model_validate({**previous.model_dump(),
        'phase': Phase.PREPARE, 'run_status': RunStatus.RUNNING, 'revision': previous.revision + 1,
        'outcome': None, 'error': None, 'report_ref': None, 'pending_action': None, 'approval_ref': None,
        'step': 0, 'trial': 0, 'replay_index': 0, 'validation_index': 0,
        'validation_refs': [], 'baseline_validation_refs': [], 'observation_ref': None,
        'failure_signatures': [], 'action_fingerprints': [], 'local_branch': None,
        'replay_plan_ref': previous.replay_plan_ref if keep_patch else None,
        'reproduced': previous.reproduced if keep_patch else False,
        'source_aligned': False, 'environment_digest': '', 'budget': Budget(**budget),
        'continuation_instruction': instruction, 'continuation_count': len(markers),
        'abnormal_termination': True, 'continuation_markers': markers})
