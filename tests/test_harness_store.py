from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
import uuid

import pytest

from tracefix.runtime.contracts import digest
from tracefix.runtime.harness_contracts import (HarnessJob, HarnessSession, HarnessWorkItem,
    InitializationManifest, TaskPlan, SessionHandoff, VerificationRecord, VerificationCommand,
    HarnessFeedback, VerificationEvidence, ExecutionCheckpoint)
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, MemoryHarnessStore, PostgresHarnessStore
from tracefix.storage.store import PostgresStore


def database_url():
    configured = os.environ.get('TRACEFIX_DATABASE_URL')
    if configured:
        return configured
    environment = Path(__file__).resolve().parents[1] / '.env'
    if environment.is_file():
        for line in environment.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('TRACEFIX_DATABASE_URL='):
                return line.split('=', 1)[1].strip().strip('"\'')
    return 'postgresql://tracefix:tracefix@127.0.0.1:55432/tracefix'


@pytest.fixture
def postgres_harness_pair(tmp_path):
    import psycopg
    from psycopg import sql

    try:
        admin = psycopg.connect(database_url(), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip('PostgreSQL unavailable; real transaction gate not executed')
    extension = admin.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n "
        "ON n.oid=e.extnamespace WHERE e.extname='vector'").fetchone()
    if not extension:
        admin.close()
        pytest.skip('Existing pgvector extension required')
    schema = 'harness_' + uuid.uuid4().hex
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    stores = []
    try:
        artifacts = Artifacts(tmp_path / 'artifacts')
        for _index in range(2):
            store = PostgresStore(database_url())
            stores.append(store)
            store.conn.execute(sql.SQL('SET search_path TO {}, {}').format(sql.Identifier(schema), sql.Identifier(extension[0])))
            store.setup()
            store.setup()
        yield [PostgresHarnessStore(store, artifacts) for store in stores]
    finally:
        for store in stores:
            store.close()
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture(params=['memory', 'postgres'])
def ledger(request, tmp_path):
    if request.param == 'postgres':
        return request.getfixturevalue('postgres_harness_pair')[0]
    return MemoryHarnessStore(Artifacts(tmp_path / 'artifacts'))


def initialized(ledger, *, two=False):
    job = ledger.create_job(HarnessJob(job_id='job', scope_id='scope', repo_id='repo', console_task_id='console',
        goal_hash=digest('goal'), origin_base_commit='a' * 40, acceptance_refs=['original'], local_commit_authorized=True))
    session = ledger.create_session(HarnessSession(session_id='init', job_id=job.job_id, scope_id='scope',
        kind='initializer', run_id='run-init', input_commit=job.accepted_commit), scope_id='scope', expected_revision=job.revision)
    identity = dict(scope_id='scope', job_id=job.job_id, session_id=session.session_id, run_id=session.run_id)
    manifest = InitializationManifest(**identity, goal_hash=job.goal_hash, policy_version='policy/1',
        input_commit=job.accepted_commit, environment_digest='env', commands=[], healthy=True)
    reference = ledger.artifacts.put('scope', session.run_id, manifest.model_dump())
    job = ledger.finish_initialization(session.session_id, reference, scope_id='scope', expected_revision=session.revision)
    items = [HarnessWorkItem(harness_task_id='one', job_id='job', scope_id='scope', title='修复',
        acceptance_refs=['original'], allowed_files=['src/**'], writable_files=['src/**'])]
    if two:
        items.append(items[0].model_copy(update={'harness_task_id': 'two', 'depends_on': ['one']}))
    plan = TaskPlan(**identity, plan_version=1, acceptance_refs=['original'], work_items=items)
    reference = ledger.artifacts.put('scope', session.run_id, plan.model_dump())
    return ledger.install_plan(job.job_id, reference, scope_id='scope', expected_revision=job.revision)


