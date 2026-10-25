import asyncio
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.rules import Finding, Rule, RuleLibrary, RuleResolver
from tracefix.runtime.contracts import (BrowserAction, CheckItem, FileEdit, FindingRepairSource,
    Outcome, PatchProposal, Phase, RunState, digest)
from tracefix.runtime.finding_acceptance import freeze_finding_acceptance
from tracefix.runtime.smoke import PNG, make_engine
from tracefix.service.finding import VerifiedFindingAcceptance
from tracefix.service.resources import RunServices
from tracefix.storage.store import UnknownOperation
from tests.test_finding_acceptance import RecordingRunner, representative_spec


@pytest.fixture
async def acceptance_engine(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    spec = representative_spec()
    spec.goal = state.goal
    for action in spec.regression_plan:
        if action.kind == 'navigate':
            action.value = state.url
    for scenario in spec.behavior_scenarios:
        for step in scenario.steps:
            if step.action.kind == 'navigate':
                step.action.value = state.url
    evidence = engine.artifacts.put(state.scope_id, 'source_run', PNG, 'png')
    finding = Finding(id='selected', job_id='source-task', run_id='source_run', scope_id=state.scope_id,
        project_id=state.scope_id, repo_id='ci', source='guided', severity='minor', title='任务完成失败',
        expected='完成后保持勾选', actual='完成没有生效', evidence_refs=[evidence], fingerprint='stable',
        source_base_commit=engine.source['commit'], source_manifest=digest('source snapshot'))
    library = RuleLibrary(tmp_path / 'rules.sqlite3')
    library.save_finding(finding)
    engine.rule_library = library
    engine.read_source_findings = lambda current: [library.finding(finding.id)]
    source = FindingRepairSource(source_run_id='source_run', source_finding_ids=[finding.id],
        source_scope_id=state.scope_id, source_project_id=state.scope_id, source_repo_id='ci',
        source_base_commit=engine.source['commit'], source_manifest=finding.source_manifest,
        source_repo_snapshot_ref='snapshot.json', source_repo_snapshot=engine.source,
        source_rule_snapshot_ref='rules.json', source_rule_snapshot_hash=digest('rules'),
        source_rule_snapshot={}, source_evidence_refs=[evidence],
        source_evidence_hashes={evidence: digest(engine.artifacts.read(state.scope_id, 'source_run', evidence))},
        findings=[finding.model_dump(mode='json')], target_base_commit=engine.source['commit'])
    state.repair_source, state.repo_id = source, 'ci'
    state.environment_digest = engine.runner.environment_digest
    state.source_aligned = True
    state.test_spec_ref = engine.put(state, spec.model_dump(mode='json'), name='冻结验收规范')
    state.test_spec_hash = digest(spec)
    state.phase = Phase.PREPARE
    state.step = 2
    engine.runner.started = True
    engine.store.save(state)
    payload = {'schema_version': 1, 'run_id': state.run_id, 'repo_id': state.scope_id,
        'commit': engine.source['commit'], 'source_run_id': 'source_run', 'source_scope_id': state.scope_id,
        'source_repo_id': 'ci', 'source_commit': engine.source['commit'],
        'source_manifest': source.source_manifest, 'source_rule_snapshot_hash': source.source_rule_snapshot_hash,
        'findings': [{'finding_id': finding.id, 'fingerprint': finding.fingerprint, 'expected': finding.expected,
            'actual': finding.actual, 'evidence_refs': [evidence], 'original_scenario_id': 'complete',
            'guards': [{'kind': 'normal', 'scenario_id': 'normal'},
                       {'kind': 'reverse', 'scenario_id': 'cancel'}, {'kind': 'refresh', 'scenario_id': 'refresh'}]}],
        'spec': spec.model_dump(mode='json'), 'spec_hash': digest(spec)}
    frozen = freeze_finding_acceptance(payload, state, engine.source)

    class FakeAcceptanceRunner(RecordingRunner):
        async def run(self, spec, **options):
            self.baseline = options['binding']['stage'] == 'baseline'
            return await super().run(spec, **options)

    runner = FakeAcceptanceRunner()
    services = RunServices(SimpleNamespace(run_id=state.run_id, repo_id=state.scope_id, github_reads=[]),
        SimpleNamespace(data_root=tmp_path), acceptance_runner_factory=lambda **options: runner)
    services.finding_acceptance = VerifiedFindingAcceptance(state.run_id, state.scope_id, engine.source['commit'],
        'source_run', (finding.id,), spec, frozen=frozen)
    services.bind_session(SimpleNamespace(engine=engine, artifacts=engine.artifacts))
    await services.prepare()
    engine.runtime_services = services
    monkeypatch.setenv('TRACEFIX_SERVICE_ACCEPTANCE_RESET_PATH', '/reset')
    return engine, state, services, runner, finding


async def candidate(engine, state):
    state.patch_base_commit = engine.workspace.head()
    proposal = PatchProposal(summary='Fake test candidate', evidence_refs=state.finding_acceptance_evidence_refs,
        edits=[FileEdit(path='src/value.ts', before_hash=digest(engine.workspace.read('src/value.ts').encode()),
                        content='export const persisted = true;\n')])
    state.patch_ref = engine.put(state, proposal.model_dump(mode='json'))
    state.patch_hash = engine.workspace.apply(proposal, base=state.patch_base_commit)['patch_hash']
    state.phase = Phase.VERIFY
    engine.store.save(state)
    return state


async def test_frozen_spec_or_inherited_success_never_substitutes_independent_baseline(acceptance_engine):
    engine, state, _, runner, _ = acceptance_engine
    state.reproduced = True
    state.outcome = Outcome.FIX_VERIFIED
    assert not engine.source_acceptance_ready(state)
    assert not engine.source_rule_trials_passed(state)
    assert not engine.finding_acceptance_verified(state)
    assert runner.calls == []


async def test_prepare_requires_sdk_configuration_and_independent_baseline_before_patch(acceptance_engine):
    engine, state, _, runner, _ = acceptance_engine
    with pytest.raises(PermissionError, match='Claude Agent SDK'):
        await engine.prepare(state, None)
    assert runner.calls == [] and not state.patch_hash
    assert engine.runner.started and state.source_aligned
    state = await engine.execute_finding_acceptance(state)
    assert len(runner.calls) == 1
    assert runner.calls[0][1]['stage'] == 'baseline' and runner.calls[0][1]['patch_hash'] is None
    assert state.finding_acceptance_ref and state.finding_reproduction_ref
    assert engine.source_acceptance_ready(state) and engine.source_rule_trials_passed(state)
    assert not state.patch_hash and not state.patch_ref
    assert engine.get(state, state.test_spec_ref) == runner.calls[0][0]
    state.phase = Phase.REPRODUCE
    engine.store.save(state)
    output = await engine.reproduce(state, None)
    state = RunState(**output['data'])
    assert state.phase == Phase.DIAGNOSE and state.reproduced
    assert len(runner.calls) == 1
    assert state.reproduction_plan_frozen
    assert engine.get(state, state.replay_plan_ref) == [step.action.model_dump(mode='json')
        for step in engine.spec(state).behavior_scenarios[0].steps]


async def test_baseline_receipt_survives_lost_state_update_and_resume(acceptance_engine):
    engine, state, services, runner, _ = acceptance_engine
    completed = await engine.execute_finding_acceptance(state)
    persisted = engine.store.load(state.run_id, state.scope_id)
    persisted.finding_reproduction_ref = None
    persisted.finding_acceptance_evidence_refs = []
    engine.store.save(persisted)
    services.finding_acceptance.reproduction_ref = None
    resumed = await engine.execute_finding_acceptance(persisted)
    assert resumed.finding_reproduction_ref == completed.finding_reproduction_ref
    assert len(runner.calls) == 1
    assert engine.source_acceptance_ready(resumed)


@pytest.mark.parametrize('boundary', ['cancel', 'unknown'])
async def test_cancelled_or_unknown_baseline_cannot_be_blindly_replayed(acceptance_engine, boundary):
    engine, state, services, _, _ = acceptance_engine
    calls = []

    async def uncertain(current):
        calls.append(True)
        if boundary == 'cancel':
            raise asyncio.CancelledError
        raise RuntimeError('uncertain runner outcome')

    services.reproduce_finding_acceptance = uncertain
    with pytest.raises(asyncio.CancelledError if boundary == 'cancel' else RuntimeError) as caught:
        await engine.execute_finding_acceptance(state)
    operation_id = caught.value.tracefix_operation_id
    saved = engine.store.load(state.run_id, state.scope_id)
    with pytest.raises(UnknownOperation) as blocked:
        await engine.execute_finding_acceptance(saved)
    assert blocked.value.details['operation_id'] == operation_id
    assert calls == [True]
    assert list(engine.store.operations) == [operation_id]


async def test_baseline_export_is_downloadable_and_never_exposes_host_inventory(acceptance_engine):
    engine, state, _, _, _ = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    assert state.finding_reproduction_ref not in state.evidence_refs
    refs = state.finding_acceptance_evidence_refs
    binary = next(engine.get(state, ref) for ref in refs if engine.get(state, ref).get('encoding') == 'base64')
    assert binary['media_type'] == 'application/zip'
    assert base64.b64decode(binary['content']) == b'actual runner evidence'
    assert digest(base64.b64decode(binary['content'])) == binary['content_hash']
    manifest = next(engine.get(state, ref) for ref in refs if engine.get(state, ref).get('binding'))
    assert manifest['binding']['acceptance_hash'] == state.finding_acceptance_hash
    assert str(engine.artifacts.root.parent) not in json.dumps(manifest)
    assert all(engine.bundle_exists(state, ref) for ref in refs + manifest['evidence_refs'])
    private = engine.get(state, state.finding_reproduction_ref)
    assert any(Path(path).is_absolute() for path in private['evidence_hashes'])


async def test_public_json_evidence_is_controlled_projection_without_host_inventory(acceptance_engine):
    engine, state, _, runner, _ = acceptance_engine
    host_payload = str((Path(engine.artifacts.root).parent / 'host-inventory.json').resolve())
    original_run = runner.run

    async def run_with_host_inventory(spec, **options):
        report = await original_run(spec, **options)
        evidence_path = options['artifacts_dir'] / 'runner-result.json'
        evidence_ref = str(evidence_path.resolve())
        report['evidence_refs'].append(evidence_ref)
        for case in report['cases']:
            case['evidence_refs'].append(evidence_ref)
            case['untrusted_payload'] = {'host_path': host_payload}
            case['checks'][0]['error'] = host_payload
        report['error'] = host_payload
        evidence_path.write_text(json.dumps({
            'host_path': host_payload,
            'nested': {'absolute': host_payload, 'arbitrary': {'token': 'must not escape'}},
            'report': report,
        }), encoding='utf-8')
        return report

    runner.run = run_with_host_inventory
    state = await engine.execute_finding_acceptance(state)
    refs = state.finding_acceptance_evidence_refs

    def strings(value):
        if isinstance(value, dict):
            return [text for child in value.values() for text in strings(child)]
        if isinstance(value, list):
            return [text for child in value for text in strings(child)]
        return [value] if isinstance(value, str) else []

    public_json = []
    for ref in refs:
        value = engine.get(state, ref)
        if value.get('media_type') == 'application/json' and value.get('encoding') == 'base64':
            public_json.append(value)
    assert public_json
    for value in public_json:
        decoded = base64.b64decode(value['content']).decode('utf-8')
        assert 'untrusted_payload' not in decoded
        assert value['source_content_hash'] != value['content_hash']
        assert digest(base64.b64decode(value['content'])) == value['content_hash']
        projected = json.loads(decoded)
        assert not any(host_payload in value or str(engine.artifacts.root.parent) in value
                       for value in strings(projected))
        assert {case['id'] for case in projected['cases']} == {'complete', 'normal', 'cancel', 'refresh'}
        assert projected['evidence_refs']
        assert all(ref in refs and engine.bundle_exists(state, ref) for ref in projected['evidence_refs'])
        assert all(ref in projected['evidence_refs'] for case in projected['cases']
                   for ref in case['evidence_refs'])
    manifest = next(engine.get(state, ref) for ref in refs
                    if engine.get(state, ref).get('binding'))
    assert set(manifest['evidence_refs']) <= set(refs)
    assert 'reason' not in manifest and 'error' not in manifest
    assert not any(host_payload in value or str(engine.artifacts.root.parent) in value
                   for value in strings(manifest))
    assert all(engine.bundle_exists(state, ref) for ref in manifest['evidence_refs'])
    for ref in manifest['evidence_refs']:
        value = engine.get(state, ref)
        content = base64.b64decode(value['content'])
        assert digest(content) == manifest['evidence_hashes'][ref] == value['content_hash']
        assert manifest['source_evidence_hashes'][ref] == value['source_content_hash']
    private = engine.get(state, state.finding_reproduction_ref)
    source_json = next(path for path in private['evidence_hashes'] if path.endswith('runner-result.json'))
    assert digest(Path(source_json).read_bytes()) == private['evidence_hashes'][source_json]
    assert json.loads(Path(source_json).read_text(encoding='utf-8'))['host_path'] == host_payload
    assert engine.source_acceptance_ready(state)


async def test_same_frozen_baseline_and_patch_verification_keep_guard_checks(acceptance_engine):
    engine, state, _, runner, _ = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    state = await candidate(engine, state)
    state = await engine.execute_finding_acceptance(state, verify=True)
    assert engine.finding_acceptance_verified(state)
    assert runner.calls[0][0] == runner.calls[1][0]
    assert runner.calls[1][1]['patch_hash'] == state.patch_hash
    report = engine.get(state, engine.get(state, state.finding_verification_ref)['report_ref'])
    assert {item['id'] for item in report['cases']} == {'complete', 'normal', 'cancel', 'refresh'}
    state.patch_hash = digest('different candidate')
    assert not engine.finding_acceptance_verified(state)


async def test_completion_only_patch_is_rejected_before_success_and_source_fixed(acceptance_engine):
    engine, state, _, runner, finding = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    state = await candidate(engine, state)

    def cannot_cancel(report):
        if report['binding']['stage'] != 'verify':
            return
        cancel = next(item for item in report['cases'] if item['id'] == 'cancel')
        cancel['status'] = 'fail'
        cancel['checks'][-1].update(passed=False, failure_kind='assertion')
        report.update(status='fail', passed=False)

    runner.mutation = cannot_cancel
    state = await engine.execute_finding_acceptance(state, verify=True)
    assert not engine.finding_acceptance_verified(state)
    output = await engine.complete_verification(state)
    assert RunState(**output['data']).outcome == Outcome.INCONCLUSIVE
    assert engine.rule_library.finding(finding.id).status == 'suspected'


async def test_acceptance_check_results_are_runtime_proofs_and_oracle_gate_remains(acceptance_engine):
    engine, state, _, _, _ = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    state.phase = Phase.EXPLORE
    state.check_suite_stage = 'explore'
    engine.store.save(state)
    item = CheckItem(id='user_goal', name='目标', criteria=state.goal, source='user_goal', detector='goal')
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'fail' and result.fallback is None
    assert all(engine.bundle_exists(state, ref) for ref in result.evidence_refs)
    oracle = item.model_copy(update={'source': 'rule', 'id': 'rule', 'detector': 'oracle', 'rule_version': 1})
    assert engine.finding_acceptance_checks(state, oracle) is None
    state = await candidate(engine, state)
    state = await engine.execute_finding_acceptance(state, verify=True)
    state.check_suite_stage = 'verify'
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'pass'
    assert not engine.verification_passed(state)


@pytest.mark.parametrize('mutation', ['source', 'assertions', 'trace'])
async def test_proof_binding_corruption_blocks_ready_and_verification(acceptance_engine, mutation):
    engine, state, _, _, _ = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    if mutation == 'source':
        state.repair_source.source_run_id = 'other_run'
    elif mutation == 'assertions':
        state.test_spec_hash = digest('changed assertions')
    else:
        proof = engine.get(state, state.finding_reproduction_ref)
        Path(next(iter(proof['evidence_hashes']))).write_bytes(b'changed trace')
    assert not engine.source_acceptance_ready(state)
    assert not engine.finding_acceptance_verified(state)


async def test_missing_executor_is_inconclusive_and_never_ready(acceptance_engine, monkeypatch):
    engine, state, _, _, _ = acceptance_engine
    monkeypatch.delenv('TRACEFIX_SERVICE_ACCEPTANCE_RESET_PATH')
    state = await engine.execute_finding_acceptance(state)
    assert state.phase == Phase.FINALIZE and state.outcome == Outcome.INCONCLUSIVE
    assert not engine.source_acceptance_ready(state)


async def test_state_freezes_acceptance_and_clears_patch_proof_on_candidate_change(acceptance_engine):
    engine, state, _, _, _ = acceptance_engine
    state = await engine.execute_finding_acceptance(state)
    with pytest.raises(ValueError, match='不可变更'):
        engine.changed(state, finding_acceptance_hash=digest('changed'))
    with pytest.raises(ValueError, match='不可替换'):
        engine.changed(state, finding_reproduction_ref='other.json')
    state = await candidate(engine, state)
    state = await engine.execute_finding_acceptance(state, verify=True)
    state = engine.changed(state, patch_hash=digest('next candidate'))
    assert state.finding_verification_ref is None
    assert state.finding_reproduction_ref
