import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracefix.agents.claude_sdk_adapter import ClaudeSdkAdapter, ExecutionInspection, ExecutionResult
from tracefix.runtime.harness import HarnessRuntime
from tracefix.runtime.harness_contracts import ExecutionCheckpoint
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, MemoryHarnessStore

from test_harness_store import coder


@pytest.fixture
def ledger(tmp_path):
    return MemoryHarnessStore(Artifacts(tmp_path / 'artifacts'))


def test_stop_checkpoint_resume_changes_epoch_and_rejects_late_writer(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen',
        scope_id='scope', owner='owner')
    job = ledger.request_stop('job', ledger.get_job('job', scope_id='scope').revision,
        scope_id='scope')
    snapshot = ledger.artifacts.put('scope', session.run_id, {'frozen_files': []})
    checkpoint = ExecutionCheckpoint(scope_id='scope', job_id='job', session_id=session.session_id,
        run_id=session.run_id, execution_id=execution.execution_id, owner_epoch=execution.owner_epoch,
        status='STOPPED', workspace_snapshot_ref=snapshot, writers_alive=False,
        artifacts_consistent=True, stop_request_id=job.stop_request_id)
    checkpoint_ref = ledger.artifacts.put('scope', session.run_id, checkpoint.model_dump())
    stopped = ledger.confirm_stopped('job', checkpoint_ref, scope_id='scope',
        expected_revision=job.revision, session_id=session.session_id)
    resumed = ledger.claim_resume('job', stopped.revision, 'new-owner', scope_id='scope',
        inspection_ref=checkpoint_ref)
    current = ledger.get_execution(execution.execution_id, scope_id='scope')
    assert resumed.status == 'RUNNING' and current.status == 'STARTING'
    with pytest.raises(PermissionError):
        ledger.update_execution(execution.execution_id, scope_id='scope',
            expected_revision=current.revision, owner='owner', owner_epoch=execution.owner_epoch,
            status='RUNNING')


def test_unknown_execution_projects_blocker_and_never_reopens(ledger):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen',
        scope_id='scope', owner='owner')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope',
        expected_revision=execution.revision, owner='owner', owner_epoch=session.owner_epoch,
        status='STARTING')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope',
        expected_revision=execution.revision, owner='owner', owner_epoch=session.owner_epoch,
        status='UNKNOWN', pending_operations=['worker'])
    assert execution.status == 'UNKNOWN'
    assert ledger.get_job('job', scope_id='scope').blocker == 'EXECUTOR_STATE_UNKNOWN'
    with pytest.raises(HarnessConflict):
        ledger.update_execution(execution.execution_id, scope_id='scope',
            expected_revision=execution.revision, owner='owner', owner_epoch=session.owner_epoch,
            status='STARTING')


def stopped_execution(ledger, *, reason=None, session_id='sdk-session', transcript=True):
    session = coder(ledger)
    execution = ledger.prepare_execution(session.session_id, 'coder', 'coder:1', 'frozen',
        scope_id='scope', owner='owner')
    execution = ledger.update_execution(execution.execution_id, scope_id='scope',
        expected_revision=execution.revision, owner='owner', owner_epoch=session.owner_epoch,
        status='FAILED', result_ref='preserved-result', sdk_session_id=session_id)
    job = ledger.request_stop('job', ledger.get_job('job', scope_id='scope').revision, scope_id='scope')
    snapshot = ledger.artifacts.put('scope', session.run_id, {'files': {'source.py': 'partial'}})
    transcript_ref = ledger.artifacts.put('scope', session.run_id, {'content': 'observed'}) if transcript else None
    checkpoint = ExecutionCheckpoint(scope_id='scope', job_id='job', session_id=session.session_id,
        run_id=session.run_id, execution_id=execution.execution_id, owner_epoch=execution.owner_epoch,
        status='STOPPED', workspace_snapshot_ref=snapshot, transcript_ref=transcript_ref,
        sdk_session_id=session_id, writers_alive=False, artifacts_consistent=True,
        stop_request_id=job.stop_request_id, recovery_reason=reason)
    reference = ledger.artifacts.put('scope', session.run_id, checkpoint.model_dump())
    job = ledger.confirm_stopped('job', reference, scope_id='scope', expected_revision=job.revision,
        session_id=session.session_id)
    return job, session, execution, checkpoint, reference


