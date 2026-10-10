from copy import deepcopy

import pytest
from pydantic import ValidationError

from tracefix.knowledge.assembler import ContextAssembler, TokenCounter
from tracefix.knowledge.context import build_context
from tracefix.rules import Finding, Rule, RuleResolver, render_rule_context
from tracefix.runtime.continuation import derive_run
from tracefix.runtime.contracts import GUIIssue, Outcome, Phase, RunState, RunStatus, digest
from tracefix.runtime.finding_source import (
    SourceRunIdentity, create_finding_repair_run, evidence_closure,
    rebind_finding_repair_revision, resolve_source_run, validate_finding_repair_source,
)
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore


def source_fixture(tmp_path, *, phases=None):
    artifacts = Artifacts(tmp_path / 'artifacts')
    store = MemoryStore()
    rule = Rule(id='feedback', name='操作结果应显示反馈', status='enabled',
        phases=phases or ['EXPLORE', 'DIAGNOSE', 'PATCH', 'VERIFY'],
        scope={'project_ids': ['project']},
        detection={'type': 'oracle', 'oracle': {'kind': 'console_no_error'}},
        fix_guidance='在真实成功和失败分支显示可访问提示', tags=['用户规则'])
    resolver = RuleResolver([rule])
    source = RunState(run_id='internal_run', scope_id='project', repo_id='repo',
        goal='检查保存操作是否显示结果', url='http://app:3000', mode='test')
    snapshot = {'scope_id': source.scope_id, 'repo_id': source.repo_id, 'commit': 'a' * 40,
        'files': {'src/value.ts': digest('old')}, 'workspace_repository': {'head': 'b' * 40}}
    source.repo_snapshot_ref = artifacts.put(source.scope_id, source.run_id, snapshot, label='源码快照')
    source.source_manifest = digest(snapshot)
    rule_snapshot, _ = resolver.resolve_snapshot(run_id=source.run_id, project_id=source.scope_id,
        scope_id=source.scope_id, repo_id=source.repo_id, phase='*')
    source.rule_snapshot_ref = artifacts.put(source.scope_id, source.run_id,
        rule_snapshot.model_dump(mode='json'), label='规则快照')
    source.rule_snapshot_hash = rule_snapshot.hash
    source.rule_refs = [reference.model_dump(mode='json') for reference in rule_snapshot.refs]
    screenshot = artifacts.put(source.scope_id, source.run_id, b'pixels', 'png')
    source.observation_ref = artifacts.put(source.scope_id, source.run_id,
        {'url': source.url, 'snapshot': '- button 保存', 'screenshot_ref': screenshot}, label='观测')
    source.evidence_refs = [source.observation_ref]
    source.replay_plan_ref = artifacts.put(source.scope_id, source.run_id,
        [{'kind': 'navigate', 'value': source.url}], label='重现计划')
    source.diagnosis_feedback_refs = [artifacts.put(source.scope_id, source.run_id,
        {'reason': '保存分支没有显示反馈'}, label='失败反馈')]
    source.run_status = RunStatus.COMPLETED
    source.outcome = Outcome.FIX_VERIFIED
    source.reproduced = True
    source.patch_hash = 'previous-patch'
    source.patch_base_commit = 'b' * 40
    store.save(source)
    finding = Finding(id='selected_finding', job_id='job', run_id=source.run_id,
        scope_id=source.scope_id, project_id=source.scope_id, repo_id=source.repo_id,
        rule_id=rule.id, rule_version=rule.version, source=rule.source, severity='major',
        title='保存操作缺少结果提示', expected='保存后显示结果', actual='没有结果提示',
        location={'url': source.url}, evidence_refs=[screenshot], fingerprint='stable',
        status='suspected', source_base_commit=snapshot['commit'], source_manifest=source.source_manifest)
    findings = {finding.id: finding}
    adapter = lambda external_id: SourceRunIdentity(run_id=source.run_id, scope_id=source.scope_id)
    options = {'resolve_run_id': adapter, 'load_run': store.load,
               'load_finding': findings.__getitem__, 'artifacts': artifacts, 'rule_resolver': resolver}
    return source, findings, artifacts, store, resolver, options


