import copy

import pytest

from test_architecture_validation_repairs import bundle as validation_bundle, gate
from tracefix.rules.models import RuleRef, RuleSnapshot
from tracefix.runtime.contracts import CheckItem, CheckPlan, CheckResult, digest


@pytest.fixture
def check_bundle(validation_bundle):
    state, _, artifacts = validation_bundle
    state.mode = 'repair'
    state.reproduced = False
    snapshot = RuleSnapshot(run_id=state.run_id, refs=[RuleRef(id='accessible', version=2)])
    state.rule_snapshot_ref = 'rules.json'
    state.rule_snapshot_hash = snapshot.hash
    state.rule_refs = [reference.model_dump(mode='json') for reference in snapshot.refs]
    artifacts[state.rule_snapshot_ref] = snapshot.model_dump(mode='json')
    plan = CheckPlan(run_id=state.run_id, source_manifest=state.source_manifest,
        test_spec_hash=state.test_spec_hash, rule_snapshot_hash=snapshot.hash, items=[
            CheckItem(id='accessible', name='可访问名称', criteria='控件必须具备可访问名称',
                      source='rule', detector='static', severity='major', rule_version=2),
            CheckItem(id='goal-task', name='任务完成状态', criteria='任务标记后保持完成状态',
                      source='user_goal', detector='dom', severity='blocker')])
    state.check_plan_ref = 'checks.json'
    state.check_plan_hash = digest(plan)
    artifacts[state.check_plan_ref] = plan.model_dump(mode='json')
    state.check_suite_completed = True
    state.check_suite_stage = 'verify'
    state.check_suite_patch_hash = state.patch_hash
    initial_observation = copy.deepcopy(artifacts['observation.json'])
    initial_observation['patch_hash'] = None
    initial_observation['snapshot'] = '- checkbox "Task" [ref=e1]'
    artifacts['initial-observation.json'] = initial_observation
    for stage, patch_hash, target, observation_ref in [
            ('explore', None, state.initial_check_result_refs, 'initial-observation.json'),
            ('verify', state.patch_hash, state.check_result_refs, 'observation.json')]:
        for item in plan.items:
            result = CheckResult(**item.model_dump(include={
                'id', 'name', 'criteria', 'severity', 'source', 'detector'}),
                status='fail' if stage == 'explore' else 'pass', actual='检测到未完成任务' if
                    stage == 'explore' else '检查标准全部满足',
                evidence_refs=[observation_ref], started_at=1, finished_at=2,
                check_plan_hash=state.check_plan_hash, source_manifest=state.source_manifest,
                patch_hash=patch_hash, stage=stage)
            ref = f'{stage}-{item.id}.json'
            artifacts[ref] = result.model_dump(mode='json')
            target.append(ref)
    return validation_bundle


@pytest.mark.parametrize('reproduced', [False, True])
def test_complete_check_evidence_can_replace_legacy_reproduction(check_bundle, reproduced):
    check_bundle[0].reproduced = reproduced
    assert gate(check_bundle)


def test_legacy_run_still_requires_reproduction(validation_bundle):
    assert gate(validation_bundle)
    validation_bundle[0].reproduced = False
    assert not gate(validation_bundle)


def test_nonblocking_findings_do_not_reject_an_otherwise_verified_fix(check_bundle):
    check_bundle[2]['verify-accessible.json']['status'] = 'fail'
    assert gate(check_bundle)


@pytest.mark.parametrize('field', ['run_id', 'source_manifest', 'test_spec_hash', 'rule_snapshot_hash'])
def test_frozen_check_plan_binding_cannot_be_rebound(check_bundle, field):
    state, _, artifacts = check_bundle
    plan = artifacts[state.check_plan_ref]
    plan[field] = 'other'
    state.check_plan_hash = digest(plan)
    for ref in state.initial_check_result_refs + state.check_result_refs:
        artifacts[ref]['check_plan_hash'] = state.check_plan_hash
    assert not gate(check_bundle)


@pytest.mark.parametrize('stage', ['explore', 'verify'])
@pytest.mark.parametrize('field,value', [
    ('check_plan_hash', 'other'), ('source_manifest', 'other'), ('patch_hash', 'other'),
    ('stage', 'other'), ('id', 'unknown'), ('name', 'other'), ('criteria', 'other'),
    ('severity', 'minor'), ('source', 'rule'), ('detector', 'guided'),
    ('actual', ''), ('started_at', float('nan')), ('finished_at', 0),
    ('evidence_refs', []), ('evidence_refs', ['../outside.json']), ('error', 'tool failure')])
def test_check_results_require_exact_identity_binding_and_credible_evidence(check_bundle, stage, field, value):
    check_bundle[2][f'{stage}-goal-task.json'][field] = value
    assert not gate(check_bundle)


@pytest.mark.parametrize('stage', ['explore', 'verify'])
@pytest.mark.parametrize('fault', ['missing', 'duplicate-ref', 'duplicate-item', 'unknown-item'])
def test_every_initial_and_final_check_must_be_present_once(check_bundle, stage, fault):
    state, _, artifacts = check_bundle
    refs = state.initial_check_result_refs if stage == 'explore' else state.check_result_refs
    if fault == 'missing':
        refs.pop()
    elif fault == 'duplicate-ref':
        refs[1] = refs[0]
    elif fault == 'duplicate-item':
        artifacts[refs[1]] = copy.deepcopy(artifacts[refs[0]])
    else:
        artifacts[refs[1]]['id'] = 'unknown'
    assert not gate(check_bundle)


