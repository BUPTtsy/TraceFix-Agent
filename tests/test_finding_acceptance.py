from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread

import pytest
from pydantic import ValidationError

from tracefix.runtime.contracts import FindingRepairSource, Outcome, RunState, digest
from tracefix.runtime.finding_acceptance import (
    AcceptanceSpec, FindingAcceptance, freeze_finding_acceptance,
    read_finding_reproduction, reproduce_finding_acceptance, verify_finding_acceptance,
)
from tracefix.storage.artifacts import Artifacts


URL = 'http://localhost:43210/'


def checkpoint(condition):
    return {'locator': {'role': 'checkbox', 'name': '代表性任务'}, 'condition': condition}


def step(kind, condition, value=None):
    action = {'kind': kind}
    if kind == 'click':
        action['locator'] = {'role': 'checkbox', 'name': '代表性任务'}
    if value is not None:
        action['value'] = value
    return {'action': action, 'assertions': [checkpoint(condition)]}


def representative_spec():
    return AcceptanceSpec.model_validate({'goal': '修复任务完成和取消状态持久化',
        'assertions': [checkpoint('checked')],
        'regression_plan': [{'kind': 'navigate', 'value': URL}],
        'regression_assertions': [checkpoint('unchecked')],
        'behavior_scenarios': [
            {'id': 'complete', 'steps': [step('navigate', 'unchecked', URL), step('click', 'checked')]},
            {'id': 'normal', 'steps': [step('navigate', 'unchecked', URL), step('observe', 'unchecked')]},
            {'id': 'cancel', 'steps': [step('navigate', 'unchecked', URL), step('click', 'checked'),
                                    step('click', 'unchecked')]},
            {'id': 'refresh', 'steps': [step('navigate', 'unchecked', URL), step('click', 'checked'),
                                     step('navigate', 'checked', URL)]},
        ]})