def test_gui_issue_source_is_backward_compatible_and_limited_to_one_rule():
    legacy = {'title': '问题', 'expected': '预期', 'actual': '实际', 'evidence_refs': ['ev']}
    assert GUIIssue.model_validate(legacy).rule_refs == []
    with pytest.raises(ValidationError):
        GUIIssue.model_validate({**legacy, 'rule_refs': ['first', 'second']})
    state = RunState(scope_id='project', goal='测试旧契约', url='http://app')
    assert state.rule_ids == [] and state.rule_coverage == [] and state.repair_source is None
    assert state.project_id is None and state.repo_id is None


def test_explicit_adapter_requires_unique_internal_run_identity(tmp_path):
    source, _, _, store, _, _ = source_fixture(tmp_path)
    identity = SourceRunIdentity(run_id=source.run_id, scope_id=source.scope_id)
    assert resolve_source_run('control_task', resolve_run_id=lambda external: identity,
                              load_run=store.load).run_id == source.run_id
    for identities in ([], [identity, identity], {'agentRunId': source.run_id}, source.run_id):
        with pytest.raises(ValueError, match='唯一内部'):
            resolve_source_run('control_task', resolve_run_id=lambda external: identities, load_run=store.load)


def test_selected_finding_creates_independent_repair_with_complete_context(tmp_path):
    source, findings, artifacts, store, resolver, options = source_fixture(tmp_path)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    provenance = repair.repair_source
    assert provenance.source_run_id == source.run_id and repair.run_id != source.run_id
    assert provenance.source_base_commit == 'a' * 40
    assert provenance.source_workspace_base_commit == 'b' * 40
    assert repair.phase == Phase.PREPARE and repair.outcome is None and not repair.reproduced
    assert not repair.evidence_refs and not repair.validation_refs and not repair.check_result_refs
    selected = provenance.findings[0]
    assert selected['title'] == findings['selected_finding'].title
    assert selected['expected'] and selected['actual'] and selected['location']
    assert selected['rule']['version'] == 1 and selected['fix_guidance']
    assert selected['replay_plan'] and selected['feedback'] and selected['evidence_refs']
    assert len(evidence_closure(source, artifacts)) == 2
    validated = validate_finding_repair_source(repair, load_run=store.load,
        load_finding=findings.__getitem__, artifacts=artifacts, rule_resolver=resolver)
    assert validated[0].id == 'selected_finding' and source.outcome == Outcome.FIX_VERIFIED


@pytest.mark.parametrize('updates,message', [
    ({'run_id': 'another_run'}, 'source Run'), ({'run_id': None}, 'source Run'),
    ({'scope_id': None}, 'scope'), ({'scope_id': 'other'}, 'scope'),
    ({'repo_id': 'other'}, '项目/仓库'), ({'source_base_commit': 'c' * 40}, 'commit/manifest'),
    ({'source_manifest': 'other'}, 'commit/manifest'), ({'evidence_refs': ['outside.json']}, '证据闭包'),
    ({'rule_version': 2}, '规则版本'), ({'source': 'static'}, '规则版本'),
])
def test_source_finding_must_match_run_scope_commit_and_evidence(tmp_path, updates, message):
    _, findings, _, _, _, options = source_fixture(tmp_path)
    findings['selected_finding'] = findings['selected_finding'].model_copy(update=updates)
    with pytest.raises(ValueError, match=message):
        create_finding_repair_run('control_task', ['selected_finding'], **options)


def test_missing_evidence_or_changed_hash_rejected(tmp_path):
    source, _, artifacts, _, _, options = source_fixture(tmp_path)
    artifacts._path(source.scope_id, source.run_id, source.observation_ref).write_bytes(b'changed')
    with pytest.raises(ValueError, match='完整性'):
        create_finding_repair_run('control_task', ['selected_finding'], **options)


