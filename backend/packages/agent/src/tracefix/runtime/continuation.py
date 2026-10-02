from tracefix.runtime.contracts import Phase, RunState, RunStatus, new_id


def can_continue(status, outcome=None):
    status = str(status).lower()
    return status in {'abnormal', 'failed', 'cancelled', 'completed'} and not (
        status == 'completed' and outcome in {'FIX_VERIFIED', 'NO_BUG_FOUND'})


def continuation_state(previous, instruction, markers, *, process_ended=False):
    if not can_continue(previous.run_status, previous.outcome) and not process_ended:
        raise ValueError('只有非成功结束的任务可以继续')
    keep_patch = bool(previous.patch_hash and previous.reproduced and previous.replay_plan_ref)
    return RunState.model_validate({**previous.model_dump(),
        'phase': Phase.PREPARE, 'run_status': RunStatus.RUNNING, 'revision': previous.revision + 1,
        'outcome': None, 'error': None, 'report_ref': None, 'pending_action': None, 'approval_ref': None,
        'error_details': None, 'diagnosis_retry_count': 0, 'diagnosis_feedback_refs': [],
        'step': 0, 'trial': 0, 'replay_index': 0, 'validation_index': 0,
        'validation_refs': [], 'baseline_validation_refs': [], 'observation_ref': None,
        'failure_signatures': [], 'action_fingerprints': [], 'local_branch': None,
        'replay_plan_ref': previous.replay_plan_ref if keep_patch else None,
        'exploration_plan_ref': previous.exploration_plan_ref if keep_patch else None,
        'reproduction_plan_frozen': previous.reproduction_plan_frozen if keep_patch else False,
        'reproduced': previous.reproduced if keep_patch else False,
        'source_aligned': False, 'environment_digest': '',
        'budget': previous.budget.model_copy(deep=True),
        'continuation_instruction': instruction, 'continuation_count': len(markers),
        'abnormal_termination': True, 'continuation_markers': markers})


def derive_run(previous: RunState, *, goal: str | None = None, url: str | None = None,
               mode: str | None = None, additional_rule_ids: list[str] | None = None,
               run_id: str | None = None) -> RunState:
    """Create a child Run while carrying the parent's frozen rule set.

    A derived Run gets a new identity and keeps the parent's ``rule_refs``;
    ``additional_rule_ids`` are resolved and appended when its first snapshot
    is frozen. Existing TestSpec, patch, and browser evidence are intentionally
    not copied across the new execution boundary.
    """
    return RunState.model_validate({
        **previous.model_dump(),
        'run_id': run_id or new_id('run'),
        'parent_run_id': previous.run_id,
        'goal': goal or previous.goal,
        'url': url or previous.url,
        'mode': mode or previous.mode,
        'phase': Phase.PREPARE,
        'run_status': RunStatus.RUNNING,
        'revision': 0,
        'outcome': None,
        'error': None,
        'error_details': None,
        'diagnosis_retry_count': 0,
        'diagnosis_feedback_refs': [],
        'report_ref': None,
        'test_spec_ref': None,
        'test_spec_hash': '',
        'replay_plan_ref': None,
        'exploration_plan_ref': None,
        'reproduction_plan_frozen': False,
        'skills_loaded': [],
        'observation_ref': None,
        'evidence_refs': [],
        'hypothesis_refs': [],
        'working_set_refs': [],
        'patch_ref': None,
        'patch_hash': None,
        'validation_refs': [],
        'baseline_validation_refs': [],
        'pending_action': None,
        'approval_ref': None,
        'step': 0,
        'trial': 0,
        'replay_index': 0,
        'validation_index': 0,
        'failure_signatures': [],
        'action_fingerprints': [],
        'loop_state_fingerprints': [],
        'loop_error_signatures': [],
        'loop_no_progress_steps': 0,
        'loop_warnings': [],
        'loop_evidence': None,
        'last_error_signature': None,
        'local_branch': None,
        'reproduced': False,
        'source_aligned': False,
        'environment_digest': '',
        'rule_snapshot_ref': None,
        'rule_snapshot_hash': '',
        'additional_rule_ids': list(dict.fromkeys(additional_rule_ids or [])),
        # 子 Run 从空引导队列开始；父 Run 的约束仍随状态副本继承。
        'guidance': [],
        'superseded_by_run_id': None,
        'inherited_evidence_refs': [],
    })
