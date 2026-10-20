from dataclasses import asdict
from types import SimpleNamespace

import pytest

from tracefix.runtime.continuation import derive_run
from tracefix.runtime.contracts import Outcome, RunState, digest
from tracefix.runtime.finding_acceptance import FrozenFindingAcceptance, _validate_current, freeze_finding_acceptance
from tracefix.runtime.harness_finding import carry_finding_acceptance, validate_finding_service_factory
from tracefix.service.contracts import ServiceRequest
from tracefix.service.finding import RunBoundFindings, strict_acceptance_adapter
from tracefix.service.resources import RunServices
from tests.test_finding_acceptance import RecordingRunner, acceptance_fixture as _acceptance_fixture


@pytest.fixture
def finding_case(tmp_path):
    parent, payload, artifacts = _acceptance_fixture.__wrapped__(tmp_path)
    parent.test_spec_ref = artifacts.put(parent.scope_id, parent.run_id, payload['spec'])
    parent.test_spec_hash = payload['spec_hash']
    frozen = freeze_finding_acceptance(payload, parent, {'commit': 'a' * 40})
    parent.finding_acceptance_ref = artifacts.put(parent.scope_id, parent.run_id, asdict(frozen))
    parent.finding_acceptance_hash = frozen.acceptance_hash
    parent.finding_reproduction_ref = 'parent-reproduction.json'
    parent.finding_verification_ref = 'parent-verification.json'
    parent.finding_acceptance_evidence_refs = ['parent-proof.json']

    def put(state, value, **options):
        return artifacts.put(state.scope_id, state.run_id, value)

    engine = SimpleNamespace(artifacts=artifacts,
        get=lambda state, reference: artifacts.json(state.scope_id, state.run_id, reference), put=put)
    source = {'scope_id': parent.scope_id, 'repo_id': parent.repo_id, 'commit': 'a' * 40}
    return engine, parent, derive_run(parent), source


def test_non_finding_child_drops_inherited_acceptance_refs():
    parent = RunState(scope_id='scope', goal='goal', url='http://app')
    child = derive_run(parent).model_copy(update={'finding_acceptance_ref': 'old.json',
        'finding_acceptance_hash': digest('old'), 'finding_reproduction_ref': 'proof.json',
        'finding_verification_ref': 'verification.json', 'finding_acceptance_evidence_refs': ['evidence.json']})
    result = carry_finding_acceptance(SimpleNamespace(), parent, child, {})
    assert not result.finding_acceptance_ref
    assert not result.finding_reproduction_ref
    assert not result.finding_verification_ref
    assert not result.finding_acceptance_evidence_refs


def test_same_revision_regenerates_acceptance_and_spec_without_old_success(finding_case):
    engine, parent, child, source = finding_case
    result = carry_finding_acceptance(engine, parent, child, source)
    assert result.repair_source == parent.repair_source
    assert not result.finding_reproduction_ref
    assert not result.finding_verification_ref
    assert not result.finding_acceptance_evidence_refs
    frozen = FrozenFindingAcceptance(**engine.get(result, result.finding_acceptance_ref))
    accepted = _validate_current(frozen, result, engine.artifacts)
    assert accepted.run_id == result.run_id
    assert accepted.spec_hash == parent.test_spec_hash
    assert engine.get(result, result.test_spec_ref) == engine.get(parent, parent.test_spec_ref)


def test_changed_revision_requires_original_explicit_authorization(finding_case):
    engine, parent, child, source = finding_case
    source['commit'] = 'b' * 40
    with pytest.raises(PermissionError, match='allow_revision_rebind'):
        carry_finding_acceptance(engine, parent, child, source)
    frozen = FrozenFindingAcceptance(**engine.get(parent, parent.finding_acceptance_ref))
    payload = frozen.acceptance.model_dump(mode='json')
    payload['allow_revision_rebind'] = True
    original = freeze_finding_acceptance(payload, parent, {'commit': 'a' * 40})
    parent.finding_acceptance_ref = engine.put(parent, asdict(original))
    parent.finding_acceptance_hash = original.acceptance_hash
    result = carry_finding_acceptance(engine, parent, child, source)
    assert result.repair_source.revision_rebound
    assert result.repair_source.target_base_commit == 'b' * 40
    rebound = FrozenFindingAcceptance(**engine.get(result, result.finding_acceptance_ref))
    assert rebound.acceptance.findings == original.acceptance.findings
    assert rebound.acceptance.spec == original.acceptance.spec


