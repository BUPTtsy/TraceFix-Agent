from copy import deepcopy

import pytest
from pydantic import ValidationError

from tracefix.runtime.contracts import Outcome, RunState, RunStatus, digest
from tracefix.service.contracts import ServiceRequest
from tracefix.service.results import ServiceError, preserve_report_diff, serialize_result
from tracefix.storage.artifacts import Artifacts


def request_payload():
    repository = {'repo_id': 'alpha', 'projectId': 'alpha', 'commit': 'a' * 40,
        'owner': 'owner', 'repo': 'app', 'default_ref': 'main',
        'credential_ref': 'deployment/alpha', 'allowed_files': ['src/**']}
    repository['config_digest'] = digest(repository)
    repository['frozen_run_id'] = 'run_alpha'
    repository['snapshot_digest'] = digest(repository)
    return {'schema_version': 1, 'task_id': 'task_alpha', 'run_id': 'run_alpha', 'repo_id': 'alpha',
        'mode': 'test', 'goal': '检查页面操作结果', 'execution': {'mode': 'batch'},
        'repository_snapshot': {'repo_id': 'alpha', 'projectId': 'alpha', 'commit': 'a' * 40,
            'config_digest': repository['config_digest'], 'repository_config': repository,
            'projects_path': 'frozen/projects.yaml', 'profile_path': 'frozen/alpha.yaml'}}


@pytest.mark.parametrize('patch', [
    {'execution': {'mode': 'interactive'}}, {'execution': {'mode': 'batch', 'command': ['evil']}},
    {'github_reads': [{'source_kind': 'file', 'path': 'src/a.ts', 'owner': 'evil'}]},
    {'github_reads': [{'source_kind': 'issue', 'number': True}]},
    {'github_reads': [{'source_kind': 'commit', 'path': 'src/a.ts'}]},
    {'token': 'private'}, {'goal': 'ghp_' + 'a' * 40}, {'goal': 'sk-' + 'a' * 40},
    {'finding_acceptance': {'cookie': 'private'}}, {'run_id': 'other'}, {'repo_id': 'other'},
    {'schema_version': True}, {'schema_version': '1'},
    {'source_run_id': 'source'}, {'additional_rule_ids': ['rule', 'rule']},
])
def test_service_rejects_uncontrolled_requests(patch):
    with pytest.raises(ValidationError):
        ServiceRequest.model_validate({**request_payload(), **patch})


def test_frozen_snapshot_is_validated_before_execution():
    payload = request_payload()
    assert ServiceRequest.model_validate(payload).execution.mode == 'batch'
    for key in ('owner', 'commit', 'frozen_run_id'):
        changed = deepcopy(payload)
        changed['repository_snapshot']['repository_config'][key] = 'forged'
        with pytest.raises(ValidationError):
            ServiceRequest.model_validate(changed)


def test_regular_test_does_not_require_github_credential_reference():
    payload = request_payload()
    snapshot = payload['repository_snapshot']
    repository = snapshot['repository_config']
    repository['credential_ref'] = None
    repository['config_digest'] = digest({key: value for key, value in repository.items()
        if key not in {'config_digest', 'frozen_run_id', 'snapshot_digest'}})
    repository['snapshot_digest'] = digest({key: value for key, value in repository.items() if key != 'snapshot_digest'})
    snapshot['config_digest'] = repository['config_digest']
    assert ServiceRequest.model_validate(payload).github_reads == []


def test_result_reads_integrity_checked_authoritative_report(tmp_path):
    artifacts = Artifacts(tmp_path)
    state = RunState(run_id='run_alpha', scope_id='alpha', goal='检查页面操作结果', url='http://app',
                     run_status=RunStatus.COMPLETED, outcome=Outcome.FIX_VERIFIED)
    diff = 'diff --git a/src/a.ts b/src/a.ts\n+fixed\n'
    state.patch_hash = digest(diff.encode())
    reference = artifacts.put('alpha', state.run_id, diff, 'diff')
    report = {'run_id': state.run_id, 'scope_id': 'alpha', 'run_status': 'COMPLETED',
        'outcome': 'FIX_VERIFIED', 'patch_diff_ref': reference, 'patch_available': True,
        'patch_verification': 'verified'}
    state.report_ref = artifacts.put('alpha', state.run_id, report)
    result = serialize_result(state, artifacts, task_id='task_alpha', repo_id='alpha')
    assert result['diff'] == diff and result['patch_verification'] == 'verified'
    state.patch_hash = 'forged'
    with pytest.raises(ServiceError, match='补丁报告'):
        serialize_result(state, artifacts, task_id='task_alpha', repo_id='alpha')


def test_service_preserves_raw_crlf_diff_without_weakening_patch_gate(tmp_path):
    from types import SimpleNamespace
    artifacts = Artifacts(tmp_path)
    diff = 'diff --git a/src/a.ts b/src/a.ts\n-old\r\n+fixed\n'
    state = RunState(run_id='run_alpha', scope_id='alpha', goal='检查页面操作结果', url='http://app',
                     run_status=RunStatus.COMPLETED, outcome=Outcome.FIX_VERIFIED)
    state.patch_hash = digest(diff.encode())
    reference = artifacts.put('alpha', state.run_id, diff, 'diff')
    state.report_ref = artifacts.put('alpha', state.run_id, {'run_id': state.run_id, 'scope_id': 'alpha',
        'run_status': 'COMPLETED', 'outcome': 'FIX_VERIFIED', 'patch_diff_ref': reference,
        'patch_available': True, 'patch_verification': 'verified'})
    workspace = SimpleNamespace(diff=lambda base: diff)
    preserve_report_diff(state, artifacts, workspace)
    result = serialize_result(state, artifacts, task_id='task_alpha', repo_id='alpha')
    assert result['diff'] == diff and digest(result['diff'].encode()) == state.patch_hash
    with pytest.raises(ServiceError):
        preserve_report_diff(state, artifacts, SimpleNamespace(diff=lambda base: diff + 'forged'))
    state.run_status = RunStatus.WAITING_APPROVAL
    with pytest.raises(ServiceError, match='终态'):
        serialize_result(state, artifacts, task_id='task_alpha', repo_id='alpha')


@pytest.mark.parametrize('text', ['token=private', 'sk-' + 'a' * 40, 'text\x1b[31m', 'text\x00'])
def test_raw_diff_export_rejects_redacted_or_control_text(tmp_path, text):
    from types import SimpleNamespace
    artifacts = Artifacts(tmp_path)
    diff = 'diff --git a/src/a.ts b/src/a.ts\n+' + text + '\r\n'
    state = RunState(run_id='run_alpha', scope_id='alpha', goal='检查页面操作结果', url='http://app',
                     run_status=RunStatus.COMPLETED, outcome=Outcome.INCONCLUSIVE)
    reference = artifacts.put('alpha', state.run_id, diff, 'diff')
    state.report_ref = artifacts.put('alpha', state.run_id, {'run_id': state.run_id, 'scope_id': 'alpha',
        'run_status': 'COMPLETED', 'outcome': 'INCONCLUSIVE', 'patch_diff_ref': reference,
        'patch_available': True, 'patch_verification': 'unverified'})
    original = state.report_ref
    with pytest.raises(ServiceError) as error:
        preserve_report_diff(state, artifacts, SimpleNamespace(diff=lambda base: diff))
    assert error.value.code == 'patch_export_redacted' and state.report_ref == original
