import asyncio
from types import SimpleNamespace

import pytest

from tracefix.agents.claude_sdk_adapter import ClaudeSdkAdapter, ExecutionInspection
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
