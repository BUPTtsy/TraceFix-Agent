import pytest
from types import SimpleNamespace

from tracefix.model.gateway import ModelOutputError, ModelResult
from tracefix.rules import Rule, RuleResolver, RuleLibrary
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, GUIIssue, Locator, Outcome,
                                        Phase, RunState, RunStatus, TestSpec as RuntimeTestSpec, digest)
from tracefix.runtime.smoke import FakeBrowser, FakeModel, FakeRunner, make_engine
from tracefix.runtime.engine import Engine
from tracefix.runtime.finding_source import (SourceRunIdentity, create_finding_repair_run,
                                             rebind_finding_repair_revision)
from tracefix.execution.workspace import Workspace
from tests.test_finding_repair_source import source_fixture
from tests.test_harness_graph import create_harness_engine, IncrementExecutor


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


def test_finding_repair_loads_complete_source_snapshot_without_inheriting_result(tmp_path):
    source, findings, artifacts, store, resolver, options = source_fixture(tmp_path)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    engine = object.__new__(Engine)
    engine.rule_resolver = resolver
    snapshot = engine._rule_snapshot(repair)
    assert snapshot.hash == repair.repair_source.source_rule_snapshot_hash
    assert snapshot.rules and snapshot.rules[0].fix_guidance
    assert repair.outcome is None and not repair.reproduced


def test_source_reproduction_matches_finding_location_and_actual(tmp_path):
    source, findings, artifacts, store, resolver, options = source_fixture(tmp_path)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    engine = object.__new__(Engine)
    finding = findings['selected_finding']
    mismatched = SimpleNamespace(id='feedback', status='fail', fallback=None,
        actual='[{"rule_id":"feedback","rule_version":1,"title":"保存操作缺少结果提示",'
               '"location":{"url":"http://other:3000"},"actual":"没有结果提示"}]')
    signatures = engine.source_finding_trial_signatures(repair, [mismatched])
    assert signatures[finding.id] == 'INCONCLUSIVE'
    matched = SimpleNamespace(id='feedback', status='fail', fallback=None,
        actual='[{"rule_id":"feedback","rule_version":1,"title":"保存操作缺少结果提示",'
               '"location":{"url":"http://app:3000"},"actual":"没有结果提示"}]')
    signatures = engine.source_finding_trial_signatures(repair, [matched])
    assert signatures[finding.id] not in {'PASS', 'INCONCLUSIVE'}