def test_parent_bound_service_cannot_be_reused_for_child(finding_case):
    engine, parent, child, source = finding_case
    engine.runtime_services = SimpleNamespace(run_id=parent.run_id, finding_acceptance=object())
    with pytest.raises(PermissionError, match='专属验收服务'):
        carry_finding_acceptance(engine, parent, child, source)


def test_damaged_source_evidence_is_rejected(finding_case, monkeypatch):
    engine, parent, child, source = finding_case
    original = engine.artifacts.read
    source_ref = parent.repair_source.source_evidence_refs[0]

    def read(scope, run, reference):
        return b'tampered' if run == parent.repair_source.source_run_id and reference == source_ref else original(scope, run, reference)

    monkeypatch.setattr(engine.artifacts, 'read', read)
    with pytest.raises(ValueError, match='证据已经变化'):
        carry_finding_acceptance(engine, parent, child, source)


def bind_services(engine, parent, tmp_path, *, runner_factory=None, github_reads=None):
    frozen = FrozenFindingAcceptance(**engine.get(parent, parent.finding_acceptance_ref))
    repository = {'repo_id': parent.scope_id, 'projectId': parent.scope_id,
        'owner': 'owner', 'repo': 'repo', 'commit': 'a' * 40,
        'profile': {'source_commit': 'a' * 40}, 'frozen_run_id': parent.run_id}
    repository['config_digest'] = digest({key: value for key, value in repository.items()
        if key != 'frozen_run_id'})
    repository['snapshot_digest'] = digest(repository)
    snapshot = {'repo_id': parent.scope_id, 'projectId': parent.scope_id,
        'commit': 'a' * 40, 'repository_config': repository,
        'config_digest': repository['config_digest'], 'snapshot': dict(repository),
        'projects_path': 'projects.yaml', 'profile_path': 'profile.yaml'}
    request = ServiceRequest.model_validate({'schema_version': 1, 'task_id': 'task',
        'run_id': parent.run_id, 'repo_id': parent.scope_id, 'mode': 'repair',
        'goal': parent.goal, 'execution': {'spec': frozen.acceptance.spec.model_dump(mode='json')},
        'repository_snapshot': snapshot, 'source_run_id': frozen.acceptance.source_run_id,
        'source_finding_ids': [item.finding_id for item in frozen.acceptance.findings],
        'finding_acceptance': frozen.acceptance.model_dump(mode='json'),
        'github_reads': github_reads or []})
    options = {'acceptance_runner_factory': runner_factory} if runner_factory else {}
    services = RunServices(request, SimpleNamespace(data_root=tmp_path), **options)
    services.finding_acceptance = strict_acceptance_adapter(request, parent, snapshot)
    services.finding_acceptance.reproduction_ref = 'parent-reproduction.json'
    services.finding_acceptance.verification_ref = 'parent-verification.json'
    services.bind_session(SimpleNamespace(engine=engine, artifacts=engine.artifacts))
    engine.runtime_services = services
    return services