def coder(ledger, *, two=False):
    job = initialized(ledger, two=two)
    item = ledger.claim_work_item(job.job_id, job.revision, 'owner', scope_id='scope')
    job = ledger.get_job('job', scope_id='scope')
    session = ledger.create_session(HarnessSession(session_id='coder', job_id='job', scope_id='scope',
        kind='coder', harness_task_id=item.harness_task_id, run_id='run-coder', input_commit=job.accepted_commit),
        scope_id='scope', expected_revision=job.revision)
    return ledger.allocate_invocation(session.session_id, scope_id='scope', expected_revision=session.revision)


def candidate(ledger, **verification_updates):
    session = coder(ledger)
    session = ledger.bind_candidate(session.session_id, scope_id='scope', expected_revision=session.revision,
        owner='owner', owner_epoch=session.owner_epoch, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan')
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)
    evidence = ledger.artifacts.put('scope', session.run_id, VerificationEvidence(**identity,
        candidate_commit='b' * 40, source_manifest='source', patch_hash='patch', environment_digest='env',
        test_spec_hash='spec', check_plan_hash='plan', command=['pytest'], exit_code=0).model_dump())
    record = VerificationRecord(**identity, input_commit=session.input_commit, candidate_commit='b' * 40,
        source_manifest='source', patch_hash='patch', environment_digest='env', test_spec_hash='spec',
        check_plan_hash='plan', acceptance_refs=['original'],
        commands=[VerificationCommand(command=['pytest'], exit_code=0, evidence_ref=evidence)], passed=True)
    record = record.model_copy(update=verification_updates)
    verification_ref = ledger.artifacts.put('scope', session.run_id, record.model_dump())
    handoff = SessionHandoff(**identity, input_commit=session.input_commit, candidate_commit='b' * 40,
        source_manifest='source', patch_hash='patch', environment_digest='env', verification_ref=verification_ref)
    handoff_ref = ledger.artifacts.put('scope', session.run_id, handoff.model_dump())
    return session, verification_ref, handoff_ref


def accept(ledger, session, verification_ref, handoff_ref):
    return ledger.accept_increment(session.session_id, session.revision, verification_ref, 'b' * 40, handoff_ref,
        scope_id='scope', owner='owner', owner_epoch=session.owner_epoch)


def test_dependency_claim_and_expired_lease_do_not_create_second_writer(ledger):
    job = initialized(ledger, two=True)
    item = ledger.claim_work_item('job', job.revision, 'owner', scope_id='scope', lease_seconds=-1)
    assert item.harness_task_id == 'one'
    job = ledger.get_job('job', scope_id='scope')
    with pytest.raises(HarnessConflict):
        ledger.claim_work_item('job', job.revision, 'second', scope_id='scope')
    assert ledger.get_work_item('two', scope_id='scope').status == 'PENDING'


def test_logical_execution_reentry_and_stale_owner_are_rejected(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen', scope_id='scope', owner='owner')
    assert ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen', scope_id='scope').execution_id == execution.execution_id
    with pytest.raises(HarnessConflict):
        ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'changed', scope_id='scope')
    with pytest.raises(PermissionError):
        ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=0,
            owner='intruder', owner_epoch=execution.owner_epoch, status='STARTING')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=0,
        owner='owner', owner_epoch=execution.owner_epoch, status='STARTING')
    with pytest.raises(HarnessConflict):
        ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=0,
            owner='owner', owner_epoch=execution.owner_epoch, status='RUNNING')


