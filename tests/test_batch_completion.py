"""Batch execution must always produce a terminal, consumable result."""

from pathlib import Path

import pytest
from langgraph.errors import GraphRecursionError

from tracefix.execution.browser import MCPActionUnknown, MCPConnectionError
from tracefix.knowledge.assembler import ContextWindowError
from tracefix.model.gateway import ModelError
from tracefix.runtime.contracts import (Outcome, Phase, RunState, RunStatus,
                                       TestSpec as Spec, Validation, digest)
from tracefix.runtime.smoke import FakeBrowser, make_engine


async def test_batch_fix_finishes_without_interactive_approval(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"

    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED, finished.error
    assert finished.outcome == Outcome.FIX_VERIFIED
    assert finished.approval_ref is None
    assert finished.local_branch is None
    assert engine.store.approvals == {}
    assert len(finished.validation_refs) == 6
    assert finished.report_ref
    report = engine.get(finished, finished.report_ref)
    assert report["outcome"] == Outcome.FIX_VERIFIED
    assert report["patch_verification"] == "verified"
    assert report["patch_available"] is True
    assert report["patch_diff_ref"]
    assert report["result_summary"]
    assert engine.runner.closed and engine.browser.closed


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
    assert engine.store.operations[failed["payload"]["operation_id"]]["status"] == "STARTED"
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


async def test_batch_graph_recursion_limit_finishes_with_loop_report(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"

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


async def test_batch_verification_browser_failure_preserves_applied_patch(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = "batch"
    original_action = engine.browser.action
    failed_attempts = []

    async def lose_verification_receipt(action):
        if "persisted = true" in engine.workspace.read("src/value.ts"):
            failed_attempts.append(action)
            raise MCPActionUnknown("unknown-verification-action", action.kind,
                                   MCPConnectionError("response lost"))
        return await original_action(action)

    engine.browser.action = lose_verification_receipt
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert len(failed_attempts) == 1
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.budget.patches == 1
    assert len(finished.validation_refs) == 4
    report = engine.get(finished, finished.report_ref)
    assert report["patch_available"] is True
    assert report["patch_verification"] == "unverified"
    diff = engine.artifacts.read(state.scope_id, state.run_id,
                                 report["patch_diff_ref"]).decode("utf-8")
    assert "persisted = true" in diff
    assert any(event["type"] == "patch.applied" for event in
               engine.store.trace(state.run_id, state.scope_id))


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


@pytest.mark.parametrize('damage', ['changed_diff', 'missing_validation', 'export_failure'])
async def test_batch_final_report_rechecks_patch_and_validation_evidence(tmp_path, damage):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    original_finalize = engine.finalize
    original_get = engine.get

    async def corrupt_before_final_report(current, next_node):
        if damage == 'changed_diff':
            engine.workspace.path('src/value.ts', write=True).write_text(
                'export const persisted = false;\n', encoding='utf-8')
        elif damage == 'missing_validation':
            def missing_validation(saved, reference):
                if reference == current.validation_refs[-1]:
                    raise FileNotFoundError('验证记录损坏')
                return original_get(saved, reference)
            engine.get = missing_validation
        else:
            def broken_export():
                raise OSError('无法读取工作区 diff')
            engine.workspace.diff = broken_export
        return await original_finalize(current, next_node)

    engine.finalize = corrupt_before_final_report
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    report = original_get(finished, finished.report_ref)
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert report['patch_verification'] != 'verified'
    assert report['error_details']['terminal_reason'] == 'final_verification_invalid'
    assert report['patch_diff_ref']
    assert engine.runner.closed and engine.browser.closed


async def test_batch_optional_memory_failure_keeps_final_report(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'

    def failed_memory(*args):
        raise RuntimeError('记忆存储暂时不可用')

    engine.retriever.candidate = failed_memory
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED
    assert finished.outcome == Outcome.FIX_VERIFIED
    assert finished.report_ref
    assert any(event['type'] == 'run.warning' for event in
               engine.store.trace(state.run_id, state.scope_id))


async def test_batch_unauthorized_patch_paths_receive_diagnosis_feedback(tmp_path):
    from tracefix.runtime.contracts import PatchProposal

    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    inner = engine.model
    contexts = []

    class UnauthorizedPatches:
        async def generate(self, schema, context, **kwargs):
            result = await inner.generate(schema, context, **kwargs)
            if schema is PatchProposal:
                contexts.append(context['diagnosis_retry_count'])
                result.value.edits[0].path = '.env'
            return result

    engine.model = UnauthorizedPatches()
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert contexts == [0, 1, 2]
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.REPAIR_EXHAUSTED
    assert len(finished.diagnosis_feedback_refs) == 3
    assert finished.budget.patches == 0
    feedback = engine.get(finished, finished.diagnosis_feedback_refs[0])
    assert feedback['error_details']['path'] == '.env'


@pytest.mark.parametrize('complete_only', [False, True])
async def test_persistence_profile_rejects_loss_of_uncomplete_behavior(tmp_path, complete_only):
    engine, state = make_engine(tmp_path)
    spec = Spec.model_validate_json((Path(__file__).resolve().parents[1] /
                                    'profiles/persistence.spec.json').read_text(encoding='utf-8'))

    class PersistedTaskBrowser(FakeBrowser):
        def __init__(self, workspace):
            super().__init__(workspace)
            self.task_status = self.saved_status = 'Todo'
            self.filter = 'All'
            self.frames = []

        async def action(self, action):
            raw = await super().action(action)
            if action.kind == 'navigate':
                self.task_status, self.filter = self.saved_status, 'All'
            elif action.kind == 'click' and action.locator.role == 'checkbox':
                if not complete_only or self.task_status != 'Done':
                    self.task_status = 'Done' if self.task_status == 'Todo' else 'Todo'
                    self.saved_status = self.task_status
            elif action.kind == 'click' and action.locator.name == 'Todo':
                self.filter = 'Todo'
            snapshot = ('- heading "Task board" [ref=e1]\n'
                        '- button "Add task" [ref=e2]\n'
                        '- button "Todo" [ref=e3]\n')
            if self.filter != 'Todo' or self.task_status == 'Todo':
                snapshot += '- checkbox "Complete Write project brief" [ref=e4]'
                if self.task_status == 'Done':
                    snapshot += ' [checked]'
            raw['snapshot'] = snapshot
            self.frames.append((action.kind, self.task_status, self.filter))
            return raw

    engine.browser = PersistedTaskBrowser(engine.workspace)
    original_command = engine.runner.command

    async def reset_scenario(kind):
        if kind == 'reset':
            engine.browser.task_status = engine.browser.saved_status = 'Todo'
            engine.browser.filter = 'All'
        return await original_command(kind)

    engine.runner.command = reset_scenario
    engine.workspace.path('src/value.ts', write=True).write_bytes(
        b'export const persisted = true;\n')
    state.execution_mode = 'batch'
    state.phase, state.validation_index = Phase.VERIFY, 4
    state.reproduced = state.source_aligned = True
    state.environment_digest = 'profile-regression-environment'
    state.patch_hash = digest(engine.workspace.diff().encode())
    state.test_spec_ref = engine.put(state, spec.model_dump())
    state.test_spec_hash = digest(spec)
    state.replay_plan_ref = engine.put(state, [action.model_dump() for action in spec.regression_plan[:2]])
    for kind in ('static', 'unit', 'build', 'health'):
        artifact_ref = engine.put(state, {'kind': kind, 'passed': True})
        validation = Validation(kind=kind, passed=True, source_manifest=state.source_manifest,
            patch_hash=state.patch_hash, environment_digest=state.environment_digest,
            test_spec_hash=state.test_spec_hash, artifact_ref=artifact_ref)
        state.validation_refs.append(engine.put(state, validation.model_dump()))
    engine.store.save(state)

    for step in range(len(spec.regression_plan) + 7):
        if state.phase != Phase.VERIFY:
            break
        output = await engine.verify(state, None)
        state = RunState(**output['data'])

    validations = [engine.get(state, ref) for ref in state.validation_refs]
    original = next(validation for validation in validations if validation['kind'] == 'original')
    regression = next(validation for validation in validations if validation['kind'] == 'regression')
    assert original['passed'] is True
    assert regression['passed'] is (not complete_only)
    assert state.phase == (Phase.DIAGNOSE if complete_only else Phase.FINALIZE)
    regression_result = engine.get(state, regression['artifact_ref'])
    assert [check['passed'] for check in regression_result['assertions']] == [True, True, not complete_only]
    assert engine.browser.frames[-6:] == [
        ('navigate', 'Todo', 'All'), ('click', 'Done', 'All'), ('navigate', 'Done', 'All'),
        ('click', 'Done' if complete_only else 'Todo', 'All'),
        ('navigate', 'Done' if complete_only else 'Todo', 'All'),
        ('click', 'Done' if complete_only else 'Todo', 'Todo')]