def test_resume_clears_interrupted_result_and_records_lost_transcript_rebuild(ledger):
    job, session, execution, checkpoint, reference = stopped_execution(ledger, transcript=False)
    ledger.claim_resume('job', job.revision, 'next-owner', scope_id='scope', inspection_ref=reference)
    current = ledger.get_execution(execution.execution_id, scope_id='scope')
    assert current.status == 'STARTING' and current.result_ref is None
    assert current.sdk_session_id is None and current.attempt_no == 2
    assert 'transcript' in current.attempt_history[-1].reason


def test_resume_at_node_boundary_preserves_sealed_result(ledger):
    job, session, execution, checkpoint, reference = stopped_execution(ledger,
        reason='node_boundary:FAILED:RUNNING')
    ledger.claim_resume('job', job.revision, 'next-owner', scope_id='scope', inspection_ref=reference)
    current = ledger.get_execution(execution.execution_id, scope_id='scope')
    assert current.status == 'FAILED' and current.result_ref == 'preserved-result'
    assert current.attempt_no == 1


def test_missing_snapshot_blocks_runtime_resume_without_clearing_stop(ledger):
    job, session, execution, checkpoint, reference = stopped_execution(ledger)
    ledger.artifacts._path('scope', session.run_id, checkpoint.workspace_snapshot_ref).unlink()
    runtime = object.__new__(HarnessRuntime)
    runtime.store, runtime.owner = ledger, 'next-owner'
    state = SimpleNamespace(scope_id='scope', harness_job_id='job')
    with pytest.raises((OSError, ValueError)):
        runtime.resume(SimpleNamespace(), state, reference)
    current = ledger.get_job('job', scope_id='scope')
    assert current.status == 'BLOCKED' and current.blocker == 'RECOVERY_ARTIFACTS_MISSING'
    assert current.stop_request_id == job.stop_request_id


def test_old_epoch_events_are_ignored_after_resume(ledger):
    job, session, execution, checkpoint, reference = stopped_execution(ledger)
    ledger.claim_resume('job', job.revision, 'next-owner', scope_id='scope', inspection_ref=reference)
    runtime = object.__new__(HarnessRuntime)
    runtime.store = ledger
    emitted = []
    engine = SimpleNamespace(event=lambda *args: emitted.append(args))
    state = SimpleNamespace(scope_id='scope', harness_job_id='job')
    runtime._emit_owned(engine, state, execution, 'sdk.message', {'content': 'late'})
    assert emitted == []


async def test_success_racing_stop_seals_stopped_checkpoint_without_advancing(tmp_path):
    from test_harness_graph import create_harness_engine

    engine, state, runtime, executor = await create_harness_engine(tmp_path)
    original = executor.start

    async def stop_before_success(request, emit):
        result = await original(request, emit)
        job = runtime.job(state)
        runtime.store.request_stop(job.job_id, job.revision, scope_id=state.scope_id)
        return result

    executor.start = stop_before_success
    executor.inspect = AsyncMock(return_value=ExecutionInspection(
        execution_id='checked', status='STOPPED', active=False))
    with pytest.raises(HarnessConflict, match='已停止'):
        await runtime._execute(engine, state, 'initializer',
            json.dumps({'approved_commands': engine.profile.commands,
                'acceptance_refs': [state.test_spec_ref]}))
    assert runtime.job(state).status == 'STOPPED'
    execution = runtime.store.list_executions(state.harness_job_id, scope_id=state.scope_id)[-1]
    checkpoint = engine.get(state, execution.checkpoint_ref)
    assert execution.status == 'STOPPED' and checkpoint['writers_alive'] is False
    assert runtime.job(state).accepted_commit == runtime.job(state).origin_base_commit


