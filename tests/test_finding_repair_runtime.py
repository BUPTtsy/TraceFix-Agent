import pytest

from tracefix.model.gateway import ModelOutputError, ModelResult
from tracefix.rules import Rule, RuleResolver
from tracefix.runtime.contracts import BrowserAction, Decision, GUIIssue, Phase, digest
from tracefix.runtime.smoke import make_engine


def runtime_rule(rule_id='feedback', *, detector='guided', **fields):
    detection = ({'type': 'guided', 'guided': {'prompt': '操作后检查完整反馈'}}
        if detector == 'guided' else {'type': 'static', 'static': {'regex': r'persisted = false'}})
    return Rule(id=rule_id, name='操作结果应显示反馈', status='enabled',
        phases=['EXPLORE', 'DIAGNOSE', 'PATCH', 'VERIFY'], detection=detection,
        fix_guidance='在真实成功和失败分支显示可访问提示', tags=['user-rule'], **fields)


async def rule_engine(tmp_path, *rules):
    engine, state = make_engine(tmp_path)
    engine.rule_resolver = RuleResolver(rules)
    state.rule_ids = [rule.id for rule in rules]
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    state.phase = Phase.EXPLORE
    state.observation_ref = await engine.capture(state, await engine.browser.action(BrowserAction(kind='observe')))
    state.evidence_refs = [state.observation_ref]
    engine.store.save(state)
    return engine, state


async def test_gui_issue_keeps_its_own_rule_source_and_free_issue_stays_free(tmp_path):
    rule = runtime_rule()
    engine, state = await rule_engine(tmp_path, rule, runtime_rule('second'))
    observation = engine.get(state, state.observation_ref)
    decision = Decision(action=BrowserAction(kind='finish'), rule_refs=[rule.id, 'second'], issues=[
        GUIIssue(title='来源明确的问题', expected='显示操作反馈', actual='没有操作反馈',
            evidence_refs=[state.observation_ref], rule_refs=[rule.id]),
        GUIIssue(title='自由发现的问题', expected='显示空状态', actual='页面空白',
            evidence_refs=[state.observation_ref])])
    engine.record_gui_issues(state, decision, observation)
    findings = [event['payload']['finding'] for event in engine.store.trace(state.run_id, state.scope_id)
                if event['type'] == 'finding.created']
    assert [(item['rule_id'], item['rule_version']) for item in findings] == [(rule.id, rule.version), (None, None)]
    assert all(item['scope_id'] == state.scope_id and item['project_id'] == state.scope_id
               and item['repo_id'] == 'ci' and item['run_id'] == state.run_id for item in findings)
    assert findings[0]['expected'] == '显示操作反馈' and findings[0]['actual'] == '没有操作反馈'
    assert all(item['status'] == 'suspected' and item['evidence_refs'] == [state.observation_ref]
               for item in findings)


async def test_gui_issue_forged_rule_rejected_before_any_finding_saved(tmp_path):
    engine, state = await rule_engine(tmp_path, runtime_rule())
    issue = GUIIssue(title='伪造来源', expected='有反馈', actual='无反馈',
                     evidence_refs=[state.observation_ref], rule_refs=['forged'])
    with pytest.raises(ValueError, match='来源规则'):
        engine.record_gui_issues(state, Decision(action=BrowserAction(kind='finish'), issues=[issue]),
                                 engine.get(state, state.observation_ref))
    assert not any(event['type'] == 'finding.created' for event in engine.store.trace(state.run_id, state.scope_id))


async def test_model_call_rejects_issue_rule_outside_current_injection(tmp_path):
    engine, state = await rule_engine(tmp_path, runtime_rule())

    class InvalidIssueModel:
        async def generate(self, schema, context, **options):
            value = Decision(action=BrowserAction(kind='finish'), issues=[GUIIssue(title='伪造来源',
                expected='有反馈', actual='无反馈', evidence_refs=[state.observation_ref], rule_refs=['forged'])])
            return ModelResult(value, {}, 'fixture', 'stop')

    engine.model = InvalidIssueModel()
    with pytest.raises(ModelOutputError, match='GUIIssue'):
        await engine.model_call(state, Decision, {'observation': engine.get(state, state.observation_ref)})


async def test_guided_model_pass_is_not_deterministic_rule_coverage(tmp_path):
    engine, state = await rule_engine(tmp_path, runtime_rule())
    state = await engine.ensure_check_plan(state)
    state.check_suite_stage = 'explore'
    item = next(item for item in engine.check_plan(state).items if item.source == 'rule')

    async def model_pass(*args, **kwargs):
        return 'pass', '模型宣称已通过', [state.observation_ref]

    engine.model_check = model_pass
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'inconclusive'
    assert '缺少可执行验收' in result.actual
    assert not engine.rules_fully_checked(state, Phase.EXPLORE)


async def test_static_detector_failure_cannot_pass_through_model_fallback(tmp_path):
    rule = runtime_rule(detector='static')
    engine, state = await rule_engine(tmp_path, rule)
    state = await engine.ensure_check_plan(state)
    state.check_suite_stage = 'explore'
    item = next(item for item in engine.check_plan(state).items if item.source == 'rule')

    def unavailable_source(path):
        raise OSError('source adapter unavailable')

    async def model_pass(*args, **kwargs):
        return 'pass', '模型宣称已通过', [state.observation_ref]

    engine.workspace.read = unavailable_source
    engine.model_check = model_pass
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'inconclusive' and result.error
    assert result.fallback['status'] == 'inconclusive'


async def test_static_findings_include_source_evidence_and_frozen_rule(tmp_path):
    rule = runtime_rule(detector='static')
    engine, state = await rule_engine(tmp_path, rule)
    state = await engine.ensure_check_plan(state)
    state.check_suite_stage = 'explore'
    item = next(item for item in engine.check_plan(state).items if item.source == 'rule')
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'fail'
    finding = next(event['payload']['finding'] for event in engine.store.trace(state.run_id, state.scope_id)
                   if event['type'] == 'finding.created')
    assert finding['rule_id'] == rule.id and finding['rule_version'] == rule.version
    assert finding['scope_id'] == state.scope_id and finding['repo_id'] == 'ci'
    assert finding['source_base_commit'] == engine.source['commit']
    assert finding['source_manifest'] == state.source_manifest
    assert finding['evidence_refs'] and all(engine.bundle_exists(state, ref) for ref in finding['evidence_refs'])


async def test_selected_repair_rule_requires_patch_and_verify_scope(tmp_path):
    rule = runtime_rule(scope={'path_globs': ['src/other.ts']})
    engine, state = await rule_engine(tmp_path, rule)
    with pytest.raises(ValueError, match='覆盖不足'):
        engine.require_repair_rule_coverage(state)


async def test_hash_corruption_never_falls_back_to_rule_refs(tmp_path):
    engine, state = await rule_engine(tmp_path, runtime_rule())
    snapshot = engine.get(state, state.rule_snapshot_ref)
    state.rule_snapshot_ref = engine.put(state, {**snapshot, 'hash': digest('tampered')})
    with pytest.raises(ValueError, match='快照'):
        engine.ensure_rule_snapshot(state)
