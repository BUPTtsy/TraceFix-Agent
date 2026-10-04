from types import SimpleNamespace

from tracefix.runtime.contracts import Validation, digest
from tracefix.runtime.validation_feedback import (
    build_error_feedback,
    build_validation_feedback,
    feedback_is_current,
)
from tracefix.runtime.verification import verification_binding


def state():
    return SimpleNamespace(scope_id='scope', run_id='run', source_manifest='source',
        patch_hash='patch', environment_digest='env', test_spec_hash='spec',
        patch_ref='candidate.json')


def bundle(passed=False, *, kind='original', error_code=None):
    current = state()
    plan_hash = 'plan'
    binding = verification_binding(current, plan_hash)
    candidate = {'type': 'patch_candidate', **binding, 'candidate': 'candidate'}
    result = {'type': 'validation_result', 'kind': kind, **binding, 'passed': passed}
    png = b'\x89PNG\r\n\x1a\npublic-development-fixture'
    observation = {'type': 'gui_observation', **binding, 'snapshot': 'checkbox "Task"',
                   'screenshot_ref': 'screenshot.png', 'screenshot_hash': digest(png)}
    if kind in {'original', 'regression'}:
        result.update(observation_ref='observation.json', observation_hash=digest(observation),
            assertions=[{'assertion': {'condition': 'checked'}, 'matches': 1, 'passed': passed}])
    elif kind in {'static', 'unit', 'build'}:
        result.update(exit_code=0 if passed else 1, output='public command output')
    if error_code:
        result['error_code'] = error_code
    validation = Validation(kind=kind, passed=passed, scope_id=current.scope_id,
        run_id=current.run_id, source_manifest=current.source_manifest,
        patch_hash=current.patch_hash, environment_digest=current.environment_digest,
        test_spec_hash=current.test_spec_hash, artifact_ref='validation.json',
        replay_plan_hash=plan_hash)
    validation = validation.model_copy(update={'artifact_ref': 'result.json'})
    artifacts = {'candidate.json': candidate, 'validation.json': validation.model_dump(mode='json'),
                 'result.json': result, 'observation.json': observation, 'screenshot.png': png}
    return current, plan_hash, validation, artifacts


def readers(artifacts):
    def exists(ref):
        return ref in artifacts
    def read(ref):
        return artifacts[ref]
    return exists, read


def make_feedback(bundle_data):
    current, plan_hash, validation, artifacts = bundle_data
    exists, read = readers(artifacts)
    return build_validation_feedback(current, validation, artifact_exists=exists,
        artifact_read=read, artifact_read_bytes=read, replay_plan_hash=plan_hash,
        validation_ref='validation.json')


def test_failed_validation_routes_counterevidence_and_keeps_candidate():
    feedback = make_feedback(bundle(False, kind='original'))
    assert feedback.status == 'failed'
    assert feedback.failure_class == 'counterevidence'
    assert feedback.next_phase == 'diagnose'
    assert feedback.failed_candidate is True
    assert feedback.failure_signature
    assert 'candidate.json' in feedback.actual_refs
    assert {'observation.json', 'screenshot.png'} <= set(feedback.actual_refs)


def test_precondition_and_editing_feedback_route_to_expected_phase():
    precondition = make_feedback(bundle(False, kind='original', error_code='PRECONDITION_FAILED'))
    assert (precondition.failure_class, precondition.next_phase) == ('precondition', 'reproduce')
    editing = make_feedback(bundle(False, kind='static', error_code='ANCHOR_AMBIGUOUS'))
    assert (editing.failure_class, editing.next_phase) == ('editing', 'edit')


def test_recovery_feedback_does_not_replay_unknown_operation():
    feedback = make_feedback(bundle(False, kind='health', error_code='WAIT_TIMEOUT'))
    assert (feedback.failure_class, feedback.next_phase) == ('recovery', 'recover')
    assert feedback.error_code == 'WAIT_TIMEOUT'
    assert '恢复' in feedback.instruction


def test_pass_is_public_report_only_and_binding_drift_cannot_pass():
    data = bundle(True)
    feedback = make_feedback(data)
    assert feedback.status == 'reported_pass'
    current, plan_hash, validation, artifacts = data
    artifacts['candidate.json']['patch_hash'] = 'other-patch'
    drifted = make_feedback(data)
    assert drifted.status == 'stale'
    assert not feedback_is_current(feedback, current, replay_plan_hash=plan_hash,
        artifact_exists=lambda ref: ref in artifacts, artifact_read=lambda ref: artifacts[ref])


def test_missing_evidence_is_unavailable_and_wrong_candidate_pass_not_reused():
    current, plan_hash, validation, artifacts = bundle(True)
    artifacts.pop('candidate.json')
    exists, read = readers(artifacts)
    feedback = build_validation_feedback(current, validation, artifact_exists=exists,
        artifact_read=read, replay_plan_hash=plan_hash, validation_ref='validation.json')
    assert feedback.status == 'unavailable'
    assert feedback.missing_refs == ['candidate.json']


def test_error_feedback_binds_error_and_candidate_refs():
    current = state()
    details = {'error_code': 'STALE_BASE', 'failure_category': 'editing'}
    artifacts = {'error.json': details, 'candidate.json': {'content': 'old'}}
    exists, read = readers(artifacts)
    feedback = build_error_feedback(current, details, error_ref='error.json',
        artifact_exists=exists, artifact_read=read, replay_plan_hash='plan')
    assert feedback.status == 'failed'
    assert feedback.failure_class == 'editing'
    assert feedback.next_phase == 'edit'
    assert digest(details) == feedback.artifact_hashes['error.json']


def test_gui_pass_without_observation_or_with_drifted_snapshot_is_unavailable():
    data = bundle(True)
    data[3]['result.json'].pop('observation_ref')
    assert make_feedback(data).status == 'unavailable'
    data = bundle(True)
    data[3]['observation.json']['snapshot'] = 'changed snapshot'
    feedback = make_feedback(data)
    assert feedback.status == 'stale'
    assert 'observation.json:observation_hash' in feedback.binding_mismatches


def test_failure_signature_ignores_new_artifact_ids_and_run_id():
    first = make_feedback(bundle(False))
    data = bundle(False)
    data[0].run_id = 'later-run'
    for value in data[3].values():
        if type(value) is dict and 'run_id' in value:
            value['run_id'] = 'later-run'
    data[3]['result.json']['observation_hash'] = digest(data[3]['observation.json'])
    data[3]['extra.json'] = data[3].pop('result.json')
    data = (data[0], data[1], data[2].model_copy(update={'artifact_ref': 'extra.json',
                                                  'run_id': 'later-run'}), data[3])
    data[3]['validation.json'] = data[2].model_dump(mode='json')
    assert make_feedback(data).failure_signature == first.failure_signature


def test_distinct_command_errors_keep_distinct_failure_semantics():
    first = bundle(False, kind='unit')
    second = bundle(False, kind='unit')
    first[3]['result.json']['output'] = 'expected stored done=false, actual done=true'
    second[3]['result.json']['output'] = 'expected account alpha, actual account beta'
    assert make_feedback(first).failure_signature != make_feedback(second).failure_signature


def test_explicit_non_public_provenance_cannot_be_feedback():
    for marker in [{'source': 'held_out'}, {'final_scoring_only': True}, {'learnable': False}]:
        data = bundle(True)
        data[3]['result.json'].update(marker)
        feedback = make_feedback(data)
        assert feedback.status == 'stale'
        assert feedback.binding_mismatches == ['non_public_evidence']
