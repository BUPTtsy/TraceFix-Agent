import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

import pytest

from tracefix.runtime.contracts import BrowserAction, RunState, Validation, digest, verification_gate
from tracefix.execution.browser import assertions
from tracefix.runtime.smoke import PNG
from tracefix.runtime.verification import verification_binding
from tracefix.storage.artifacts import Artifacts


ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "behavior_checker", ROOT / "tools/checks/verify_real_e2e.py"
)
checker = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(checker)


@pytest.fixture
def specification():
    return json.loads((ROOT / "profiles/persistence.spec.json").read_text(encoding="utf-8"))


@pytest.fixture
def workspace():
    parent = ROOT / ".tracefix"
    parent.mkdir(exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="behavior-checker-", dir=parent))
    try:
        yield destination
    finally:
        shutil.rmtree(destination)


def test_frozen_spec_retains_original_bytes_after_source_changes(workspace, specification):
    source = workspace / "source.json"
    original = json.dumps(specification).encode()
    source.write_bytes(original)
    frozen, digest = checker.freeze_spec(workspace / "run", source)
    source.write_text('{"assertions": []}', encoding="utf-8")
    assert frozen.read_bytes() == original
    assert digest == hashlib.sha256(original).hexdigest()
    with pytest.raises(FileExistsError):
        checker.freeze_spec(workspace / "run", frozen)


@pytest.mark.parametrize("payload", [{}, {"assertions": []}, [], "invalid"])
def test_freezing_rejects_missing_original_assertions(workspace, payload):
    source = workspace / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        checker.freeze_spec(workspace / "run", source)
    assert not (workspace / "run/frozen-spec.json").exists()


def test_agent_command_uses_frozen_spec_without_oracle_feedback(workspace):
    frozen = workspace / "frozen-spec.json"
    command = checker.agent_command(workspace / "projects.json", workspace / "data", frozen)
    assert command[command.index("--spec") + 1] == frozen
    assert command[command.index("--execution-mode") + 1] == "batch"
    assert not any("oracle" in str(argument).lower() for argument in command)


def test_run_freezes_spec_before_initializing_source(workspace, monkeypatch, specification):
    source = workspace / "source.json"
    contents = json.dumps(specification).encode()
    source.write_bytes(contents)
    monkeypatch.setattr(checker, "ROOT", workspace)
    monkeypatch.setattr(checker, "postgres_connection_config", lambda: ("postgresql://unused", ""))
    called = []

    def stop_at_initialization(argv, **kwargs):
        called.append(argv)
        assert argv[1] == "bugboard/scripts/init_demo.py"
        frozen = Path(argv[-1]).parent / "frozen-spec.json"
        assert frozen.read_bytes() == contents
        raise RuntimeError("initialization boundary reached")

    monkeypatch.setattr(checker, "command", stop_at_initialization)
    arguments = checker.argparse.Namespace(case="B01", spec=source)
    output = workspace / "report.json"
    with pytest.raises(RuntimeError, match="initialization boundary reached"):
        checker.run_flow(arguments, output)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert len(called) == 1
    assert report["status"] == "failed"
    assert report["frozen_spec_sha256"] == hashlib.sha256(contents).hexdigest()
    assert (workspace / report["frozen_spec"]).read_bytes() == contents


@pytest.mark.parametrize("weakness", ["no_scenario", "completion_only", "visible_only", "wrong_original"])
def test_freezing_rejects_b01_without_full_round_trip(workspace, specification, weakness):
    if weakness == "no_scenario":
        specification["behavior_scenarios"] = []
    elif weakness == "completion_only":
        specification["behavior_scenarios"][0]["steps"][3]["assertions"][0]["condition"] = "checked"
    elif weakness == "visible_only":
        for step in specification["behavior_scenarios"][0]["steps"]:
            for assertion in step["assertions"]:
                assertion["condition"] = "visible"
    else:
        specification["assertions"][0]["condition"] = "visible"
    source = workspace / "source.json"
    source.write_text(json.dumps(specification), encoding="utf-8")
    with pytest.raises(ValueError, match="B01 冻结规范"):
        checker.freeze_spec(workspace / "run", source)
    assert not (workspace / "run/frozen-spec.json").exists()