async def test_real_services_are_reconstructed_and_execute_new_child_evidence(finding_case, tmp_path, monkeypatch):
    engine, parent, child, source = finding_case
    baseline, verification = RecordingRunner(), RecordingRunner(baseline=False)
    runners = iter([baseline, verification])
    services = bind_services(engine, parent, tmp_path,
        runner_factory=lambda **options: next(runners))
    original = services.request.model_dump(mode='json')
    library = object()
    engine.rule_library = RunBoundFindings(library, services)
    engine.runner = object()
    monkeypatch.setenv('TRACEFIX_SERVICE_ACCEPTANCE_RESET_PATH', '/reset')
    result = carry_finding_acceptance(engine, parent, child, source)
    derived = engine.runtime_services
    result.source_manifest = digest(source)
    result.environment_digest = digest('independent child environment')
    assert derived is not services and derived.session is not services.session
    assert derived.request.run_id == result.run_id == derived.run_id
    assert derived.request.repository_snapshot.repository_config['frozen_run_id'] == result.run_id
    ServiceRequest.model_validate(derived.request.model_dump(mode='json'))
    assert derived.request.source_run_id == services.request.source_run_id
    assert derived.request.source_finding_ids == services.request.source_finding_ids
    assert derived.finding_acceptance.reproduction_ref is None
    assert derived.finding_acceptance.verification_ref is None
    assert engine.rule_library.library is library and engine.rule_library.services is derived
    assert await derived.reference_documents(result) == []
    assert not derived.finding_acceptance_reproduced(result)
    reproduction = await derived.reproduce_finding_acceptance(result)
    assert reproduction.outcome == Outcome.BUG_CONFIRMED
    assert len(baseline.calls) == 1 and baseline.calls[0][1]['run_id'] == result.run_id
    result.patch_hash = digest('child candidate')
    verified = await derived.verify_finding_acceptance(result)
    assert verified.outcome == Outcome.FIX_VERIFIED
    assert len(verification.calls) == 1 and derived.finding_acceptance_verified(result)
    assert services.request.model_dump(mode='json') == original
    assert services.finding_acceptance.reproduction_ref == 'parent-reproduction.json'
    assert derived._resources is not services._resources
    await derived.aclose()
    assert services._close_task is None and services.session.engine is engine
    await services.aclose()


def test_child_service_rebinds_authorized_revision_and_snapshot_digest(finding_case, tmp_path):
    engine, parent, child, source = finding_case
    frozen = FrozenFindingAcceptance(**engine.get(parent, parent.finding_acceptance_ref))
    payload = frozen.acceptance.model_dump(mode='json')
    payload['allow_revision_rebind'] = True
    authorized = freeze_finding_acceptance(payload, parent, {'commit': 'a' * 40})
    parent.finding_acceptance_ref = engine.put(parent, asdict(authorized))
    parent.finding_acceptance_hash = authorized.acceptance_hash
    services = bind_services(engine, parent, tmp_path)
    source['commit'] = 'b' * 40
    result = carry_finding_acceptance(engine, parent, child, source)
    snapshot = engine.runtime_services.request.repository_snapshot
    assert snapshot.commit == 'b' * 40
    assert snapshot.repository_config['profile']['source_commit'] == 'b' * 40
    assert snapshot.repository_config['snapshot_digest'] != services.request.repository_snapshot.repository_config['snapshot_digest']
    assert engine.runtime_services.finding_acceptance.allow_revision_rebind
    assert engine.runtime_services.finding_acceptance.frozen.acceptance.findings == authorized.acceptance.findings
    assert result.repair_source.source_base_commit == 'a' * 40


@pytest.mark.parametrize('condition', ['github', 'resource', 'foreign_session'])
def test_unreconstructable_services_are_rejected_before_rebinding(finding_case, tmp_path, condition):
    engine, parent, child, source = finding_case
    reads = [{'source_kind': 'commit'}] if condition == 'github' else []
    services = bind_services(engine, parent, tmp_path, github_reads=reads)
    if condition == 'resource':
        services.own(object())
    elif condition == 'foreign_session':
        services.session.engine = object()
    with pytest.raises(PermissionError):
        validate_finding_service_factory(engine, parent)
    with pytest.raises(PermissionError):
        carry_finding_acceptance(engine, parent, child, source)
    assert engine.runtime_services is services