def test_acceptance_advances_three_records_and_outbox_idempotently(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    job = accept(ledger, session, verification_ref, handoff_ref)
    assert job.accepted_commit == 'b' * 40
    assert ledger.get_session(session.session_id, scope_id='scope').status == 'COMPLETED'
    assert ledger.get_work_item('one', scope_id='scope').status == 'VERIFIED'
    assert accept(ledger, session, verification_ref, handoff_ref).revision == job.revision
    events = ledger.outbox_events('job', scope_id='scope')
    assert len([event for event in events if event.type == 'harness.increment.accepted']) == 1
    for event in events:
        ledger.consume_event('console', event, scope_id='scope')
    assert ledger.consume_event('console', events[-1], scope_id='scope')['last_seq'] == len(events)
    with pytest.raises(HarnessConflict):
        ledger.consume_event('console', events[-1].model_copy(update={'payload': {'forged': True}}), scope_id='scope')


@pytest.mark.parametrize('changes', [{'passed': False}, {'source_manifest': 'forged'}, {'acceptance_refs': []}, {'commands': []}])
def test_forged_verification_never_advances(ledger, changes):
    session, verification_ref, handoff_ref = candidate(ledger, **changes)
    with pytest.raises(HarnessConflict):
        accept(ledger, session, verification_ref, handoff_ref)
    assert ledger.get_job('job', scope_id='scope').accepted_commit == 'a' * 40
    assert ledger.get_work_item('one', scope_id='scope').status == 'CANDIDATE'


def test_damaged_or_wrong_scope_artifact_is_rejected(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    with pytest.raises(PermissionError):
        ledger.get_job('job', scope_id='other')
    ledger.artifacts._path('scope', session.run_id, handoff_ref).write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        accept(ledger, session, verification_ref, handoff_ref)
    assert ledger.get_job('job', scope_id='scope').accepted_commit == 'a' * 40


def test_stop_intent_blocks_launch_and_is_idempotent(ledger):
    session = coder(ledger)
    job = ledger.get_job('job', scope_id='scope')
    stopping = ledger.request_stop('job', job.revision, scope_id='scope')
    assert ledger.request_stop('job', job.revision, scope_id='scope').stop_request_id == stopping.stop_request_id
    with pytest.raises(HarnessConflict):
        ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'input', scope_id='scope')
    with pytest.raises(HarnessConflict):
        ledger.claim_resume('job', stopping.revision, 'second', scope_id='scope', inspection_ref='missing')


def test_acceptance_transaction_rolls_back_when_outbox_fails(ledger, monkeypatch):
    session, verification_ref, handoff_ref = candidate(ledger)
    original = ledger._event

    def fail(job, kind, payload=None):
        if kind == 'harness.increment.accepted':
            raise RuntimeError('injected outbox failure')
        return original(job, kind, payload)

    monkeypatch.setattr(ledger, '_event', fail)
    with pytest.raises(RuntimeError, match='injected'):
        accept(ledger, session, verification_ref, handoff_ref)
    assert ledger.get_job('job', scope_id='scope').accepted_commit == 'a' * 40
    assert ledger.get_work_item('one', scope_id='scope').status == 'CANDIDATE'
    assert ledger.get_session(session.session_id, scope_id='scope').status == 'RUNNING'


def test_postgres_two_connections_claim_once(postgres_harness_pair):
    first, second = postgres_harness_pair
    job = initialized(first)
    barrier = threading.Barrier(2)

    def claim(store):
        barrier.wait()
        try:
            return store.claim_work_item('job', job.revision, str(id(store)), scope_id='scope').harness_task_id
        except HarnessConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [first, second]))
    assert results.count('one') == 1
    assert results.count(None) == 1
    assert second.get_work_item('one', scope_id='scope').status == 'LEASED'


def test_resume_requires_frozen_checkpoint_and_fences_old_owner(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen', scope_id='scope', owner='owner')
    job = ledger.get_job('job', scope_id='scope')
    job = ledger.request_stop('job', job.revision, scope_id='scope')
    snapshot = ledger.artifacts.put('scope', session.run_id, {'frozen_files': []})
    checkpoint = ExecutionCheckpoint(scope_id='scope', job_id='job', session_id=session.session_id,
        run_id=session.run_id, execution_id=execution.execution_id, owner_epoch=execution.owner_epoch,
        status='STOPPED', workspace_snapshot_ref=snapshot, writers_alive=True, artifacts_consistent=True)
    bad_ref = ledger.artifacts.put('scope', session.run_id, checkpoint.model_dump())
    with pytest.raises(HarnessConflict):
        ledger.confirm_stopped('job', bad_ref, scope_id='scope', expected_revision=job.revision, session_id=session.session_id)
    checkpoint.writers_alive = False
    reference = ledger.artifacts.put('scope', session.run_id, checkpoint.model_dump())
    job = ledger.confirm_stopped('job', reference, scope_id='scope', expected_revision=job.revision, session_id=session.session_id)
    stopped_execution = ledger.get_execution(execution.execution_id, scope_id='scope')
    rebuilt = ledger.rebuild_execution(execution.execution_id, 'SDK session 丢失', reference, scope_id='scope',
        expected_revision=stopped_execution.revision, owner='owner', owner_epoch=execution.owner_epoch)
    assert rebuilt.attempt_no == 2
    assert rebuilt.input_hash == 'frozen'
    assert rebuilt.attempt_history[0].reason == 'SDK session 丢失'
    job = ledger.claim_resume('job', job.revision, 'new-owner', scope_id='scope', inspection_ref=reference)
    assert job.owner_epoch == execution.owner_epoch + 1
    execution_now = ledger.get_execution(execution.execution_id, scope_id='scope')
    with pytest.raises(PermissionError):
        ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=execution_now.revision,
            owner='owner', owner_epoch=execution.owner_epoch, status='STARTING')
    same = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen', scope_id='scope')
    assert same.execution_id == execution.execution_id


