import pytest
from langgraph.errors import GraphRecursionError

from tracefix.execution.browser import MCPActionUnknown, MCPConnectionError
from tracefix.knowledge.assembler import ContextWindowError
from tracefix.model.gateway import ModelError
from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus, digest
from tracefix.runtime.smoke import make_engine as make_smoke_engine
from tests.support_sdk_candidate import verified_sdk_candidate
from tests.test_harness_graph import create_harness_engine


def make_engine(tmp_path, **options):
    engine, state = make_smoke_engine(tmp_path, **options)
    state.mode = 'test'
    return engine, state


def test_existing_candidate_branch_does_not_return_stale_commit(tmp_path):
    engine, state = make_engine(tmp_path)
    candidate = 'tracefix/' + state.run_id
    target = engine.workspace.root / 'src/value.ts'
    target.write_bytes(b'export const persisted = true;\n')
    engine.workspace.branch(candidate)
    previous_commit = engine.workspace.head()
    target.write_bytes(b'export const persisted = 1;\n')
    assert engine.workspace.branch(candidate) == candidate
    assert engine.workspace.head() != previous_commit and engine.workspace.diff() == ''



@pytest.mark.parametrize("status", ["UNKNOWN_OPERATION", "WAITING_NETWORK", "FAILED"])
async def test_batch_model_error_is_terminal_and_not_replayed(tmp_path, status):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"

    class UnknownModel:
        calls = 0

        async def generate(self, schema, context, **kwargs):
            self.calls += 1
            raise ModelError(
                "provider response lost",
                status=status,
                category="network",
                details={"requires_manual_review": True},
            )

    model = UnknownModel()
    engine.model = model
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert model.calls == 1
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details["status"] == status
    assert finished.report_ref
    assert finished.run_status not in {RunStatus.PAUSED, RunStatus.WAITING_APPROVAL}
    report = engine.get(finished, finished.report_ref)
    assert report["patch_verification"] in {"none", "unverified"}
    assert report["patch_diff_ref"]
    assert engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]) == b""
    assert report["result_summary"]
    assert any(event["type"] == "run.finished" for event in
               engine.store.trace(state.run_id, state.scope_id))



async def test_batch_exception_group_preserves_full_traceback_in_final_report(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'

    async def fail_with_nested_exception(current, next_node):
        try:
            raise ConnectionError('api_key=private-credential ' + 'provider detail ' * 300)
        except ConnectionError as cause:
            raise ExceptionGroup('unhandled errors in a TaskGroup', [
                RuntimeError('worker root failure')]) from cause

    engine.prepare = fail_with_nested_exception
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(finished, finished.report_ref)
    formatted = report['error_details']['exception_traceback']
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert formatted == finished.error
    assert len(formatted) > 2500
    assert 'fail_with_nested_exception' in formatted
    assert 'ConnectionError: api_key=[REDACTED]' in formatted
    assert 'The above exception was the direct cause' in formatted
    assert 'ExceptionGroup: unhandled errors in a TaskGroup' in formatted
    assert 'RuntimeError: worker root failure' in formatted
    assert 'private-credential' not in formatted
    assert engine.runner.closed and engine.browser.closed



async def test_batch_browser_unknown_operation_preserves_unreceipted_intent(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"
    attempts = []

    async def lose_receipt(action):
        attempts.append(action)
        raise MCPActionUnknown("unknown-browser-action", action.kind,
                               MCPConnectionError("response lost"))

    engine.browser.action = lose_receipt
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert len(attempts) == 1
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details["status"] == "UNKNOWN_OPERATION"
    events = engine.store.trace(state.run_id, state.scope_id)
    failed = next(event for event in events if event["type"] == "tool.error")
    assert engine.store.operations[failed["payload"]["operation_id"]]["status"] == "UNKNOWN"
    report = engine.get(finished, finished.report_ref)
    assert report["patch_available"] is False
    assert report["patch_verification"] == "none"
    assert report["patch_diff_ref"]
    assert engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]) == b""
    assert report["result_summary"]



async def test_batch_unreceipted_operation_finishes_without_invoking_side_effect(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"
    attempts = []

    async def prepare_with_unreceipted_operation(current, next_node):
        operation_id = f"{current.run_id}:{current.revision}:unknown:{digest({})[:24]}"
        engine.store.begin(current, operation_id, {})

        async def perform():
            attempts.append("executed")
            return {"passed": True}

        await engine.operation(current, "unknown", {}, perform)
        pytest.fail("An operation without a receipt must not complete by replay")

    engine.prepare = prepare_with_unreceipted_operation
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert attempts == []
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details["status"] == "UNKNOWN_OPERATION"
    assert finished.error_details["category"] == "missing_receipt"
    assert finished.report_ref
    assert all(operation["status"] == "STARTED" for operation in engine.store.operations.values())



async def test_batch_context_window_error_finishes_with_consumable_output(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"

    class ContextTooLargeModel:
        calls = 0

        async def generate(self, schema, context, **kwargs):
            self.calls += 1
            raise ContextWindowError(200, 100, ["frozen_test_spec"])

    model = ContextTooLargeModel()
    engine.model = model
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert model.calls == 1
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details["category"] == "context_window"
    report = engine.get(finished, finished.report_ref)
    assert report["patch_verification"] == "none"
    assert engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]) == b""
    assert report["result_summary"]