async def executable_repair_fixture(tmp_path, *, runner_fail=None):
    engine, source_run = make_engine(tmp_path)
    rule = runtime_rule(detector='static')
    library = RuleLibrary(tmp_path / 'rules.sqlite3')
    with library.connect() as connection:
        connection.execute('DELETE FROM rule_versions')
        connection.execute('DELETE FROM rules')
    rule = library.save_rule(rule)
    engine.rule_library = library
    engine.rule_resolver = RuleResolver(library)
    source_run.rule_ids = [rule.id]
    source_run.mode = 'test'
    engine.store.save(source_run)
    source_run = engine.ensure_rule_snapshot(source_run)
    source_run.phase = Phase.EXPLORE
    source_run.observation_ref = await engine.capture(source_run,
        await engine.browser.action(BrowserAction(kind='observe')))
    source_run.evidence_refs = [source_run.observation_ref]
    source_run = await engine.ensure_check_plan(source_run)
    source_run.check_suite_stage = 'explore'
    item = next(item for item in engine.check_plan(source_run).items if item.id == rule.id)
    result = await engine.evaluate_check_item(source_run, item)
    source_run.evidence_refs.extend(result.evidence_refs)
    source_run.run_status = RunStatus.COMPLETED
    source_run.outcome = Outcome.FIX_VERIFIED
    source_run.reproduced = True
    engine.store.save(source_run)
    finding = library.findings(run_id=source_run.run_id, rule_id=rule.id)[0]
    repair = create_finding_repair_run('control-task', [finding.id],
        resolve_run_id=lambda external: SourceRunIdentity(run_id=source_run.run_id, scope_id=source_run.scope_id),
        load_run=engine.store.load, load_finding=library.finding,
        artifacts=engine.artifacts, rule_resolver=engine.rule_resolver)
    workspace, snapshot = Workspace.export(engine.scopes, engine.context,
        repair.repair_source.source_base_commit, tmp_path / 'repair-workspace')
    snapshot_ref = engine.artifacts.put(repair.scope_id, repair.run_id, snapshot, label='独立修复源码快照')
    repair = rebind_finding_repair_revision(repair, repo_snapshot=snapshot,
        repo_snapshot_ref=snapshot_ref, artifacts=engine.artifacts)
    engine.workspace, engine.source = workspace, snapshot
    engine.browser, engine.runner = FakeBrowser(workspace), FakeRunner(digest(snapshot), runner_fail)

    class RepairFixtureModel(FakeModel):
        async def generate(self, schema, context, **options):
            if schema is RuntimeTestSpec:
                value = RuntimeTestSpec(goal=repair.goal, assertions=[Assertion(
                    locator=Locator(role='checkbox', name='Complete task'), condition='checked')],
                    regression_assertions=[Assertion(locator=Locator(role='heading', name='Task board'))])
                return ModelResult(value, {}, 'FAKE-CI', 'stop')
            return await super().generate(schema, context, **options)

    engine.model = RepairFixtureModel(workspace)
    repair.execution_mode = 'batch'
    spec = RuntimeTestSpec(goal=repair.goal, assertions=[Assertion(
        locator=Locator(role='checkbox', name='Complete task'), condition='checked')],
        regression_assertions=[Assertion(locator=Locator(role='heading', name='Task board'))])
    repair.test_spec_ref = engine.put(repair, spec.model_dump(mode='json'))
    repair.test_spec_hash = digest(spec)
    engine.store.save(repair)
    engine, repair, harness, _ = await create_harness_engine(tmp_path / 'sdk',
        engine_state=(engine, repair), runner_factory=lambda workspace, source: FakeRunner(source, runner_fail))

    class FindingExecutor(IncrementExecutor):
        async def start(self, request, emit):
            result = await super().start(request, emit)
            if request.role == 'initializer':
                result.structured_output['work_items'] = result.structured_output['work_items'][:1]
            return result

    harness.runner = FindingExecutor()
    return engine, repair, library, finding


async def test_fake_contract_source_repair_reproduces_and_verifies_before_fixed(tmp_path):
    engine, repair, library, finding = await executable_repair_fixture(tmp_path)
    assert repair.outcome is None and not repair.reproduced and repair.test_spec_ref
    await engine.run(repair)
    completed = engine.store.load(repair.run_id, repair.scope_id)
    assert completed.outcome == Outcome.FIX_VERIFIED
    assert completed.reproduced and completed.trial == 3 and completed.validation_refs
    fixed = library.finding(finding.id)
    assert fixed.status == 'fixed' and fixed.repair_run_id == repair.run_id
    assert fixed.patch_hash == completed.patch_hash and fixed.verification_refs == completed.validation_refs
    report = engine.get(completed, completed.report_ref)
    assert report['provenance']['verified'] and report['source_findings'][0]['id'] == finding.id


async def test_fake_contract_failed_repair_validation_keeps_source_unfixed(tmp_path):
    engine, repair, library, finding = await executable_repair_fixture(tmp_path, runner_fail='unit')
    await engine.run(repair)
    completed = engine.store.load(repair.run_id, repair.scope_id)
    assert completed.outcome != Outcome.FIX_VERIFIED
    assert library.finding(finding.id).status != 'fixed'
    report = engine.get(completed, completed.report_ref)
    assert report['patch_verification'] != 'verified' and not report['provenance']['verified']
