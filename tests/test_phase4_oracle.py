import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.isolation import AGENT_SURFACES, AgentExposure, DockerAgentBoundary, HostAgentBoundary, IsolationError
from evals.oracle_bridge import OracleBridge, acknowledge


class FixtureBoundary:
    def verify(self, private_root, private_files):
        return {'kind': 'fixture_only', 'real_isolation': False}


def bridge_fixture(tmp_path, passed=True, exit_code=None):
    private = tmp_path / 'evaluator'
    private.mkdir()
    report = private / 'hidden-report.json'
    script = private / 'hidden-oracle.py'
    result = {'case_id': 'B01', 'passed': passed, 'error': None if passed else 'secret failure',
              'steps': [{'name': 'secret refresh assertion', 'passed': passed}]}
    script.write_text(
        "import json, pathlib, sys\n"
        f"result = {result!r}\n"
        "pathlib.Path('hidden-report.json').write_text(json.dumps(result))\n"
        "print(json.dumps(result))\n"
        "print('secret stack trace', file=sys.stderr)\n"
        f"sys.exit({(0 if passed else 1) if exit_code is None else exit_code})\n",
        encoding='utf-8',
    )
    patch = tmp_path / 'candidate.patch'
    patch.write_bytes(b'candidate diff\n')
    patch_hash = hashlib.sha256(patch.read_bytes()).hexdigest()
    binding = {
        'run_id': 'run-first', 'group_id': 'single-off', 'case_id': 'B01',
        'source_revision': 'src-rev-1', 'source_hash': '1' * 64,
        'environment_digest': 'env-1', 'seed': 'seed-1', 'spec_hash': 'spec-1',
        'protocol_hash': 'protocol-1', 'patch_hash': patch_hash,
    }
    bridge = OracleBridge(private, (sys.executable, str(script)), script,
                          boundary=FixtureBoundary(), report_paths=(report,))
    return bridge, binding, patch, lambda: patch_hash


def private_rows(bridge):
    return [json.loads(line) for line in (bridge.private_root / 'ledger.jsonl').read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('passed', [True, False])
def test_score_captures_oracle_stdout_stderr_exit_and_report_privately(tmp_path, capsys, passed):
    bridge, binding, patch, verify = bridge_fixture(tmp_path, passed)
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    assert capsys.readouterr() == ('', '')
    record = private_rows(bridge)[0]
    assert record['oracle_passed'] is passed
    assert record['status'] == 'scored'
    assert record['returncode'] == (0 if passed else 1)
    assert 'secret refresh assertion' in record['stdout']
    assert 'secret stack trace' in record['stderr']
    assert 'secret refresh assertion' in record['reports']['hidden-report.json']
    assert record['binding'] == binding
    assert record['boundary']['real_isolation'] is False


@pytest.mark.parametrize('exception', [False, True])
def test_acknowledgement_hides_python_output_and_has_constant_exit(capsys, exception):
    def action():
        print('hidden success')
        print('hidden failure', file=sys.stderr)
        if exception:
            raise RuntimeError('hidden detail')
        return {'passed': True}
    assert acknowledge(action) == 0
    assert capsys.readouterr() == ('{"status":"settled"}\n', '')


@pytest.mark.parametrize('mismatch', ['requested', 'materialized', 'after'])
def test_patch_mismatch_never_reuses_pass(tmp_path, mismatch):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    if mismatch == 'requested':
        binding['patch_hash'] = '0' * 64
    elif mismatch == 'materialized':
        verify = lambda: '0' * 64
    else:
        values = iter((binding['patch_hash'], '0' * 64))
        verify = lambda: next(values)
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    record = private_rows(bridge)[0]
    assert record['status'] == 'binding_mismatch'
    assert record['oracle_passed'] is None
    if mismatch != 'after':
        assert not bridge.report_paths[0].exists()


@pytest.mark.parametrize('mode', ['empty', 'malformed', 'launch_failure', 'contradictory_exit', 'wrong_case'])
def test_missing_score_infra_and_case_mismatch_stay_private(tmp_path, capsys, mode):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    if mode == 'empty':
        bridge.oracle_script.write_text('print("public process completion")\n', encoding='utf-8')
    elif mode == 'malformed':
        bridge.oracle_script.write_text('print("{bad JSON secret}")\n', encoding='utf-8')
    elif mode == 'launch_failure':
        bridge.command = ('missing-executable-for-oracle', str(bridge.oracle_script))
    elif mode == 'contradictory_exit':
        bridge.oracle_script.write_text('import sys\nprint(\'{"case_id":"B01","passed":true}\')\nsys.exit(1)\n', encoding='utf-8')
    else:
        binding['case_id'] = 'B02'
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    assert capsys.readouterr() == ('', '')
    record = private_rows(bridge)[0]
    assert record['oracle_passed'] is None
    expected = ('missing_score' if mode in {'empty', 'malformed'} else
                'binding_mismatch' if mode == 'wrong_case' else 'infrastructure_error')
    assert record['status'] == expected


def test_host_agent_cannot_claim_isolation(tmp_path, capsys):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    bridge.boundary = HostAgentBoundary()
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    assert capsys.readouterr() == ('', '')
    record = private_rows(bridge)[0]
    assert record['status'] == 'isolation_error'
    assert record['oracle_passed'] is None
    assert not bridge.report_paths[0].exists()


def test_report_cannot_be_public_or_reused(tmp_path):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    bridge.report_paths = (tmp_path / 'agent-report.json',)
    bridge.score(binding, patch, verify_candidate=verify)
    assert private_rows(bridge)[0]['status'] == 'isolation_error'
    bridge.report_paths = (bridge.private_root / 'hidden-report.json',)
    bridge.report_paths[0].write_text('stale score', encoding='utf-8')
    bridge.score(binding, patch, verify_candidate=verify)
    assert private_rows(bridge)[1]['status'] == 'infrastructure_error'
    assert private_rows(bridge)[1]['oracle_passed'] is None


def test_same_scoring_request_is_settled_once(tmp_path):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    assert bridge.score(binding, patch, verify_candidate=verify) == {'status': 'settled'}
    rows = private_rows(bridge)
    assert rows[0]['status'] == 'scored'
    assert rows[1]['status'] == 'duplicate_request'
    assert len(rows) == 2


def exposure_fixture(tmp_path):
    public = tmp_path / 'agent'
    public.mkdir()
    return AgentExposure({surface: (public,) for surface in AGENT_SURFACES}, container_ids=('a' * 64,),
                         expected_image_ids={'a' * 64: 'sha256:' + 'b' * 64},
                         network_boundary_evidence='network-fixture',
                         cache_snapshot_evidence='cache-fixture',
                         retrieval_snapshot_evidence='retrieval-fixture')


def inspect_fixture(exposure):
    public = exposure.roots['workspace'][0]
    return [{'Id': exposure.container_ids[0], 'Image': 'sha256:' + 'b' * 64,
             'Config': {'User': '1000:1000'}, 'State': {'Running': True},
             'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                            'CapAdd': [], 'SecurityOpt': ['no-new-privileges:true'],
                            'PidMode': '', 'IpcMode': 'private', 'NetworkMode': 'internal-fixture'},
             'Mounts': [{'Type': 'bind', 'Source': str(public), 'Destination': '/workspace'}]}]


