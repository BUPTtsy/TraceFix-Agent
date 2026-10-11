from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.runtime.contracts import ObservableWait, TestSpec as RuntimeTestSpec, digest
from tracefix.runtime.playwright_acceptance import (
    PlaywrightAcceptanceRunner, VERSION, acceptance_request, oracle_result, validate_execution)


def fixture_spec():
    assertion = {'locator': {'role': 'checkbox', 'name': 'Complete task'}, 'condition': 'checked'}
    return RuntimeTestSpec(goal='检查完成状态', assertions=[assertion], regression_assertions=[assertion],
        behavior_scenarios=[{'id': 'complete', 'steps': [{'action': {'kind': 'click',
            'locator': assertion['locator']}, 'assertions': [assertion]}]}])


def request(spec):
    return acceptance_request(spec, url='http://127.0.0.1:39991', reset_path='/__reset',
        binding={'run_id': 'run', 'repo_id': 'repo', 'commit': 'revision', 'spec_hash': digest(spec)},
        include_regression=False)


def test_request_rejects_wrong_hash_and_normalizes_url():
    spec = fixture_spec()
    assert request(spec)['url'].endswith('/')
    with pytest.raises(ValueError, match='hash'):
        acceptance_request(spec, url='http://127.0.0.1:39991', reset_path=None, binding={})
    with pytest.raises(ValueError, match='HTTP'):
        acceptance_request(spec, url='file:///private', reset_path=None,
                           binding={'run_id': 'run', 'repo_id': 'repo', 'commit': 'rev', 'spec_hash': digest(spec)})


def test_evidence_adapter_rejects_missing_and_foreign_binding(tmp_path):
    spec = fixture_spec()
    payload = request(spec)
    result = {'schema_version': VERSION, 'status': 'pass', 'passed': True, 'binding': payload['binding'],
        'cases': [{'id': 'complete', 'scenario_hash': digest(spec.behavior_scenarios[0]),
                   'status': 'pass', 'checks': [], 'evidence_refs': [str(tmp_path / 'missing')]}]}
    with pytest.raises(ValueError, match='证据缺失'):
        validate_execution(result, payload, tmp_path)
    result['binding'] = {**result['binding'], 'repo_id': 'other'}
    with pytest.raises(ValueError, match='绑定'):
        validate_execution(result, payload, tmp_path)


def test_adapter_action_failure_cannot_be_scored_as_assertion_failure(tmp_path):
    spec = fixture_spec()
    payload = request(spec)
    evidence = tmp_path / 'evidence.json'
    evidence.write_text('{}')
    result = {'schema_version': VERSION, 'status': 'fail', 'passed': False, 'binding': payload['binding'],
        'cases': [{'id': 'complete', 'scenario_hash': digest(spec.behavior_scenarios[0]), 'status': 'fail',
                   'checks': [{'passed': None, 'failure_kind': 'action'}], 'evidence_refs': [str(evidence)]}]}
    with pytest.raises(ValueError, match='断言失败'):
        validate_execution(result, payload, tmp_path)
    with pytest.raises(ValueError, match='不确定'):
        oracle_result({'status': 'inconclusive'}, 'B01')


@pytest.mark.asyncio
async def test_required_missing_node_preserves_inconclusive(tmp_path):
    spec = fixture_spec()
    runner = PlaywrightAcceptanceRunner(node_command='tracefix-missing-node-command')
    result = await runner.run(spec, url='http://127.0.0.1:39991', artifacts_dir=tmp_path / 'new',
        reset_path='/__reset', binding=request(spec)['binding'], include_regression=False)
    assert result['status'] == 'inconclusive'
    assert result['passed'] is None
    assert (tmp_path / 'new/result.json').is_file()


def test_package_root_is_isolated():
    runner = PlaywrightAcceptanceRunner()
    assert (runner.package_root / 'package-lock.json').is_file()
    assert runner.package_root.name == 'interface-beta'


@pytest.mark.asyncio
async def test_docker_transport_rejects_network_from_other_run(tmp_path):
    async def docker(*args, **kwargs):
        return {'output': '[{"Internal":true,"Labels":{"tracefix.run":"other"}}]'}
    runner = PlaywrightAcceptanceRunner(docker_runner=SimpleNamespace(run_id='run', network='isolated-net',
                                       resolved_image_ids={'browser': 'sha256:fixed'}, docker=docker))
    with pytest.raises(ValueError, match='internal'):
        await runner._command(request(fixture_spec()), tmp_path / 'input.json', tmp_path)


def test_canonical_request_preserves_python_float_hash():
    spec = fixture_spec()
    spec.behavior_scenarios[0].steps[0].action.wait = ObservableWait(assertions=[], timeout_seconds=5.0)
    spec = RuntimeTestSpec.model_validate(spec.model_dump(mode='json'))
    payload = request(spec)
    assert '5.0' in payload['spec_canonical']
    assert '5.0' in payload['scenario_canonical']['complete']