def test_repair_rule_missing_patch_or_verify_is_rejected(tmp_path):
    _, _, _, _, _, options = source_fixture(tmp_path, phases=['EXPLORE', 'DIAGNOSE'])
    with pytest.raises(ValueError, match='覆盖不足'):
        create_finding_repair_run('control_task', ['selected_finding'], **options)


def test_revision_requires_explicit_rebind_and_resets_acceptance(tmp_path):
    _, _, artifacts, _, _, options = source_fixture(tmp_path)
    with pytest.raises(ValueError, match='显式'):
        create_finding_repair_run('control_task', ['selected_finding'], target_base_commit='c' * 40, **options)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    repair.test_spec_ref = 'previous-spec'
    repair.observation_ref = 'previous-observation'
    repair.replay_plan_ref = 'previous-replay'
    repair.reproduced = True
    repair.validation_refs = ['previous-validation']
    repair.outcome = Outcome.FIX_VERIFIED
    snapshot = deepcopy(repair.repair_source.source_repo_snapshot)
    snapshot.update(commit='c' * 40, workspace_repository={'head': 'd' * 40})
    reference = artifacts.put(repair.scope_id, repair.run_id, snapshot, label='新源码快照')
    rebound = rebind_finding_repair_revision(repair, repo_snapshot=snapshot,
        repo_snapshot_ref=reference, artifacts=artifacts)
    assert rebound.repair_source.revision_rebound
    assert rebound.repair_source.target_base_commit == 'c' * 40
    assert rebound.repair_source.source_base_commit == 'a' * 40
    assert not rebound.test_spec_ref and not rebound.observation_ref and not rebound.replay_plan_ref
    assert not rebound.reproduced and not rebound.validation_refs and rebound.outcome is None


def test_repair_source_is_revalidated_before_results_are_written(tmp_path):
    _, _, artifacts, store, resolver, options = source_fixture(tmp_path)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    repair.repair_source.findings[0]['expected'] = 'forged acceptance'
    with pytest.raises(ValueError, match='正文/来源'):
        validate_finding_repair_source(repair, load_run=store.load,
            load_finding=options['load_finding'], artifacts=artifacts, rule_resolver=resolver)


@pytest.mark.parametrize('phase', [Phase.EXPLORE, Phase.DIAGNOSE, Phase.PATCH, Phase.VERIFY])
def test_rules_and_finding_source_survive_compaction_and_workset(tmp_path, phase):
    _, _, _, _, resolver, options = source_fixture(tmp_path)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    repair.phase = phase
    rules = render_rule_context(resolver.source)
    context = build_context(repair, {'authorized_actions': ['navigate', 'finish']},
        observation={'snapshot': '- button 保存\n' * 4000}, rule_context=rules)
    context.update(allowed_files=['src/**'], allowed_urls=['http://app:3000'])
    assembled = ContextAssembler(context_window=14000, output_tokens=1000, overhead_tokens=500,
        counter=TokenCounter(lambda text: len(text))).assemble(context)
    assert assembled.compacted
    assert assembled.context['detection_rules'] == rules
    assert assembled.context['repair_source'] == repair.repair_source.model_dump(mode='json')
    assert assembled.context['allowed_files'] == ['src/**']
    assert assembled.context['test_spec']['authorized_actions'] == ['navigate', 'finish']
    protected = {block['name']: block for block in assembled.manifest['blocks']}
    assert protected['repair_source']['protected'] and protected['detection_rules']['protected']


def test_derived_run_drops_check_suite_and_previous_verification(tmp_path):
    source, _, _, _, _, _ = source_fixture(tmp_path)
    source.check_plan_ref = 'old-check-plan'
    source.check_result_refs = source.initial_check_result_refs = ['old-result']
    source.check_suite_completed = True
    source.overall_status = 'PASSED'
    source.rule_coverage = [{'status': 'checked_pass'}]
    source.budget.model_calls = 40
    child = derive_run(source)
    assert not child.check_plan_ref and not child.check_result_refs and not child.initial_check_result_refs
    assert not child.check_suite_completed and child.overall_status is None
    assert not child.rule_coverage and child.budget.model_calls == 0 and child.outcome is None
