from __future__ import annotations

import json
from fnmatch import fnmatchcase

from tracefix.runtime.contracts import CheckItem, CheckPlan, CheckResult, GoalCheckDraft, digest


def make_check_plan(state, rules, goal: GoalCheckDraft | None, *, extraction_error=None):
    items = [CheckItem(id=rule.id, name=rule.name,
        criteria=json.dumps(rule.detection.model_dump(mode='json'), ensure_ascii=False),
        severity=rule.severity, source='rule', detector=rule.detection.type,
        detection=rule.model_dump(mode='json'), rule_version=rule.version) for rule in rules]
    goal_id = 'goal-' + digest([state.run_id, state.goal])[:24]
    if any(item.id == goal_id for item in items):
        goal_id += '-user'
    items.append(CheckItem(id=goal_id, name=goal.name if goal else '用户目标检查（提炼失败）',
        criteria=goal.criteria if goal else state.goal, source='user_goal', detector='dom',
        severity='blocker', extraction_error=extraction_error))
    return CheckPlan(run_id=state.run_id, source_manifest=state.source_manifest,
        test_spec_hash=state.test_spec_hash, rule_snapshot_hash=state.rule_snapshot_hash, items=items)


def summarize_checks(plan: CheckPlan, results: list[CheckResult]):
    by_id = {result.id: result for result in results}
    missing = [item.id for item in plan.items if item.id not in by_id]
    counts = {status: sum(result.status == status for result in results)
              for status in ('pass', 'fail', 'error', 'inconclusive')}
    blocker_failed = sum(item.severity == 'blocker' and (
        item.id not in by_id or by_id[item.id].status != 'pass') for item in plan.items)
    overall = ('FAILED' if blocker_failed else 'INCONCLUSIVE' if missing
        or counts['error'] or counts['inconclusive'] else 'PASSED_WITH_FINDINGS'
        if counts['fail'] else 'PASSED')
    return overall, {'total': len(plan.items), 'passed': counts['pass'], 'failed': counts['fail'],
        'error': counts['error'], 'inconclusive': counts['inconclusive'],
        'blocker_failed': blocker_failed, 'coverage_complete': not missing, 'missing_ids': missing}


def scope_matches(rule, observation, paths):
    from urllib.parse import urlsplit

    patterns = rule.scope.url_patterns
    url = observation.get('url', '')
    if patterns and not any(fnmatchcase(url, pattern) or
                            fnmatchcase(urlsplit(url).path or '/', pattern) for pattern in patterns):
        return False
    return not rule.scope.path_globs or any(fnmatchcase(path, pattern)
        for path in paths for pattern in rule.scope.path_globs)


def oracle_ready(rule, observation, action=None):
    config = rule.detection.oracle or {}
    kind = config.get('kind')
    if kind in {'a11y_axe', 'visual_threshold'}:
        return isinstance(observation.get('oracle_failures'), dict) and kind in observation['oracle_failures']
    channel = {'console_no_error': 'console', 'network_status': 'network',
               'dom_assertion': 'snapshot', 'dom_after_action': 'snapshot'}.get(kind)
    if channel is None or channel not in observation:
        return False
    metadata = observation.get('collection', {}).get('channels', {}).get(channel)
    status = metadata.get('status') if isinstance(metadata, dict) else metadata
    if status and status != 'available':
        return False
    if kind == 'dom_after_action':
        trigger = config.get('trigger') or {}
        if not action or trigger.get('action') and action.get('kind') != trigger['action']:
            return False
        locator = trigger.get('locator') or {}
        actual = action.get('locator') or {}
        if locator.get('role') and actual.get('role') != locator['role']:
            return False
        if locator.get('name_regex'):
            import re
            if not re.search(locator['name_regex'], actual.get('name', '')):
                return False
        if not config.get('expect_any'):
            return False
    return True