def test_checkpoint_requires_writer_evidence_even_for_success(ledger):
    job, session, execution, checkpoint, reference = stopped_execution(ledger)
    runtime = object.__new__(HarnessRuntime)
    runtime.store = ledger
    runtime._read = lambda state: {'executions': {}}
    observed = []
    engine = SimpleNamespace(put=lambda state, value, **kwargs: observed.append(value) or 'checkpoint')
    state = SimpleNamespace(scope_id='scope', harness_job_id='job')
    runtime._checkpoint(engine, state, session, execution, SimpleNamespace(),
        ExecutionResult(execution_id=execution.execution_id, status='SUCCEEDED'), 'snapshot')
    assert observed[-1]['writers_alive'] is True
    assert observed[-1]['artifacts_consistent'] is False


async def test_stop_at_node_boundary_is_idempotent_and_preserves_result_on_resume(tmp_path):
    from test_harness_graph import create_harness_engine

    engine, state, runtime, executor = await create_harness_engine(tmp_path)
    executor.inspect = AsyncMock(return_value=ExecutionInspection(
        execution_id='checked', status='STOPPED', active=False))
    state, execution, result = await runtime._execute(engine, state, 'initializer',
        json.dumps({'approved_commands': engine.profile.commands,
            'acceptance_refs': [state.test_spec_ref]}))
    job = runtime.job(state)
    pending = runtime.store.request_stop(job.job_id, job.revision, scope_id=state.scope_id)
    stopped = await runtime.stop(engine, state)
    assert stopped.status == 'STOPPED' and stopped.stop_request_id == pending.stop_request_id
    current = runtime.store.get_execution(execution.execution_id, scope_id=state.scope_id)
    runtime.resume(engine, state, current.checkpoint_ref)
    current = runtime.store.get_execution(execution.execution_id, scope_id=state.scope_id)
    assert current.status == 'SUCCEEDED' and current.result_ref == execution.result_ref
    assert executor.owner_epoch == execution.owner_epoch


@pytest.mark.asyncio
async def test_interrupt_grace_receipt_does_not_claim_unknown_writer_stopped(tmp_path):
    class Client:
        async def interrupt(self):
            return None

    adapter = ClaudeSdkAdapter(sdk=SimpleNamespace())
    execution_id = 'execution-1'
    adapter._clients[execution_id] = Client()
    adapter._inspections[execution_id] = ExecutionInspection(
        execution_id=execution_id, status='RUNNING', active=True)
    receipt = await adapter.interrupt(execution_id, 'stop-1', grace_seconds=0)
    assert receipt.interrupt_sent and not receipt.writers_stopped
    assert receipt.status == 'INTERRUPTING'


@pytest.mark.asyncio
async def test_interrupt_marks_terminal_result_stopped_when_stop_wins(tmp_path):
    class ResultMessage:
        session_id = 'sdk-session'
        subtype = 'success'
        is_error = False
        usage = {}
        total_cost_usd = 0
        num_turns = 1
        structured_output = {'summary': 'ok', 'unresolved': []}
        terminal_reason = None
        errors = []

    class Client:
        def __init__(self, options):
            self.options = options
            self.interrupted = False

        async def connect(self):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            await asyncio.sleep(0.01)
            yield ResultMessage()

        async def disconnect(self):
            return None

        async def interrupt(self):
            self.interrupted = True

    sdk = SimpleNamespace(ClaudeAgentOptions=lambda **values: SimpleNamespace(**values),
        AgentDefinition=lambda **values: SimpleNamespace(**values))
    clients = []

    def factory(options):
        client = Client(options)
        clients.append(client)
        return client

    from test_claude_sdk_adapter import request
    adapter = ClaudeSdkAdapter(sdk=sdk, client_factory=factory)
    original = request(tmp_path)
    task = asyncio.create_task(adapter.start(original))
    await asyncio.sleep(0)
    receipt = await adapter.interrupt(original.execution_id, 'stop-1', grace_seconds=1)
    result = await task
    assert receipt.writers_stopped and result.status == 'STOPPED'
    assert clients[0].interrupted