@pytest.mark.parametrize('reproduced', [False, True])
def test_initial_suite_requires_failure_even_when_reproduction_flag_is_true(check_bundle, reproduced):
    state, _, artifacts = check_bundle
    state.reproduced = reproduced
    for ref in state.initial_check_result_refs:
        artifacts[ref]['status'] = 'pass'
    assert not gate(check_bundle)


@pytest.mark.parametrize('status', ['fail', 'error', 'inconclusive'])
def test_blocking_check_must_pass_after_repair(check_bundle, status):
    check_bundle[2]['verify-goal-task.json']['status'] = status
    assert not gate(check_bundle)


@pytest.mark.parametrize('status', ['error', 'inconclusive'])
def test_incomplete_nonblocking_checks_cannot_certify_a_fix(check_bundle, status):
    check_bundle[2]['verify-accessible.json']['status'] = status
    assert not gate(check_bundle)


@pytest.mark.parametrize('field,value', [
    ('check_suite_completed', False), ('check_suite_stage', 'explore'),
    ('check_suite_patch_hash', 'other'), ('check_plan_hash', 'other'),
    ('source_aligned', False), ('patch_hash', None), ('environment_digest', 'other'),
    ('reproduction_plan_frozen', False)])
def test_unified_checks_do_not_bypass_other_verification_gates(check_bundle, field, value):
    setattr(check_bundle[0], field, value)
    assert not gate(check_bundle)


@pytest.mark.parametrize('kind', ['static', 'unit', 'build', 'health', 'original', 'regression', 'behavior'])
def test_unified_checks_do_not_replace_required_command_and_browser_results(check_bundle, kind):
    check_bundle[2][kind + '.json']['passed'] = False
    assert not gate(check_bundle)


@pytest.mark.parametrize('ref', ['checks.json', 'rules.json', 'explore-goal-task.json',
    'verify-accessible.json', 'initial-observation.json', 'observation.json', 'screenshot.png'])
def test_all_check_artifact_references_must_be_readable(check_bundle, ref):
    check_bundle[2].pop(ref)
    assert not gate(check_bundle)


@pytest.mark.parametrize('fault', ['snapshot-run', 'snapshot-hash', 'snapshot-refs', 'rule-version',
    'missing-snapshot', 'missing-rule', 'no-user-goal', 'duplicate-plan-item'])
def test_rule_snapshot_and_complete_plan_are_required(check_bundle, fault):
    state, _, artifacts = check_bundle
    plan = artifacts[state.check_plan_ref]
    snapshot = artifacts[state.rule_snapshot_ref]
    if fault == 'snapshot-run':
        snapshot['run_id'] = 'other'
    elif fault == 'snapshot-hash':
        snapshot['hash'] = 'other'
    elif fault == 'snapshot-refs':
        state.rule_refs = []
    elif fault == 'rule-version':
        plan['items'][0]['rule_version'] = 1
    elif fault == 'missing-snapshot':
        state.rule_snapshot_ref = None
    elif fault == 'missing-rule':
        plan['items'] = plan['items'][1:]
    elif fault == 'no-user-goal':
        plan['items'] = plan['items'][:1]
    else:
        plan['items'].append(copy.deepcopy(plan['items'][0]))
    state.check_plan_hash = digest(plan)
    for ref in state.initial_check_result_refs + state.check_result_refs:
        artifacts[ref]['check_plan_hash'] = state.check_plan_hash
    assert not gate(check_bundle)


@pytest.mark.parametrize('fault', ['foreign-run', 'stale-patch', 'missing-png-hash',
    'invalid-png', 'missing-nested-evidence'])
def test_initial_failure_evidence_has_a_complete_current_run_closure(check_bundle, fault):
    artifacts = check_bundle[2]
    observation = artifacts['initial-observation.json']
    if fault == 'foreign-run':
        observation['run_id'] = 'other'
    elif fault == 'stale-patch':
        observation['patch_hash'] = 'other'
    elif fault == 'missing-png-hash':
        observation.pop('screenshot_hash')
    elif fault == 'invalid-png':
        artifacts['screenshot.png'] = b'not-png'
        observation['screenshot_hash'] = digest(artifacts['screenshot.png'])
    else:
        observation['evidence_refs'] = ['missing.json']
    assert not gate(check_bundle)


def test_goal_only_plan_is_supported_without_a_rule_library(check_bundle):
    state, _, artifacts = check_bundle
    plan = artifacts[state.check_plan_ref]
    plan['items'] = plan['items'][1:]
    plan['rule_snapshot_hash'] = ''
    state.rule_snapshot_ref = None
    state.rule_snapshot_hash = ''
    state.rule_refs = []
    state.check_plan_hash = digest(plan)
    state.initial_check_result_refs = state.initial_check_result_refs[1:]
    state.check_result_refs = state.check_result_refs[1:]
    for ref in state.initial_check_result_refs + state.check_result_refs:
        artifacts[ref]['check_plan_hash'] = state.check_plan_hash
    assert gate(check_bundle)