@pytest.mark.parametrize('execution_mode', ['batch', 'interactive'])
async def test_graph_recursion_limit_finishes_with_loop_report(tmp_path, execution_mode):
    engine, state = make_engine(tmp_path)
    state.execution_mode = execution_mode

    class ExhaustedGraph:
        async def ainvoke(self, invocation, config):
            raise GraphRecursionError("graph limit reached")

    engine.graph = ExhaustedGraph()
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.ABNORMAL
    assert finished.outcome == Outcome.LOOP_DETECTED
    assert finished.abnormal_termination is True
    assert finished.error_details["terminal_reason"] == "graph_recursion_limit"
    report = engine.get(finished, finished.report_ref)
    assert report["outcome"] == Outcome.LOOP_DETECTED
    assert report["patch_diff_ref"]
    assert report["result_summary"]
    assert engine.runner.closed and engine.browser.closed



async def test_batch_terminal_error_exports_applied_diff_without_claiming_fix(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"
    original = engine.workspace.read("src/value.ts")
    engine.workspace.path("src/value.ts", write=True).write_text(
        "export const persisted = true;\n", encoding="utf-8"
    )
    state.patch_hash = digest(engine.workspace.diff().encode())
    state.phase = Phase.FINALIZE
    state.outcome = Outcome.INFRA_FAILURE
    state.error = "模型调用发生重大错误"
    engine.store.save(state)

    await engine.finalize(state, None)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    report = engine.get(finished, finished.report_ref)
    assert report["patch_available"] is True
    assert report["patch_diff_ref"]
    assert report["patch_verification"] == "unverified"
    diff = engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]).decode("utf-8")
    assert "persisted = true" in diff
    assert original != engine.workspace.read("src/value.ts")
    assert report["outcome"] != Outcome.FIX_VERIFIED



async def test_batch_no_bug_found_finishes_with_explicit_empty_patch_result(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    state.execution_mode = "batch"

    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED
    assert finished.outcome == Outcome.NO_BUG_FOUND
    report = engine.get(finished, finished.report_ref)
    assert report["patch_available"] is False
    assert report["patch_verification"] == "none"
    assert report["patch_diff_ref"]
    assert engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]) == b""
    assert report["result_summary"]
    assert report["outcome"] == Outcome.NO_BUG_FOUND



async def test_interactive_unknown_operation_retains_manual_pause_contract(tmp_path):
    engine, state = make_engine(tmp_path)

    class UnknownModel:
        async def generate(self, schema, context, **kwargs):
            raise ModelError(
                "provider response lost",
                status="UNKNOWN_OPERATION",
                category="network",
                details={"requires_manual_review": True},
            )

    engine.model = UnknownModel()
    await engine.run(state)

    paused = engine.store.load(state.run_id, state.scope_id)
    assert paused.run_status == RunStatus.PAUSED
    assert paused.outcome is None
    assert paused.report_ref is None



async def test_sdk_batch_fix_finishes_without_interactive_approval(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    state.execution_mode = 'batch'
    output = await engine.run(state)
    finished = RunState.model_validate(output['data'])
    assert finished.run_status == RunStatus.COMPLETED, finished.error
    assert finished.outcome == Outcome.FIX_VERIFIED
    assert harness.job(finished).status == 'COMPLETED'
    assert finished.approval_ref is None and engine.store.approvals == {}
    assert [request.role for request in executor.requests] == ['initializer', 'coder', 'coder']
    report = engine.get(finished, finished.report_ref)
    assert report['patch_verification'] == 'verified' and report['patch_available']
    assert report['repair_commit'] == engine.workspace.head()
    assert engine.workspace.diff() == ''
    assert engine.runner.closed and engine.browser.closed


async def test_sdk_unknown_browser_operation_blocks_without_replaying_candidate(tmp_path):
    engine, state = await verified_sdk_candidate(tmp_path, stop_before_verify=True)
    state.execution_mode = 'batch'
    attempts = []

    async def lose_receipt(action):
        attempts.append(action.kind)
        raise MCPActionUnknown('unknown-verification-action', action.kind, MCPConnectionError('response lost'))

    engine.browser.action = lose_receipt
    output = await engine.run(state)
    finished = RunState.model_validate(output['data'])
    assert len(attempts) == 1
    assert engine.harness.job(finished).status == 'BLOCKED'
    assert finished.run_status == RunStatus.PAUSED
    assert finished.outcome != Outcome.FIX_VERIFIED
    assert 'persisted = true' in engine.workspace.read('src/value.ts')
    assert any(operation['status'] == 'UNKNOWN' for operation in engine.store.operations.values())


@pytest.mark.parametrize('damage', ['changed_diff', 'missing_validation', 'export_failure'])
async def test_sdk_final_report_rechecks_patch_and_validation_evidence(tmp_path, damage, monkeypatch):
    engine, state = await verified_sdk_candidate(tmp_path)
    if damage == 'changed_diff':
        (engine.workspace.root / 'src/value.ts').write_bytes(b'export const persisted = false;\n')
    elif damage == 'missing_validation':
        state.validation_refs = []
    else:
        def unavailable(*args):
            raise OSError('候选差异导出失败')
        monkeypatch.setattr(engine.workspace, 'diff', unavailable)
    state.phase = Phase.FINALIZE
    engine.store.save(state)
    await engine.finalize(state, None)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['terminal_reason'] == 'final_verification_invalid'
    report = engine.get(finished, finished.report_ref)
    assert report['patch_verification'] != 'verified'
    if damage == 'export_failure':
        assert report['patch_export_error']


async def test_sdk_commit_failure_is_terminal_and_reported(tmp_path, monkeypatch):
    engine, state = await verified_sdk_candidate(tmp_path)

    def unavailable(name):
        raise OSError('本地提交失败')

    monkeypatch.setattr(engine.workspace, 'branch', unavailable)
    state.phase = Phase.FINALIZE
    engine.store.save(state)
    await engine.finalize(state, None)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    report = engine.get(finished, finished.report_ref)
    assert report['patch_available'] and report['patch_verification'] == 'unverified'
    assert not report['repair_commit']
