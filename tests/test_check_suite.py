from types import SimpleNamespace

from tracefix.rules.models import Rule, RuleDetection
from tracefix.runtime.checks import make_check_plan, summarize_checks
from tracefix.runtime.contracts import CheckResult, GoalCheckDraft, digest


def test_check_plan_keeps_rules_and_user_goal_together():
    state = SimpleNamespace(run_id='run-1', source_manifest='source', test_spec_hash='spec',
                            rule_snapshot_hash='rules', goal='保存后刷新仍保持完成')
    rule = Rule(id='rule-save', name='禁止 console 错误', severity='critical',
                status='enabled', detection=RuleDetection(type='oracle',
                    oracle={'kind': 'console_no_error'}))
    plan = make_check_plan(state, [rule], GoalCheckDraft(name='刷新保持完成',
        criteria='完成任务后刷新，复选框仍为 checked'))
    assert [item.id for item in plan.items] == ['rule-save', 'goal-' + digest(['run-1', state.goal])[:24]]
    assert plan.items[0].source == 'rule'
    assert plan.items[1].source == 'user_goal'


def test_blocker_failure_determines_overall_but_all_items_are_counted():
    plan = SimpleNamespace(items=[
        SimpleNamespace(id='blocker', severity='blocker'),
        SimpleNamespace(id='minor', severity='minor'),
    ])
    results = [
        CheckResult(id='blocker', name='阻断', criteria='x', severity='blocker', source='rule',
                    detector='oracle', status='fail', actual='不满足', started_at=0, finished_at=1,
                    check_plan_hash='p', source_manifest='s'),
        CheckResult(id='minor', name='辅助', criteria='x', severity='minor', source='rule',
                    detector='static', status='pass', actual='通过', started_at=0, finished_at=1,
                    check_plan_hash='p', source_manifest='s'),
    ]
    overall, summary = summarize_checks(plan, results)
    assert overall == 'FAILED'
    assert summary['total'] == 2
    assert summary['passed'] == 1 and summary['failed'] == 1
    assert summary['coverage_complete'] is True

