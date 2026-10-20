import pytest

from tracefix.runtime.contracts import digest
from tracefix.runtime.harness_contracts import HarnessFeedback, VerificationRecord
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, MemoryHarnessStore

from tests.test_harness_store import accept, candidate, coder, initialized


@pytest.fixture
def ledger(tmp_path):
    return MemoryHarnessStore(Artifacts(tmp_path / 'artifacts'))


def test_failed_feedback_requires_actual_failure_evidence_and_new_invocation(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'first-input',
        scope_id='scope', owner='owner')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=execution.revision,
        owner='owner', owner_epoch=session.owner_epoch, status='FAILED')
    session = ledger.bind_candidate(session.session_id, scope_id='scope', expected_revision=session.revision,
        owner='owner', owner_epoch=session.owner_epoch, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan')
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)
    evidence_ref = ledger.artifacts.put('scope', session.run_id, {
        **identity, 'kind': 'verification_evidence', 'candidate_commit': 'b' * 40,
        'source_manifest': 'source', 'patch_hash': 'patch', 'environment_digest': 'env',
        'test_spec_hash': 'spec', 'check_plan_hash': 'plan', 'command': ['pytest'], 'exit_code': 1,
    })
    verification = VerificationRecord(**identity, input_commit=session.input_commit, candidate_commit='b' * 40,
        source_manifest='source', patch_hash='patch', environment_digest='env', test_spec_hash='spec',
        check_plan_hash='plan', acceptance_refs=['original'], commands=[{
            'command': ['pytest'], 'exit_code': 1, 'evidence_ref': evidence_ref}], passed=False)
    verification_ref = ledger.artifacts.put('scope', session.run_id, verification.model_dump())
    feedback_ref = ledger.artifacts.put('scope', session.run_id, HarnessFeedback(**identity,
        execution_id=execution.execution_id, candidate_commit='b' * 40, source_manifest='source',
        verification_ref=verification_ref, summary='失败命令 pytest 返回 1').model_dump())
    with pytest.raises(HarnessConflict):
        ledger.allocate_invocation(session.session_id, scope_id='scope', expected_revision=session.revision)
    resumed = ledger.allocate_invocation(session.session_id, scope_id='scope', expected_revision=session.revision,
        feedback_ref=feedback_ref)
    assert resumed.invocation_no == 2 and resumed.feedback_ref == feedback_ref


def test_final_failure_blocks_without_losing_accepted_increment(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    accepted = accept(ledger, session, verification_ref, handoff_ref)
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)
    evidence_ref = ledger.artifacts.put('scope', session.run_id, {
        **identity, 'kind': 'verification_evidence', 'candidate_commit': accepted.accepted_commit,
        'source_manifest': 'final-source', 'patch_hash': 'final-patch', 'environment_digest': 'env',
        'test_spec_hash': 'spec', 'check_plan_hash': 'plan', 'command': ['pytest', 'all'], 'exit_code': 1,
    })
    final = VerificationRecord(**identity, input_commit='a' * 40, candidate_commit=accepted.accepted_commit,
        source_manifest='final-source', patch_hash='final-patch', environment_digest='env', test_spec_hash='spec',
        check_plan_hash='plan', acceptance_refs=['original'], commands=[{
            'command': ['pytest', 'all'], 'exit_code': 1, 'evidence_ref': evidence_ref}], passed=False,
        target_commit='c' * 40)
    blocked = ledger.finalize_job('job', ledger.artifacts.put('scope', session.run_id, final.model_dump()),
        scope_id='scope', expected_revision=accepted.revision, session_id=session.session_id)
    assert blocked.status == 'BLOCKED' and blocked.blocker == 'FINAL_ACCEPTANCE_FAILED'
    assert blocked.accepted_commit == accepted.accepted_commit