def stub_inspect(monkeypatch, records):
    monkeypatch.setattr('evals.isolation.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(records).encode(), stderr=b''))


def test_registered_container_mounts_checked_from_actual_inspect(tmp_path, monkeypatch):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    exposure = exposure_fixture(tmp_path)
    records = inspect_fixture(exposure)
    stub_inspect(monkeypatch, records)
    evidence = DockerAgentBoundary(exposure).verify(bridge.private_root, (bridge.oracle_script,))
    assert evidence['kind'] == 'docker_container_inspect'
    assert evidence['mounted_sources'] == [str(exposure.roots['workspace'][0])]


@pytest.mark.parametrize('surface', sorted(AGENT_SURFACES))
def test_each_agent_readback_surface_cannot_include_evaluator_root(tmp_path, surface):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    exposure = exposure_fixture(tmp_path)
    roots = dict(exposure.roots)
    roots[surface] = (tmp_path,)
    with pytest.raises(IsolationError, match='exposes evaluator'):
        AgentExposure(roots, container_ids=exposure.container_ids).check_paths(
            bridge.private_root, (bridge.oracle_script,))


@pytest.mark.parametrize('danger', ['mount_private', 'mount_socket', 'root', 'privileged', 'host_pid', 'copy', 'inventory', 'host_process'])
def test_container_boundary_rejects_readback_and_unisolated_execution(tmp_path, monkeypatch, danger):
    bridge, binding, patch, verify = bridge_fixture(tmp_path)
    exposure = exposure_fixture(tmp_path)
    records = inspect_fixture(exposure)
    if danger == 'mount_private':
        records[0]['Mounts'][0]['Source'] = str(bridge.private_root)
    elif danger == 'mount_socket':
        records[0]['Mounts'][0]['Source'] = str(tmp_path / 'docker.sock')
    elif danger == 'root':
        records[0]['Config']['User'] = 'root'
    elif danger == 'privileged':
        records[0]['HostConfig']['Privileged'] = True
    elif danger == 'host_pid':
        records[0]['HostConfig']['PidMode'] = 'host'
    elif danger == 'copy':
        (exposure.roots['workspace'][0] / 'innocent.txt').write_bytes(bridge.oracle_script.read_bytes())
    elif danger == 'inventory':
        exposure = AgentExposure({'workspace': exposure.roots['workspace']}, container_ids=exposure.container_ids,
                                 expected_image_ids=exposure.expected_image_ids,
                                 network_boundary_evidence=exposure.network_boundary_evidence,
                                 cache_snapshot_evidence=exposure.cache_snapshot_evidence,
                                 retrieval_snapshot_evidence=exposure.retrieval_snapshot_evidence)
    else:
        exposure = AgentExposure(exposure.roots, container_ids=exposure.container_ids, host_process_ids=(123,),
                                 expected_image_ids=exposure.expected_image_ids,
                                 network_boundary_evidence=exposure.network_boundary_evidence,
                                 cache_snapshot_evidence=exposure.cache_snapshot_evidence,
                                 retrieval_snapshot_evidence=exposure.retrieval_snapshot_evidence)
    stub_inspect(monkeypatch, records)
    with pytest.raises(IsolationError):
        DockerAgentBoundary(exposure).verify(bridge.private_root, (bridge.oracle_script,))