def test_wrong_exit_receipt_never_advances(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    record = ledger.artifacts.json('scope', session.run_id, verification_ref)
    record['commands'][0]['command'] = ['pretend-success']
    verification_ref = ledger.artifacts.put('scope', session.run_id, record)
    handoff = ledger.artifacts.json('scope', session.run_id, handoff_ref)
    handoff['verification_ref'] = verification_ref
    handoff_ref = ledger.artifacts.put('scope', session.run_id, handoff)
    with pytest.raises(HarnessConflict, match='退出码'):
        accept(ledger, session, verification_ref, handoff_ref)


def test_final_cumulative_failure_blocks_then_retries_without_coding(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    job = accept(ledger, session, verification_ref, handoff_ref)
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)

    def final_record(passed):
        receipt = VerificationEvidence(**identity, candidate_commit=job.accepted_commit, source_manifest='cumulative-source',
            patch_hash='cumulative-patch', environment_digest='env', test_spec_hash='spec', check_plan_hash='plan',
            command=['pytest', 'all'], exit_code=0 if passed else 1)
        evidence_ref = ledger.artifacts.put('scope', session.run_id, receipt.model_dump())
        return ledger.artifacts.put('scope', session.run_id, VerificationRecord(**identity,
            candidate_commit=job.accepted_commit, input_commit='a' * 40, source_manifest='cumulative-source',
            patch_hash='cumulative-patch', environment_digest='env', test_spec_hash='spec', check_plan_hash='plan',
            acceptance_refs=['original'], commands=[VerificationCommand(command=receipt.command,
                exit_code=receipt.exit_code, evidence_ref=evidence_ref)], passed=passed, target_commit='c' * 40).model_dump())

    failed = ledger.finalize_job('job', final_record(False), scope_id='scope', expected_revision=job.revision, session_id=session.session_id)
    assert failed.status == 'BLOCKED'
    assert failed.blocker == 'FINAL_ACCEPTANCE_FAILED'
    assert failed.accepted_commit == job.accepted_commit
    completed = ledger.finalize_job('job', final_record(True), scope_id='scope', expected_revision=failed.revision, session_id=session.session_id)
    assert completed.status == 'COMPLETED'
    assert completed.final_patch_hash == 'cumulative-patch'
    assert len(ledger.list_sessions('job', scope_id='scope')) == 2
    assert ledger.invalidate_target('job', 'c' * 40, scope_id='scope', expected_revision=completed.revision).quality == 'MERGE_READY'
    invalidated = ledger.invalidate_target('job', 'd' * 40, scope_id='scope', expected_revision=completed.revision)
    assert invalidated.quality == 'INCREMENT_VERIFIED'
    assert invalidated.status == 'BLOCKED'
    assert invalidated.blocker == 'TARGET_BASE_CHANGED'
    assert invalidated.final_target_commit == 'c' * 40
    assert invalidated.accepted_commit == completed.accepted_commit
    with pytest.raises(HarnessConflict, match='目标分支已推进'):
        ledger.finalize_job('job', final_record(True), scope_id='scope', expected_revision=invalidated.revision, session_id=session.session_id)
    assert ledger.invalidate_target('job', 'd' * 40, scope_id='scope', expected_revision=invalidated.revision).revision == invalidated.revision
    with pytest.raises(HarnessConflict):
        ledger.invalidate_target('job', 'e' * 40, scope_id='scope', expected_revision=completed.revision)


def test_target_refreshes_after_final_acceptance_failure(ledger):
    session, verification_ref, handoff_ref = candidate(ledger)
    job = accept(ledger, session, verification_ref, handoff_ref)
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)
    evidence = VerificationEvidence(**identity, candidate_commit=job.accepted_commit,
        source_manifest='cumulative-source', patch_hash='cumulative-patch', environment_digest='env',
        test_spec_hash='spec', check_plan_hash='plan', command=['pytest', 'all'], exit_code=1)
    evidence_ref = ledger.artifacts.put('scope', session.run_id, evidence.model_dump())
    record = VerificationRecord(**identity, candidate_commit=job.accepted_commit, input_commit='a' * 40,
        source_manifest='cumulative-source', patch_hash='cumulative-patch', environment_digest='env',
        test_spec_hash='spec', check_plan_hash='plan', acceptance_refs=['original'],
        commands=[VerificationCommand(command=evidence.command, exit_code=1, evidence_ref=evidence_ref)],
        passed=False, target_commit='c' * 40)
    verification_ref = ledger.artifacts.put('scope', session.run_id, record.model_dump())
    blocked = ledger.finalize_job('job', verification_ref, scope_id='scope', expected_revision=job.revision,
        session_id=session.session_id)
    assert blocked.blocker == 'FINAL_ACCEPTANCE_FAILED'
    refreshed = ledger.invalidate_target('job', 'd' * 40, scope_id='scope', expected_revision=blocked.revision)
    assert refreshed.blocker == 'FINAL_ACCEPTANCE_FAILED'
    assert refreshed.required_target_commit == 'd' * 40
    assert refreshed.revision == blocked.revision + 1
    same = ledger.invalidate_target('job', 'd' * 40, scope_id='scope', expected_revision=refreshed.revision)
    assert same.revision == refreshed.revision
    evidence = evidence.model_copy(update={'exit_code': 0})
    evidence_ref = ledger.artifacts.put('scope', session.run_id, evidence.model_dump())
    record = record.model_copy(update={'passed': True, 'target_commit': 'd' * 40,
        'commands': [VerificationCommand(command=evidence.command, exit_code=0, evidence_ref=evidence_ref)]})
    verification_ref = ledger.artifacts.put('scope', session.run_id, record.model_dump())
    completed = ledger.finalize_job('job', verification_ref, scope_id='scope', expected_revision=same.revision,
        session_id=session.session_id)
    assert completed.status == 'COMPLETED'
    assert completed.final_target_commit == 'd' * 40
    changed = ledger.invalidate_target('job', 'e' * 40, scope_id='scope', expected_revision=completed.revision)
    refreshed = ledger.invalidate_target('job', 'f' * 40, scope_id='scope', expected_revision=changed.revision)
    assert refreshed.blocker == 'TARGET_BASE_CHANGED'
    assert refreshed.required_target_commit == 'f' * 40
    assert refreshed.revision == changed.revision + 1
    with pytest.raises(HarnessConflict, match='目标分支已推进'):
        ledger.finalize_job('job', verification_ref, scope_id='scope', expected_revision=refreshed.revision,
            session_id=session.session_id)