@pytest.fixture
def acceptance_fixture(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    evidence = artifacts.put('project', 'source_run', b'original screenshot', 'png')
    finding = {'id': 'selected', 'fingerprint': 'stable', 'expected': '完成后保持勾选',
        'actual': '完成没有生效', 'rule_id': None, 'rule_version': None, 'source': 'user',
        'title': '任务完成失败', 'evidence_refs': [evidence], 'status': 'suspected'}
    source = FindingRepairSource(source_run_id='source_run', source_finding_ids=['selected'],
        source_scope_id='project', source_project_id='project', source_repo_id='owner/repo',
        source_base_commit='a' * 40, source_manifest=digest('source snapshot'),
        source_repo_snapshot_ref='snapshot.json', source_repo_snapshot={'commit': 'a' * 40},
        source_rule_snapshot_ref='rules.json', source_rule_snapshot_hash=digest('rules'),
        source_rule_snapshot={}, source_evidence_refs=[evidence],
        source_evidence_hashes={evidence: digest(artifacts.read('project', 'source_run', evidence))},
        findings=[finding], target_base_commit='a' * 40)
    state = RunState(run_id='repair_run', scope_id='project', project_id='project', repo_id='owner/repo',
        mode='repair', goal='修复任务完成和取消状态持久化', url=URL, repair_source=source,
        source_manifest=digest('repair snapshot'), environment_digest=digest('isolated environment'))
    spec = representative_spec()
    payload = {'schema_version': 1, 'run_id': state.run_id, 'repo_id': state.scope_id,
        'commit': 'a' * 40, 'source_run_id': 'source_run', 'source_scope_id': 'project',
        'source_repo_id': 'owner/repo', 'source_commit': 'a' * 40,
        'source_manifest': source.source_manifest, 'source_rule_snapshot_hash': source.source_rule_snapshot_hash,
        'findings': [{'finding_id': 'selected', 'fingerprint': 'stable', 'expected': finding['expected'],
            'actual': finding['actual'], 'evidence_refs': [evidence], 'original_scenario_id': 'complete',
            'guards': [{'kind': 'normal', 'scenario_id': 'normal'},
                       {'kind': 'reverse', 'scenario_id': 'cancel'},
                       {'kind': 'refresh', 'scenario_id': 'refresh'}]}],
        'spec': spec.model_dump(mode='json'), 'spec_hash': digest(spec)}
    return state, payload, artifacts


class RecordingRunner:
    def __init__(self, *, baseline=True, mutation=None):
        self.baseline, self.mutation, self.calls = baseline, mutation, []

    async def run(self, spec, *, url, artifacts_dir, binding, reset_path, include_regression):
        self.calls.append((spec.model_dump(mode='json'), deepcopy(binding), include_regression))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        evidence = artifacts_dir / 'trace.zip'
        evidence.write_bytes(b'actual runner evidence')
        reference = str(evidence.resolve())
        cases = []
        for scenario in spec.behavior_scenarios:
            checks = []
            failing = self.baseline and scenario.id == 'complete'
            for step_index, current in enumerate(scenario.steps, start=1):
                for assertion_index in range(len(current.assertions)):
                    passed = not (failing and step_index == len(scenario.steps))
                    checks.append({'step': step_index, 'assertion_index': assertion_index,
                        'context': 'checkpoint', 'passed': passed, 'failure_kind': None if passed else 'assertion'})
            cases.append({'id': scenario.id, 'scenario_hash': digest(scenario),
                'status': 'fail' if failing else 'pass', 'evidence_refs': [reference], 'checks': checks})
        report = {'schema_version': 'tracefix.playwright-acceptance/1', 'status': 'fail' if self.baseline else 'pass',
            'passed': not self.baseline, 'binding': binding, 'cases': cases, 'evidence_refs': [reference]}
        if self.mutation:
            self.mutation(report)
        return report


def freeze(fixture):
    state, payload, _ = fixture
    return freeze_finding_acceptance(payload, state, {'commit': 'a' * 40})


def case(report, scenario_id):
    return next(item for item in report['cases'] if item['id'] == scenario_id)


def test_controlled_spec_freezes_without_claiming_reproduction(acceptance_fixture):
    state, payload, _ = acceptance_fixture
    state.reproduced, state.outcome = True, Outcome.FIX_VERIFIED
    frozen = freeze(acceptance_fixture)
    payload['spec']['behavior_scenarios'][0]['steps'][-1]['assertions'][0]['condition'] = 'unchecked'
    obtained = frozen.spec
    obtained.behavior_scenarios[0].steps[-1].assertions[0].condition = 'unchecked'
    assert frozen.spec.behavior_scenarios[0].steps[-1].assertions[0].condition == 'checked'
    assert not hasattr(frozen, 'reproduced')


@pytest.mark.parametrize('field,value', [
    ('schema_version', True), ('schema_version', '1'), ('schema_version', 2),
    ('verified', True), ('test_code', 'assert true'), ('source_run_id', 'wrong'),
    ('source_scope_id', 'wrong'), ('source_repo_id', 'wrong/repo'), ('source_manifest', 'b' * 64),
    ('run_id', 'source_run'), ('repo_id', 'wrong'), ('spec_hash', 'b' * 64),
])
def test_request_cannot_forge_success_or_source(acceptance_fixture, field, value):
    state, payload, _ = acceptance_fixture
    payload[field] = value
    with pytest.raises(ValueError):
        freeze_finding_acceptance(payload, state, {'commit': 'a' * 40})


@pytest.mark.parametrize('mutation', [
    lambda payload: payload['spec'].update({'unknown': True}),
    lambda payload: payload['spec']['behavior_scenarios'][0]['steps'][0]['action'].update({'kind': 'evaluate'}),
    lambda payload: payload['spec']['behavior_scenarios'][0]['steps'][0]['action'].update({'kind': 'finish'}),
    lambda payload: payload['spec']['behavior_scenarios'][0]['steps'][0]['action'].update({'observation_id': 'old'}),
    lambda payload: payload['findings'][0]['guards'].pop(),
    lambda payload: payload['findings'][0].update({'rule_version': 2}),
    lambda payload: payload['findings'][0].update({'actual': 'forged'}),
    lambda payload: payload['findings'][0].update({'evidence_refs': ['forged.png']}),
])
def test_controlled_actions_and_guards_are_required(acceptance_fixture, mutation):
    state, payload, _ = acceptance_fixture
    mutation(payload)
    with pytest.raises(ValueError):
        freeze_finding_acceptance(payload, state, {'commit': 'a' * 40})


def test_frozen_navigation_stays_on_application_origin(acceptance_fixture):
    state, payload, _ = acceptance_fixture
    payload['spec']['behavior_scenarios'][0]['steps'][0]['action']['value'] = 'http://other-app/'
    payload['spec_hash'] = digest(payload['spec'])
    with pytest.raises(ValueError, match='origin'):
        freeze_finding_acceptance(payload, state, {'commit': 'a' * 40})


def test_revision_change_requires_explicit_rebind(acceptance_fixture):
    state, payload, _ = acceptance_fixture
    payload['commit'] = 'c' * 40
    with pytest.raises(ValueError, match='revision'):
        freeze_finding_acceptance(payload, state, {'commit': 'c' * 40})
    payload['allow_revision_rebind'] = True
    assert freeze_finding_acceptance(payload, state, {'commit': 'c' * 40}).acceptance.commit == 'c' * 40


async def test_real_execution_is_required_even_if_source_claimed_success(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    state.reproduced, state.outcome = True, Outcome.FIX_VERIFIED
    runner = RecordingRunner(baseline=False)
    result = await reproduce_finding_acceptance(frozen, state, runner=runner, artifacts=artifacts,
        artifacts_dir=tmp_path / 'independent')
    assert len(runner.calls) == 1 and result.outcome == Outcome.INCONCLUSIVE


async def test_baseline_and_same_spec_patch_verification(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    baseline = RecordingRunner()
    reproduction = await reproduce_finding_acceptance(frozen, state, runner=baseline,
        artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    assert reproduction.outcome == Outcome.BUG_CONFIRMED
    proof = read_finding_reproduction(frozen, state, artifacts=artifacts, reproduction_ref=reproduction.artifact_ref)
    assert proof['binding']['patch_hash'] is None
    state.patch_hash = digest('candidate patch')
    verification = RecordingRunner(baseline=False)
    result = await verify_finding_acceptance(frozen, state, runner=verification, artifacts=artifacts,
        artifacts_dir=tmp_path / 'verify', reproduction_ref=reproduction.artifact_ref)
    assert result.outcome == Outcome.FIX_VERIFIED
    assert baseline.calls[0][0] == verification.calls[0][0]
    assert verification.calls[0][1]['patch_hash'] == state.patch_hash


@pytest.mark.parametrize('mutation', [
    lambda report: report['binding'].update({'run_id': 'wrong'}),
    lambda report: case(report, 'complete').update({'status': 'pass'}),
    lambda report: case(report, 'complete')['checks'][-1].update({'failure_kind': 'action'}),
    lambda report: case(report, 'complete')['checks'][-1].update({'passed': 'false'}),
    lambda report: case(report, 'complete')['checks'][0].update({'passed': False, 'failure_kind': 'assertion'}),
    lambda report: case(report, 'complete').update({'scenario_hash': 'wrong'}),
    lambda report: case(report, 'complete').update({'evidence_refs': []}),
    lambda report: report.update({'evidence_refs': []}),
    lambda report: report['cases'].append(deepcopy(report['cases'][0])),
])
async def test_reproduction_requires_exact_business_failure_evidence(acceptance_fixture, tmp_path, mutation):
    state, _, artifacts = acceptance_fixture
    result = await reproduce_finding_acceptance(freeze(acceptance_fixture), state,
        runner=RecordingRunner(mutation=mutation), artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    assert result.outcome == Outcome.INCONCLUSIVE


async def test_completion_only_regression_patch_is_rejected(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    reproduction = await reproduce_finding_acceptance(frozen, state, runner=RecordingRunner(),
        artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    state.patch_hash = digest('completion only')

    def cannot_cancel(report):
        cancellation = case(report, 'cancel')
        cancellation['status'] = 'fail'
        cancellation['checks'][-1].update({'passed': False, 'failure_kind': 'assertion'})
        report.update(status='fail', passed=False)

    result = await verify_finding_acceptance(frozen, state,
        runner=RecordingRunner(baseline=False, mutation=cannot_cancel), artifacts=artifacts,
        artifacts_dir=tmp_path / 'verify', reproduction_ref=reproduction.artifact_ref)
    assert result.outcome == Outcome.BUG_CONFIRMED


@pytest.mark.parametrize('scenario_id', ['complete', 'normal', 'cancel', 'refresh'])
async def test_all_frozen_checkpoints_required_for_verified_fix(acceptance_fixture, tmp_path, scenario_id):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    baseline = await reproduce_finding_acceptance(frozen, state, runner=RecordingRunner(),
        artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    state.patch_hash = digest('candidate')
    result = await verify_finding_acceptance(frozen, state,
        runner=RecordingRunner(baseline=False, mutation=lambda report: case(report, scenario_id)['checks'].pop()),
        artifacts=artifacts, artifacts_dir=tmp_path / 'verify', reproduction_ref=baseline.artifact_ref)
    assert result.outcome == Outcome.INCONCLUSIVE


async def test_changed_baseline_trace_cannot_verify_candidate(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    reproduction = await reproduce_finding_acceptance(frozen, state, runner=RecordingRunner(),
        artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    (tmp_path / 'baseline' / 'trace.zip').write_bytes(b'altered evidence')
    state.patch_hash = digest('candidate')
    runner = RecordingRunner(baseline=False)
    with pytest.raises(ValueError, match='证据已经变化'):
        await verify_finding_acceptance(frozen, state, runner=runner, artifacts=artifacts,
            artifacts_dir=tmp_path / 'verify', reproduction_ref=reproduction.artifact_ref)
    assert not runner.calls


async def test_no_patch_baseline_after_patch_and_spec_changes_are_rejected(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture
    frozen = freeze(acceptance_fixture)
    state.patch_hash = digest('candidate')
    with pytest.raises(ValueError, match='候选补丁'):
        await reproduce_finding_acceptance(frozen, state, runner=RecordingRunner(), artifacts=artifacts,
            artifacts_dir=tmp_path / 'baseline')
    state.patch_hash, state.test_spec_hash = None, digest('changed assertions')
    with pytest.raises(ValueError, match='冻结断言'):
        await reproduce_finding_acceptance(frozen, state, runner=RecordingRunner(), artifacts=artifacts,
            artifacts_dir=tmp_path / 'baseline')


async def test_unavailable_executor_is_inconclusive(acceptance_fixture, tmp_path):
    state, _, artifacts = acceptance_fixture

    class UnavailableRunner:
        async def run(self, *args, **kwargs):
            raise FileNotFoundError('missing Playwright')

    result = await reproduce_finding_acceptance(freeze(acceptance_fixture), state,
        runner=UnavailableRunner(), artifacts=artifacts, artifacts_dir=tmp_path / 'baseline')
    assert result.outcome == Outcome.INCONCLUSIVE


@pytest.mark.skipif(os.getenv('TRACEFIX_BETA_BROWSER_TESTS') != '1',
                    reason='需要显式启用独立 Playwright 浏览器验收')
async def test_actual_playwright_freezes_and_rejects_cancellation_regression(acceptance_fixture, tmp_path):
    from tracefix.runtime.playwright_acceptance import PlaywrightAcceptanceRunner
    state, payload, artifacts = acceptance_fixture
    app = {'checked': False, 'mode': 'defect'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/__reset':
                app['checked'] = False
                body = b'{}'
            elif self.path == '/api/task':
                body = json.dumps({'checked': app['checked']}).encode()
            else:
                body = '''<h1>任务验收</h1><input type="checkbox" aria-label="代表性任务"><script>
                    const checkbox = document.querySelector('input');
                    async function read() {checkbox.checked = (await (await fetch('/api/task')).json()).checked;}
                    checkbox.onchange = async () => {
                        checkbox.disabled = true;
                        await fetch('/api/task', {method:'PATCH'});
                        await read(); checkbox.disabled = false;
                    }; read();
                    </script>'''.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if self.path == '/' else 'application/json')
            self.end_headers()
            self.wfile.write(body)

        def do_PATCH(self):
            if app['mode'] == 'fixed':
                app['checked'] = not app['checked']
            elif app['mode'] == 'regression':
                app['checked'] = True
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'checked': app['checked']}).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{server.server_port}/'
    for scenario in payload['spec']['behavior_scenarios']:
        for current in scenario['steps']:
            if current['action']['kind'] == 'navigate':
                current['action']['value'] = state.url
            elif current['action']['kind'] == 'click':
                current['action']['wait'] = {'assertions': [{'locator': {'role': 'checkbox',
                    'name': '代表性任务'}, 'condition': 'enabled'}]}
    payload['spec']['regression_plan'][0]['value'] = state.url
    payload['spec'] = AcceptanceSpec.model_validate(payload['spec']).model_dump(mode='json')
    payload['spec_hash'] = digest(payload['spec'])
    frozen = freeze_finding_acceptance(payload, state, {'commit': 'a' * 40})
    runner = PlaywrightAcceptanceRunner(timeout_seconds=120)
    try:
        baseline = await reproduce_finding_acceptance(frozen, state, runner=runner, artifacts=artifacts,
            artifacts_dir=tmp_path / 'baseline-browser', reset_path='/__reset')
        assert baseline.outcome == Outcome.BUG_CONFIRMED, baseline.reason
        app['mode'], state.patch_hash = 'fixed', digest('fixed implementation')
        fixed = await verify_finding_acceptance(frozen, state, runner=runner, artifacts=artifacts,
            artifacts_dir=tmp_path / 'fixed-browser', reproduction_ref=baseline.artifact_ref, reset_path='/__reset')
        assert fixed.outcome == Outcome.FIX_VERIFIED, fixed.reason
        app['mode'], state.patch_hash = 'regression', digest('completion only implementation')
        regressed = await verify_finding_acceptance(frozen, state, runner=runner, artifacts=artifacts,
            artifacts_dir=tmp_path / 'regressed-browser', reproduction_ref=baseline.artifact_ref, reset_path='/__reset')
        assert regressed.outcome == Outcome.BUG_CONFIRMED, regressed.reason
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_external_json_is_not_a_runtime_reproduction(acceptance_fixture):
    state, payload, artifacts = acceptance_fixture
    with pytest.raises(ValidationError):
        FindingAcceptance.model_validate({**payload, 'reproduction_ref': 'user-supplied.json'})
    forged = artifacts.put(state.scope_id, state.run_id, {'verified': True, 'outcome': 'BUG_CONFIRMED'})
    with pytest.raises(ValueError, match='修补前冻结'):
        read_finding_reproduction(freeze(acceptance_fixture), state, artifacts=artifacts, reproduction_ref=forged)
