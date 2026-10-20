"""Controlled Finding acceptance specifications and independent execution proofs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, model_validator

from tracefix.runtime.contracts import (
    Assertion, BehaviorScenario, BehaviorStep, BrowserAction, Contract, Locator,
    ObservableWait, Outcome, RunState, TestSpec, digest,
)
from tracefix.storage.artifacts import Artifacts


_HASH = r'^[a-f0-9]{64}$'
_COMMIT = r'^(?:[a-f0-9]{40}|[a-f0-9]{64})$'
_IDENTITY = r'^[A-Za-z0-9_-]{1,80}$'


class AcceptanceContract(Contract):
    model_config = ConfigDict(extra='forbid', strict=True)


class AcceptanceLocator(Locator, AcceptanceContract):
    role: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=1024)


class AcceptanceAssertion(Assertion, AcceptanceContract):
    locator: AcceptanceLocator


class AcceptanceWait(ObservableWait, AcceptanceContract):
    assertions: list[AcceptanceAssertion] = Field(default_factory=list, max_length=32)


class AcceptanceAction(BrowserAction, AcceptanceContract):
    locator: AcceptanceLocator | None = None
    value: str | None = Field(default=None, max_length=8192)
    preconditions: list[AcceptanceAssertion] = Field(default_factory=list, max_length=32)
    postconditions: list[AcceptanceAssertion] = Field(default_factory=list, max_length=32)
    wait: AcceptanceWait | None = None

    @model_validator(mode='after')
    def controlled_action(self):
        if self.kind == 'finish' or any(value is not None for value in (
                self.observation_id, self.element_ref, self.page_generation)):
            raise ValueError('验收只允许未绑定的受控浏览器动作')
        return self


class AcceptanceStep(BehaviorStep, AcceptanceContract):
    action: AcceptanceAction
    assertions: list[AcceptanceAssertion] = Field(default_factory=list, max_length=32)


class AcceptanceScenario(BehaviorScenario, AcceptanceContract):
    id: str = Field(pattern=_IDENTITY)
    description: str = Field(default='', max_length=2000)
    steps: list[AcceptanceStep] = Field(min_length=1, max_length=64)


class AcceptanceSpec(TestSpec, AcceptanceContract):
    mode: Literal['gui'] = 'gui'
    goal: str = Field(min_length=5, max_length=16384)
    preconditions: list[str] = Field(default_factory=list, max_length=32)
    executable_preconditions: list[AcceptanceAssertion] = Field(default_factory=list, max_length=32)
    assertions: list[AcceptanceAssertion] = Field(min_length=1, max_length=32)
    regression_plan: list[AcceptanceAction] = Field(min_length=1, max_length=64)
    regression_assertions: list[AcceptanceAssertion] = Field(min_length=1, max_length=32)
    behavior_scenarios: list[AcceptanceScenario] = Field(min_length=4, max_length=64)

    @model_validator(mode='after')
    def no_model_acknowledgement(self):
        if self.guidance_ack:
            raise ValueError('冻结验收规范不能包含模型确认结果')
        return self


class AcceptanceGuard(AcceptanceContract):
    kind: Literal['normal', 'reverse', 'refresh']
    scenario_id: str = Field(pattern=_IDENTITY)


class AcceptanceFinding(AcceptanceContract):
    finding_id: str = Field(min_length=1, max_length=160)
    fingerprint: str = Field(min_length=1, max_length=1024)
    expected: str = Field(min_length=1, max_length=8192)
    actual: str = Field(min_length=1, max_length=8192)
    rule_id: str | None = Field(default=None, min_length=1, max_length=120)
    rule_version: int | None = Field(default=None, ge=1)
    evidence_refs: list[str] = Field(min_length=1, max_length=100)
    original_scenario_id: str = Field(pattern=_IDENTITY)
    guards: list[AcceptanceGuard] = Field(min_length=3, max_length=32)

    @model_validator(mode='after')
    def complete_guards(self):
        if bool(self.rule_id) != (self.rule_version is not None):
            raise ValueError('来源规则 id/version 必须一起提供')
        if set(guard.kind for guard in self.guards) != {'normal', 'reverse', 'refresh'}:
            raise ValueError('验收必须包含正常、逆向和刷新行为')
        ids = [self.original_scenario_id, *(guard.scenario_id for guard in self.guards)]
        if len(ids) != len(set(ids)) or len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError('问题场景、保护场景和证据引用不能重复')
        return self


class FindingAcceptance(AcceptanceContract):
    schema_version: Literal[1]
    run_id: str = Field(pattern=_IDENTITY)
    repo_id: str = Field(pattern=_IDENTITY)
    commit: str = Field(pattern=_COMMIT)
    source_run_id: str = Field(pattern=_IDENTITY)
    source_scope_id: str = Field(pattern=_IDENTITY)
    source_repo_id: str = Field(min_length=1, max_length=256)
    source_commit: str = Field(pattern=_COMMIT)
    source_manifest: str = Field(pattern=_HASH)
    source_rule_snapshot_hash: str = Field(pattern=_HASH)
    allow_revision_rebind: bool = False
    findings: list[AcceptanceFinding] = Field(min_length=1, max_length=16)
    spec: AcceptanceSpec
    spec_hash: str = Field(pattern=_HASH)

    @model_validator(mode='before')
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and type(value.get('schema_version')) is not int:
            raise ValueError('验收 schema_version 必须为整数 1')
        return value

    @model_validator(mode='after')
    def frozen_spec(self):
        if type(self.schema_version) is not int:
            raise ValueError('验收 schema_version 必须为整数 1')
        if digest(self.spec) != self.spec_hash:
            raise ValueError('验收规范 hash 与受控规范不一致')
        if self.commit != self.source_commit and not self.allow_revision_rebind:
            raise ValueError('验收 revision 改变必须显式允许重新绑定')
        ids = [finding.finding_id for finding in self.findings]
        originals = [finding.original_scenario_id for finding in self.findings]
        if len(ids) != len(set(ids)) or len(originals) != len(set(originals)):
            raise ValueError('每个 Finding 必须唯一绑定独立问题场景')
        referenced = {scenario_id for finding in self.findings for scenario_id in (
            finding.original_scenario_id, *(guard.scenario_id for guard in finding.guards))}
        scenarios = {scenario.id: scenario for scenario in self.spec.behavior_scenarios}
        if referenced != set(scenarios):
            raise ValueError('每个可执行场景必须绑定 Finding，且绑定不能缺失')
        for finding in self.findings:
            for guard in finding.guards:
                if guard.kind == 'refresh' and not any(
                        step.action.kind == 'navigate' for step in scenarios[guard.scenario_id].steps[1:]):
                    raise ValueError('刷新验收必须重新导航后检查持久状态')
        return self


@dataclass(frozen=True)
class FrozenFindingAcceptance:
    payload_json: str
    acceptance_hash: str
    source_hash: str

    @property
    def acceptance(self) -> FindingAcceptance:
        payload = FindingAcceptance.model_validate_json(self.payload_json)
        if digest(payload) != self.acceptance_hash:
            raise ValueError('冻结验收正文已变化')
        return payload

    @property
    def spec(self) -> AcceptanceSpec:
        return self.acceptance.spec


@dataclass(frozen=True)
class FindingAcceptanceResult:
    outcome: Outcome
    artifact_ref: str
    reason: str


class AcceptanceRunner(Protocol):
    async def run(self, spec: TestSpec, *, url: str, artifacts_dir: Path,
                  binding: dict[str, Any], reset_path: str | None = None,
                  include_regression: bool = True) -> dict[str, Any]: ...


def _source_identity(state: RunState) -> dict:
    source = state.repair_source
    if source is None or state.mode != 'repair' or state.run_id == source.source_run_id:
        raise ValueError('验收必须绑定独立 Finding Repair')
    mutable = {'status', 'repair_run_id', 'patch_hash', 'verification_refs'}
    return {**source.model_dump(mode='json', exclude={'target_base_commit', 'revision_rebound', 'findings'}),
        'findings': [{key: value for key, value in finding.items() if key not in mutable}
                     for finding in source.findings]}


def freeze_finding_acceptance(payload: dict | FindingAcceptance, state: RunState,
                             target_snapshot: dict) -> FrozenFindingAcceptance:
    """Validate a specification without claiming that any Finding was reproduced."""
    accepted = FindingAcceptance.model_validate(payload.model_dump(mode='json')
        if isinstance(payload, FindingAcceptance) else payload)
    source = state.repair_source
    identity = _source_identity(state)
    expected = {'run_id': state.run_id, 'repo_id': state.scope_id,
        'commit': target_snapshot.get('commit'), 'source_run_id': source.source_run_id,
        'source_scope_id': source.source_scope_id, 'source_repo_id': source.source_repo_id,
        'source_commit': source.source_base_commit, 'source_manifest': source.source_manifest,
        'source_rule_snapshot_hash': source.source_rule_snapshot_hash}
    if any(getattr(accepted, key) != value for key, value in expected.items()):
        raise ValueError('验收来源 Run、仓库、revision 或冻结快照不一致')
    if (state.scope_id != source.source_scope_id or state.repo_id != source.source_repo_id
            or accepted.spec.goal != state.goal):
        raise ValueError('验收目标与独立 Repair 的项目、仓库或目标不一致')
    if [finding.finding_id for finding in accepted.findings] != source.source_finding_ids:
        raise ValueError('验收必须逐一覆盖选中的 Finding')
    for item, finding in zip(accepted.findings, source.findings):
        if any(getattr(item, key) != finding.get(key) for key in (
                'fingerprint', 'expected', 'actual', 'rule_id', 'rule_version')):
            raise ValueError('验收 Finding 正文或规则版本与冻结来源不一致')
        if item.evidence_refs != finding.get('evidence_refs') or not set(item.evidence_refs) <= source.source_evidence_hashes.keys():
            raise ValueError('验收 Finding 证据不属于来源证据闭包')
    origin = urlsplit(state.url)
    if origin.scheme not in {'http', 'https'} or origin.username or origin.password:
        raise ValueError('验收需要无凭据的 HTTP 应用地址')
    actions = [*accepted.spec.regression_plan,
        *(step.action for scenario in accepted.spec.behavior_scenarios for step in scenario.steps)]
    for action in actions:
        if action.kind == 'navigate':
            destination = urlsplit(action.value or '')
            if (destination.scheme, destination.hostname, destination.port) != (
                    origin.scheme, origin.hostname, origin.port) or destination.username or destination.password:
                raise ValueError('验收导航不能离开冻结应用 origin')
    return FrozenFindingAcceptance(accepted.model_dump_json(), digest(accepted), digest(identity))


def _validate_current(frozen: FrozenFindingAcceptance, state: RunState, artifacts: Artifacts) -> FindingAcceptance:
    accepted = frozen.acceptance
    if (state.run_id != accepted.run_id or state.scope_id != accepted.repo_id
            or digest(_source_identity(state)) != frozen.source_hash):
        raise ValueError('当前 Repair 与冻结验收来源不一致')
    source = state.repair_source
    if source.target_base_commit != accepted.commit:
        raise ValueError('当前 Repair revision 与冻结验收不一致')
    if state.test_spec_hash and state.test_spec_hash != accepted.spec_hash:
        raise ValueError('当前 TestSpec 与冻结断言不一致')
    for item in accepted.findings:
        for reference in item.evidence_refs:
            if digest(artifacts.read(source.source_scope_id, source.source_run_id, reference)) != source.source_evidence_hashes[reference]:
                raise ValueError('来源 Finding 证据已经变化')
    return accepted


def _binding(frozen, accepted, state, stage):
    return {'acceptance_hash': frozen.acceptance_hash, 'run_id': state.run_id,
        'scope_id': state.scope_id, 'repo_id': accepted.source_repo_id, 'commit': accepted.commit,
        'source_run_id': accepted.source_run_id, 'source_manifest': state.source_manifest,
        'environment_digest': state.environment_digest,
        'spec_hash': accepted.spec_hash, 'stage': stage,
        'patch_hash': state.patch_hash if stage == 'verify' else None}


def _evidence_inventory(report: dict, directory: Path) -> dict[str, str]:
    references = report.get('evidence_refs', [])
    if not isinstance(references, list) or not references:
        raise ValueError('执行报告缺少运行时证据')
    root = directory.resolve()
    inventory = {}
    for reference in references:
        if not isinstance(reference, str):
            raise ValueError('执行证据引用无效')
        candidate = Path(reference)
        path = candidate if candidate.is_absolute() else root / candidate
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise ValueError('执行证据必须位于该独立验收目录')
        inventory[str(path.resolve())] = digest(path.read_bytes())
    return inventory


def _judge_report(accepted: FindingAcceptance, report: dict, binding: dict, stage: str) -> tuple[Outcome, str]:
    if (report.get('schema_version') != 'tracefix.playwright-acceptance/1'
            or report.get('binding') != binding or report.get('status') not in {'pass', 'fail'}):
        return Outcome.INCONCLUSIVE, '执行报告缺失、环境未就绪或绑定不一致'
    cases = report.get('cases')
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        return Outcome.INCONCLUSIVE, '执行报告缺少逐场景结果'
    ids = [case.get('id') for case in cases]
    if len(ids) != len(set(ids)):
        return Outcome.INCONCLUSIVE, '执行报告包含重复场景'
    by_id = {case.get('id'): case for case in cases}
    if set(by_id) != {scenario.id for scenario in accepted.spec.behavior_scenarios}:
        return Outcome.INCONCLUSIVE, '执行报告的场景集合与冻结规范不一致'
    originals = {item.original_scenario_id for item in accepted.findings}
    for scenario in accepted.spec.behavior_scenarios:
        case = by_id.get(scenario.id, {})
        if (case.get('scenario_hash') != digest(scenario) or not case.get('evidence_refs')
                or case.get('status') not in {'pass', 'fail'}
                or case.get('failure_kind') in {'action', 'infrastructure'}):
            return Outcome.INCONCLUSIVE, '场景结果缺证据、规范不一致或执行失败：' + scenario.id
        checks = case.get('checks', [])
        if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
            return Outcome.INCONCLUSIVE, '场景缺少逐断言结果：' + scenario.id
        expected = {('precondition', 0, index) for index in range(len(accepted.spec.executable_preconditions))}
        for step_index, step in enumerate(scenario.steps, start=1):
            contexts = {'checkpoint': step.assertions, 'action_precondition': step.action.preconditions,
                'action_postcondition': step.action.postconditions,
                'wait': step.action.wait.assertions if step.action.wait else []}
            expected.update((context, step_index, index) for context, assertions in contexts.items()
                            for index in range(len(assertions)))
        actual = [(check.get('context'), check.get('step'), check.get('assertion_index')) for check in checks]
        if (any(check.get('failure_kind') in {'action', 'infrastructure'} for check in checks)
                or any(type(check.get('passed')) is not bool for check in checks)
                or any(type(check.get('step')) is not int or type(check.get('assertion_index')) is not int
                       for check in checks)
                or len(actual) != len(set(actual)) or not set(actual) <= expected):
            return Outcome.INCONCLUSIVE, '断言报告不完整或存在执行故障：' + scenario.id
        failed = [check for check in checks if check['passed'] is False]
        if any(check.get('failure_kind') != 'assertion' for check in failed):
            return Outcome.INCONCLUSIVE, '失败没有确定性断言证据：' + scenario.id
        if (case['status'] == 'fail' and not failed) or (case['status'] == 'pass' and failed):
            return Outcome.INCONCLUSIVE, '场景状态与逐断言结果不一致：' + scenario.id
        if stage == 'baseline' and scenario.id in originals:
            final = len(scenario.steps)
            if case['status'] != 'fail' or not any(
                    check['step'] == final and check['context'] == 'checkpoint' for check in failed):
                return Outcome.INCONCLUSIVE, '原问题没有在最终业务断言独立复现：' + scenario.id
            required = {checkpoint for checkpoint in expected
                        if not (checkpoint[1] == final and checkpoint[0] == 'checkpoint')}
            if (any(check['step'] != final or check['context'] != 'checkpoint' for check in failed)
                    or not required <= set(actual)):
                return Outcome.INCONCLUSIVE, '原问题的中间状态失败，无法确认同一问题：' + scenario.id
        elif stage == 'verify':
            if failed or case['status'] != 'pass':
                return Outcome.BUG_CONFIRMED, '原问题或保护行为仍失败：' + scenario.id
            if set(actual) != expected:
                return Outcome.INCONCLUSIVE, '通过结果缺少必需断言：' + scenario.id
    expected_status = 'fail' if any(case['status'] == 'fail' for case in cases) else 'pass'
    if report['status'] != expected_status or report.get('passed') is not (expected_status == 'pass'):
        return Outcome.INCONCLUSIVE, '总体执行状态与逐场景结果不一致'
    return (Outcome.BUG_CONFIRMED, '修补前独立复现全部选中问题') if stage == 'baseline' else (
        Outcome.FIX_VERIFIED, '同一冻结问题及正常、逆向、刷新行为全部通过')


async def _execute(frozen, state, runner, artifacts, artifacts_dir, stage, reset_path):
    accepted = _validate_current(frozen, state, artifacts)
    if not state.source_manifest or not state.environment_digest:
        raise ValueError('独立验收需要已建立的源码和执行环境冻结摘要')
    binding = _binding(frozen, accepted, state, stage)
    try:
        report = await runner.run(accepted.spec, url=state.url, artifacts_dir=artifacts_dir,
            binding=dict(binding), reset_path=reset_path, include_regression=False)
    except (OSError, ValueError, TimeoutError):
        report = {'schema_version': 'tracefix.playwright-acceptance/1', 'binding': binding,
            'status': 'inconclusive', 'passed': None, 'cases': [], 'evidence_refs': [],
            'error': '独立验收执行器未能完成受控执行'}
    accepted = _validate_current(frozen, state, artifacts)
    inventory = {}
    try:
        inventory = _evidence_inventory(report, artifacts_dir)
        evidence = set(report.get('evidence_refs', []))
        if any(not set(case.get('evidence_refs', [])) <= evidence for case in report.get('cases', [])):
            raise ValueError('场景证据不属于执行证据集合')
        outcome, reason = _judge_report(accepted, report, binding, stage)
    except (OSError, ValueError, TypeError, AttributeError):
        outcome, reason = Outcome.INCONCLUSIVE, '执行证据缺失、无法读取或不属于独立验收目录'
    report_ref = artifacts.put(state.scope_id, state.run_id, report, label='独立验收执行')
    proof = {'schema_version': 1, 'binding': binding, 'source_hash': frozen.source_hash,
        'report_ref': report_ref, 'report_hash': digest(artifacts.read(state.scope_id, state.run_id, report_ref)),
        'evidence_hashes': inventory, 'outcome': str(outcome), 'reason': reason}
    reference = artifacts.put(state.scope_id, state.run_id, proof,
        label='问题独立复现冻结' if stage == 'baseline' else '问题冻结验收结果')
    return FindingAcceptanceResult(outcome, reference, reason)


async def reproduce_finding_acceptance(frozen: FrozenFindingAcceptance, state: RunState, *,
        runner: AcceptanceRunner, artifacts: Artifacts, artifacts_dir: Path,
        reset_path: str | None = None) -> FindingAcceptanceResult:
    """Execute the independent pre-patch baseline; no request-supplied success is read."""
    if state.patch_hash or state.patch_ref or state.staged_candidate_refs:
        raise ValueError('独立复现必须在候选补丁产生前执行')
    return await _execute(frozen, state, runner, artifacts, artifacts_dir, 'baseline', reset_path)


def read_finding_reproduction(frozen: FrozenFindingAcceptance, state: RunState, *,
                              artifacts: Artifacts, reproduction_ref: str) -> dict:
    """Re-read the runtime-owned baseline report and every frozen evidence hash."""
    accepted = _validate_current(frozen, state, artifacts)
    proof = artifacts.json(state.scope_id, state.run_id, reproduction_ref)
    expected = _binding(frozen, accepted, state, 'baseline')
    if (proof.get('schema_version') != 1 or proof.get('binding') != expected
            or proof.get('source_hash') != frozen.source_hash
            or proof.get('outcome') != str(Outcome.BUG_CONFIRMED)
            or not proof.get('evidence_hashes')):
        raise ValueError('缺少当前独立 Repair 的修补前冻结复现证据')
    raw = artifacts.read(state.scope_id, state.run_id, proof['report_ref'])
    if digest(raw) != proof.get('report_hash'):
        raise ValueError('冻结复现报告已经变化')
    for reference, expected_hash in proof['evidence_hashes'].items():
        path = Path(reference)
        if path.is_symlink() or digest(path.read_bytes()) != expected_hash:
            raise ValueError('冻结复现证据已经变化')
    report = artifacts.json(state.scope_id, state.run_id, proof['report_ref'])
    outcome, _ = _judge_report(accepted, report, expected, 'baseline')
    if outcome != Outcome.BUG_CONFIRMED:
        raise ValueError('冻结报告不能证明同一问题已复现')
    return proof


async def verify_finding_acceptance(frozen: FrozenFindingAcceptance, state: RunState, *,
        runner: AcceptanceRunner, artifacts: Artifacts, artifacts_dir: Path,
        reproduction_ref: str, reset_path: str | None = None) -> FindingAcceptanceResult:
    """Execute the unchanged specification against the bound candidate patch."""
    if not state.patch_hash:
        raise ValueError('修补后验收必须绑定候选 patch hash')
    read_finding_reproduction(frozen, state, artifacts=artifacts, reproduction_ref=reproduction_ref)
    return await _execute(frozen, state, runner, artifacts, artifacts_dir, 'verify', reset_path)