def test_feedback_allocates_new_invocation_but_plain_reentry_does_not(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'first-input', scope_id='scope', owner='owner')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope', expected_revision=execution.revision,
        owner='owner', owner_epoch=session.owner_epoch, status='FAILED')
    session = ledger.bind_candidate(session.session_id, scope_id='scope', expected_revision=session.revision,
        owner='owner', owner_epoch=session.owner_epoch, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan')
    identity = dict(scope_id='scope', job_id='job', session_id=session.session_id, run_id=session.run_id)
    evidence = VerificationEvidence(**identity, candidate_commit='b' * 40, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan', command=['pytest'], exit_code=1)
    evidence_ref = ledger.artifacts.put('scope', session.run_id, evidence.model_dump())
    verification = VerificationRecord(**identity, candidate_commit='b' * 40, input_commit=session.input_commit,
        source_manifest='source', patch_hash='patch', environment_digest='env', test_spec_hash='spec', check_plan_hash='plan',
        commands=[VerificationCommand(command=['pytest'], exit_code=1, evidence_ref=evidence_ref)],
        acceptance_refs=['original'], passed=False)
    verification_ref = ledger.artifacts.put('scope', session.run_id, verification.model_dump())
    feedback_ref = ledger.artifacts.put('scope', session.run_id, HarnessFeedback(**identity,
        execution_id=execution.execution_id, candidate_commit='b' * 40, source_manifest='source',
        verification_ref=verification_ref, summary='宿主复现失败').model_dump())
    with pytest.raises(HarnessConflict):
        ledger.allocate_invocation(session.session_id, scope_id='scope', expected_revision=session.revision)
    session = ledger.allocate_invocation(session.session_id, scope_id='scope', expected_revision=session.revision, feedback_ref=feedback_ref)
    assert session.invocation_no == 2
    next_execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:2', 'second-input', scope_id='scope', owner='owner')
    assert next_execution.execution_id != execution.execution_id
    assert ledger.prepare_execution(session.session_id, 'coder', 'coder:2', 'second-input', scope_id='scope').execution_id == next_execution.execution_id


def test_second_increment_claims_accepted_commit_and_fresh_run(ledger):
    job = initialized(ledger, two=True)
    item = ledger.claim_work_item('job', job.revision, 'owner', scope_id='scope')
    job = ledger.get_job('job', scope_id='scope')
    first = ledger.create_session(HarnessSession(session_id='coder', job_id='job', scope_id='scope', kind='coder',
        harness_task_id=item.harness_task_id, run_id='run-coder', input_commit=job.accepted_commit),
        scope_id='scope', expected_revision=job.revision)
    first = ledger.bind_candidate(first.session_id, scope_id='scope', expected_revision=first.revision,
        owner='owner', owner_epoch=first.owner_epoch, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan')
    identity = dict(scope_id='scope', job_id='job', session_id=first.session_id, run_id=first.run_id)
    receipt = VerificationEvidence(**identity, candidate_commit='b' * 40, source_manifest='source', patch_hash='patch',
        environment_digest='env', test_spec_hash='spec', check_plan_hash='plan', command=['pytest'], exit_code=0)
    receipt_ref = ledger.artifacts.put('scope', first.run_id, receipt.model_dump())
    verification = VerificationRecord(**identity, candidate_commit='b' * 40, input_commit=first.input_commit,
        source_manifest='source', patch_hash='patch', environment_digest='env', test_spec_hash='spec', check_plan_hash='plan',
        acceptance_refs=['original'], commands=[VerificationCommand(command=['pytest'], exit_code=0, evidence_ref=receipt_ref)], passed=True)
    verification_ref = ledger.artifacts.put('scope', first.run_id, verification.model_dump())
    handoff_ref = ledger.artifacts.put('scope', first.run_id, SessionHandoff(**identity, input_commit=first.input_commit,
        candidate_commit='b' * 40, source_manifest='source', patch_hash='patch', verification_ref=verification_ref,
        environment_digest='env').model_dump())
    job = accept(ledger, first, verification_ref, handoff_ref)
    second = ledger.claim_work_item('job', job.revision, 'owner', scope_id='scope')
    assert second.harness_task_id == 'two'
    job = ledger.get_job('job', scope_id='scope')
    new_session = ledger.create_session(HarnessSession(job_id='job', scope_id='scope', kind='coder',
        harness_task_id=second.harness_task_id, run_id='run-second', input_commit=job.accepted_commit),
        scope_id='scope', expected_revision=job.revision)
    assert new_session.input_commit == 'b' * 40
    assert new_session.source_manifest == ''