@pytest.mark.parametrize("malformation", ["unauthorized", "stale_binding", "finish", "no_final_assertion"])
def test_freezing_semantically_validates_before_initialization(workspace, monkeypatch, specification, malformation):
    steps = specification["behavior_scenarios"][0]["steps"]
    if malformation == "unauthorized":
        specification["authorized_actions"].remove("click")
    elif malformation == "stale_binding":
        steps[1]["action"]["observation_id"] = "old-observation"
    elif malformation == "finish":
        steps[-1]["action"] = {"kind": "finish"}
    else:
        steps[-1]["assertions"] = []
    source = workspace / "source.json"
    source.write_text(json.dumps(specification), encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", workspace)
    monkeypatch.setattr(checker, "postgres_connection_config", lambda: ("postgresql://unused", ""))
    calls = []
    monkeypatch.setattr(checker, "command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        checker.run_flow(checker.argparse.Namespace(case="B01", spec=source), workspace / "report.json")
    assert calls == []
    assert not list(workspace.glob(".tracefix/real-e2e/*/frozen-spec.json"))


@pytest.fixture
def verified_run(workspace, specification):
    source = workspace / "source.json"
    source.write_text(json.dumps(specification), encoding="utf-8")
    frozen, _ = checker.freeze_spec(workspace / "run", source)
    spec = checker.validate_spec(frozen.read_bytes(), case="B01")
    state = RunState(scope_id="bugboard", goal=spec.goal, url="http://app:3000/",
                     run_status="COMPLETED", outcome="FIX_VERIFIED", reproduced=True,
                     source_aligned=True, reproduction_plan_frozen=True,
                     source_manifest="source-current", patch_hash="patch-current",
                     environment_digest="environment-current", test_spec_hash=digest(spec))
    artifacts = Artifacts(workspace / "artifacts")

    def put(value, ext="json"):
        return artifacts.put(state.scope_id, state.run_id, value, ext)

    state.test_spec_ref = put(spec.model_dump(mode="json"))
    plan = [BrowserAction.model_validate(action).model_dump(mode="json") for action in [
        {"kind": "click", "locator": {"role": "checkbox", "name": "Complete Write project brief"}},
        {"kind": "navigate", "value": "http://app:3000/"}]]
    state.replay_plan_ref = put(plan)
    state.evidence_refs = [put({"reproduced": True})]
    binding = verification_binding(state, digest(plan))
    shared = {key: value for key, value in binding.items() if key != "plan_hash"}

    def gui_result(checks):
        elements = {}
        for assertion in checks:
            if assertion.condition != "absent":
                attributes = elements.setdefault((assertion.locator.role, assertion.locator.name), set())
                if assertion.condition in {"checked", "disabled"}:
                    attributes.add(f"[{assertion.condition}]")
        snapshot = "\n".join(f'- {role} "{name}" [ref=element-{index}] ' + " ".join(sorted(attributes))
                             for index, ((role, name), attributes) in enumerate(elements.items()))
        observation = {**binding, "type": "gui_observation", "snapshot": snapshot,
                       "screenshot_ref": put(PNG, "png"), "screenshot_hash": digest(PNG)}
        evaluated = assertions(snapshot, checks)
        assert evaluated["passed"]
        return {**evaluated, "observation_ref": put(observation), "observation_hash": digest(observation)}

    for kind in sorted(checker.REQUIRED_VALIDATIONS):
        result = {**binding, "type": "validation_result", "kind": kind, "passed": True}
        if kind in {"original", "regression"}:
            result.update(gui_result(spec.assertions if kind == "original" else spec.regression_assertions),
                          execution_plan_hash=digest(plan) if kind == "original" else digest(
                              [action.model_dump(mode="json") for action in spec.regression_plan]))
        else:
            result.update(exit_code=0, output=f"CI fixture simulated {kind} completed")
        validation = Validation(kind=kind, passed=True, **shared,
                                replay_plan_hash=digest(plan), artifact_ref=put(result))
        state.validation_refs.append(put(validation.model_dump()))
    for scenario in spec.behavior_scenarios:
        scenario_binding = {**binding, "scenario_id": scenario.id, "scenario_hash": digest(scenario)}
        execution_hash = digest([step.action.model_dump(mode="json") for step in scenario.steps])
        checkpoints = []
        for step_index, step in enumerate(scenario.steps, 1):
            if step.assertions:
                checkpoint = {**scenario_binding, **gui_result(step.assertions), "type": "assertion_checkpoint",
                              "kind": "behavior", "scenario_step": step_index,
                              "action_hash": digest(step.action), "execution_plan_hash": execution_hash}
                checkpoints.append(put(checkpoint))
        result_ref = put({**scenario_binding, "type": "validation_result", "kind": "behavior",
                          "passed": True, "checkpoint_refs": checkpoints, "execution_plan_hash": execution_hash,
                          "observation_ref": checkpoint["observation_ref"],
                          "observation_hash": checkpoint["observation_hash"]})
        validation = Validation(kind="behavior", passed=True, **shared,
                                replay_plan_hash=digest(plan), scenario_id=scenario.id,
                                scenario_hash=digest(scenario), artifact_ref=result_ref)
        state.validation_refs.append(put(validation.model_dump()))
    return state, artifacts, frozen


def gate(state, artifacts):
    validations = [Validation.model_validate(artifacts.json(state.scope_id, state.run_id, ref))
                   for ref in state.validation_refs]
    return verification_gate(state, validations,
        lambda ref: artifacts.exists(state.scope_id, state.run_id, ref),
        artifact_read=lambda ref: artifacts.json(state.scope_id, state.run_id, ref),
        artifact_read_bytes=lambda ref: artifacts.read(state.scope_id, state.run_id, ref))


def test_checker_accepts_fully_bound_checkpoint_evidence(verified_run):
    state, artifacts, frozen = verified_run
    assert gate(state, artifacts)
    assert checker.verify_evidence(state, artifacts, frozen, case="B01") == checker.REQUIRED_VALIDATIONS | {"behavior"}


@pytest.mark.parametrize("fault", ["six_labels_only", "scenario_hash", "source_manifest", "patch_hash",
                                  "test_spec_hash", "environment_digest", "replay_plan_hash",
                                  "missing_checkpoint", "failed_checkpoint", "checkpoint_binding",
                                  "missing_observation", "reordered_checkpoints", "weakened_assertions",
                                  "observation_hash", "action_hash", "missing_screenshot", "false_observation"])
def test_checker_rejects_false_success_with_invalid_evidence(verified_run, fault):
    state, artifacts, frozen = verified_run

    def read(reference):
        return artifacts.json(state.scope_id, state.run_id, reference)

    def put(value):
        return artifacts.put(state.scope_id, state.run_id, value)

    validation = read(state.validation_refs[-1])
    if fault == "six_labels_only":
        state.validation_refs.pop()
    elif fault in {"scenario_hash", "source_manifest", "patch_hash", "test_spec_hash", "environment_digest", "replay_plan_hash"}:
        validation[fault] = "stale-value"
        state.validation_refs[-1] = put(validation)
    else:
        result = read(validation["artifact_ref"])
        if fault == "missing_checkpoint":
            result["checkpoint_refs"].pop()
        elif fault == "reordered_checkpoints":
            result["checkpoint_refs"].reverse()
        else:
            checkpoint = read(result["checkpoint_refs"][3])
            if fault == "failed_checkpoint":
                checkpoint["passed"] = False
            elif fault == "checkpoint_binding":
                checkpoint["patch_hash"] = "stale-patch"
            elif fault == "missing_observation":
                checkpoint["observation_ref"] = "missing-observation.json"
            elif fault in {"observation_hash", "action_hash"}:
                checkpoint[fault] = "stale-value"
            elif fault in {"missing_screenshot", "false_observation"}:
                observation = read(checkpoint["observation_ref"])
                if fault == "missing_screenshot":
                    observation["screenshot_ref"] = "missing-screenshot.png"
                else:
                    observation["snapshot"] = '- checkbox "Complete Write project brief" [checked] [ref=task]'
                checkpoint["observation_ref"] = put(observation)
                checkpoint["observation_hash"] = digest(observation)
            else:
                checkpoint["assertions"][0]["assertion"]["condition"] = "visible"
            result["checkpoint_refs"][3] = put(checkpoint)
        validation["artifact_ref"] = put(result)
        state.validation_refs[-1] = put(validation)
    assert not gate(state, artifacts)
    with pytest.raises(RuntimeError, match="独立验证门禁拒绝"):
        checker.verify_evidence(state, artifacts, frozen, case="B01")


def test_checker_rejects_different_run_spec_even_with_self_consistent_hash(verified_run):
    state, artifacts, frozen = verified_run
    changed = artifacts.json(state.scope_id, state.run_id, state.test_spec_ref)
    changed["behavior_scenarios"] = []
    state.test_spec_ref = artifacts.put(state.scope_id, state.run_id, changed)
    state.test_spec_hash = digest(changed)
    with pytest.raises(RuntimeError, match="Run 规范与源码修改前冻结的规范不一致"):
        checker.verify_evidence(state, artifacts, frozen, case="B01")
