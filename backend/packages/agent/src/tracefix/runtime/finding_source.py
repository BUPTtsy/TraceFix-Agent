"""Explicit Finding provenance and independent Repair execution boundaries."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import re

from pydantic import Field

from tracefix.rules.models import Finding, RuleSnapshot
from tracefix.rules.resolver import RuleResolver
from tracefix.runtime.continuation import derive_run
from tracefix.runtime.contracts import Contract, FindingRepairSource, RunState, digest
from tracefix.storage.artifacts import Artifacts


class SourceRunIdentity(Contract):
    run_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)


RunIdentityResolver = Callable[[str], SourceRunIdentity | Sequence[SourceRunIdentity]]
RunLoader = Callable[[str, str], RunState]
FindingLoader = Callable[[str], Finding]
_ARTIFACT_REF = re.compile(r'(?:[a-f0-9]{64}|[0-9]{4,}_[A-Za-z0-9_\-\u4e00-\u9fff]{1,100})\.(?:json|txt|png|html|diff)')


def resolve_source_run(source_run_id: str, *, resolve_run_id: RunIdentityResolver,
                       load_run: RunLoader) -> RunState:
    """Resolve a caller identity only through an explicit, unique adapter."""
    resolved = resolve_run_id(source_run_id)
    identities = [resolved] if isinstance(resolved, SourceRunIdentity) else list(resolved)
    if len(identities) != 1 or not isinstance(identities[0], SourceRunIdentity):
        raise ValueError('来源 Run 必须显式解析为唯一内部 Run/scope 身份')
    identity = identities[0]
    source_run = RunState.model_validate(load_run(identity.run_id, identity.scope_id).model_dump(mode='json'))
    if (source_run.run_id, source_run.scope_id) != (identity.run_id, identity.scope_id):
        raise ValueError('解析的来源 Run 与读取的内部身份不一致')
    return source_run


def evidence_closure(source_run: RunState, artifacts: Artifacts) -> dict[str, str]:
    """Read every reachable source evidence artifact with integrity checking."""
    pending = list(dict.fromkeys([
        *source_run.evidence_refs, *source_run.initial_check_result_refs,
        *source_run.check_result_refs, *source_run.validation_refs,
        *source_run.baseline_validation_refs, *source_run.behavior_check_refs,
        source_run.observation_ref,
    ]))
    hashes = {}

    def references(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from references(item)
        elif isinstance(value, list):
            for item in value:
                yield from references(item)
        elif isinstance(value, str) and _ARTIFACT_REF.fullmatch(value):
            yield value

    while pending:
        reference = pending.pop()
        if not reference or reference in hashes:
            continue
        if not artifacts.exists(source_run.scope_id, source_run.run_id, reference):
            raise ValueError('来源证据闭包缺失或完整性校验失败：' + reference)
        raw = artifacts.read(source_run.scope_id, source_run.run_id, reference)
        hashes[reference] = digest(raw)
        if reference.endswith('.json'):
            pending.extend(references(json.loads(raw)))
    return hashes


def _source_material(source_run: RunState, source_finding_ids: list[str], *,
                     load_finding: FindingLoader, artifacts: Artifacts,
                     rule_resolver: RuleResolver) -> tuple[FindingRepairSource, list[Finding]]:
    if not source_finding_ids or len(source_finding_ids) != len(set(source_finding_ids)):
        raise ValueError('必须选择唯一且非空的来源 Finding 集合')
    if not source_run.repo_snapshot_ref or not source_run.source_manifest:
        raise ValueError('来源 Run 没有可读的冻结源码快照')
    repo_snapshot = artifacts.json(source_run.scope_id, source_run.run_id, source_run.repo_snapshot_ref)
    if not isinstance(repo_snapshot, dict) or digest(repo_snapshot) != source_run.source_manifest:
        raise ValueError('来源 Run 的源码快照 hash 不一致')
    scope_id = source_run.scope_id
    project_id = source_run.project_id or scope_id
    repo_id = source_run.repo_id or repo_snapshot.get('repo_id')
    if (repo_snapshot.get('scope_id') != scope_id
            or repo_snapshot.get('project_id', project_id) != project_id
            or repo_snapshot.get('repo_id') != repo_id):
        raise ValueError('来源 Run 的源码快照项目/仓库归属不一致')
    base_commit = repo_snapshot.get('commit')
    if not isinstance(base_commit, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', base_commit):
        raise ValueError('来源 Run 的原仓库 base commit 不可读')
    workspace_commit = (repo_snapshot.get('workspace_repository') or {}).get('head')
    if source_run.patch_base_commit and workspace_commit != source_run.patch_base_commit:
        raise ValueError('来源 Run 的导出工作区 baseline 与快照不一致')
    if not source_run.rule_snapshot_ref or not source_run.rule_snapshot_hash:
        raise ValueError('来源 Run 没有可读的冻结规则快照')
    snapshot = RuleSnapshot.model_validate(artifacts.json(
        scope_id, source_run.run_id, source_run.rule_snapshot_ref))
    if snapshot.run_id != source_run.run_id or snapshot.hash != source_run.rule_snapshot_hash:
        raise ValueError('来源 Run 的规则快照身份或 hash 不一致')
    rules = rule_resolver.load_snapshot_rules(snapshot, scope_id=scope_id,
        project_id=project_id, repo_id=repo_id)
    frozen_rules = {rule.id: rule for rule in rules}
    hashes = evidence_closure(source_run, artifacts)
    replay_plan = artifacts.json(scope_id, source_run.run_id, source_run.replay_plan_ref) if source_run.replay_plan_ref else []
    if not isinstance(replay_plan, list) or any(not isinstance(item, dict) for item in replay_plan):
        raise ValueError('来源重现计划不可读')
    feedback = []
    for reference in source_run.diagnosis_feedback_refs:
        value = artifacts.json(scope_id, source_run.run_id, reference)
        feedback.append({'ref': reference, 'result': value})
    if source_run.error or source_run.error_details:
        feedback.append({'error': source_run.error, 'details': source_run.error_details})
    findings = []
    contexts = []
    selected_refs = []
    for finding_id in source_finding_ids:
        finding = Finding.model_validate(load_finding(finding_id).model_dump(mode='json'))
        if finding.id != finding_id or not finding.run_id or finding.run_id != source_run.run_id:
            raise ValueError('来源 Finding 必须属于指定的 source Run：' + finding_id)
        if not finding.scope_id or finding.scope_id != scope_id:
            raise ValueError('来源 Finding 的 scope 归属无法确认或不一致：' + finding_id)
        if (finding.project_id is not None and finding.project_id != project_id
                or finding.repo_id is not None and finding.repo_id != repo_id):
            raise ValueError('来源 Finding 不属于同一项目/仓库：' + finding_id)
        if finding.source_base_commit != base_commit or finding.source_manifest != source_run.source_manifest:
            raise ValueError('来源 Finding 的 commit/manifest 与 source Run 不一致：' + finding_id)
        if not finding.evidence_refs or not set(finding.evidence_refs) <= hashes.keys():
            raise ValueError('来源 Finding 的证据不在 source Run 证据闭包中：' + finding_id)
        rule = frozen_rules.get(finding.rule_id) if finding.rule_id else None
        if finding.rule_id and (rule is None or finding.rule_version != rule.version or finding.source != rule.source):
            raise ValueError('来源 Finding 的规则版本/来源不属于冻结快照：' + finding_id)
        if not finding.rule_id and finding.source in {'oracle', 'static'}:
            raise ValueError('确定性 Finding 缺少来源规则：' + finding_id)
        expected = finding.expected or finding.details.get('expected', '')
        actual = finding.actual or finding.details.get('actual', '')
        if not expected or not actual:
            raise ValueError('来源 Finding 缺少预期/实际表现：' + finding_id)
        contexts.append({**finding.model_dump(mode='json'), 'expected': expected, 'actual': actual,
            'rule': rule.model_dump(mode='json') if rule else None,
            'fix_guidance': rule.fix_guidance if rule else '',
            'replay_plan': replay_plan, 'feedback': feedback})
        selected_refs.extend(finding.evidence_refs)
        findings.append(finding)
    rule_resolver.require_repair_coverage(snapshot, [finding.rule_id for finding in findings if finding.rule_id])
    return FindingRepairSource(source_run_id=source_run.run_id, source_finding_ids=source_finding_ids,
        source_scope_id=scope_id, source_project_id=project_id, source_repo_id=repo_id,
        source_base_commit=base_commit, source_workspace_base_commit=workspace_commit,
        source_manifest=source_run.source_manifest, source_repo_snapshot_ref=source_run.repo_snapshot_ref,
        source_repo_snapshot=repo_snapshot, source_rule_snapshot_ref=source_run.rule_snapshot_ref,
        source_rule_snapshot_hash=snapshot.hash, source_rule_snapshot=snapshot.model_dump(mode='json'),
        source_rule_refs=[reference.model_dump(mode='json') for reference in snapshot.refs],
        source_evidence_refs=list(dict.fromkeys(selected_refs)), source_evidence_hashes=hashes,
        findings=contexts, replay_plan=replay_plan, failure_feedback=feedback,
        target_base_commit=base_commit), findings


def create_finding_repair_run(source_run_id: str, source_finding_ids: list[str], *,
        resolve_run_id: RunIdentityResolver, load_run: RunLoader, load_finding: FindingLoader,
        artifacts: Artifacts, rule_resolver: RuleResolver, goal: str | None = None,
        url: str | None = None, rule_ids: list[str] | None = None,
        additional_rule_ids: list[str] | None = None, run_id: str | None = None,
        target_base_commit: str | None = None) -> RunState:
    """Create an unbound independent Repair; a runtime factory exports its source commit."""
    source_run = resolve_source_run(source_run_id, resolve_run_id=resolve_run_id, load_run=load_run)
    source, _ = _source_material(source_run, source_finding_ids, load_finding=load_finding,
        artifacts=artifacts, rule_resolver=rule_resolver)
    if target_base_commit is not None and target_base_commit != source.source_base_commit:
        raise ValueError('更换 revision 必须显式调用 rebind_finding_repair_revision 并重新生成 observation/TestSpec/replay')
    repair = derive_run(source_run, goal=goal, url=url, mode='repair', rule_ids=rule_ids,
        additional_rule_ids=additional_rule_ids, run_id=run_id)
    selected_snapshot, _ = rule_resolver.resolve_snapshot(run_id=repair.run_id,
        project_id=source.source_project_id, scope_id=source.source_scope_id, repo_id=source.source_repo_id,
        phase='*', parent=RuleSnapshot.model_validate(source.source_rule_snapshot),
        rule_ids=rule_ids or [], additional=additional_rule_ids or [])
    rule_resolver.require_repair_coverage(selected_snapshot, [*(rule_ids or []), *(additional_rule_ids or [])])
    return RunState.model_validate({**repair.model_dump(mode='json'), 'project_id': source.source_project_id,
        'repo_id': source.source_repo_id, 'repair_source': source, 'repo_snapshot_ref': '',
        'source_manifest': '', 'memory_snapshot_ref': '', 'agent_instructions_ref': None,
        'agent_instructions_hash': None, 'agent_instructions_path': None})


def validate_finding_repair_source(state: RunState, *, load_run: RunLoader,
        load_finding: FindingLoader, artifacts: Artifacts, rule_resolver: RuleResolver) -> list[Finding]:
    """Re-read provenance before execution or a fixed-status write."""
    source = state.repair_source
    if source is None or state.mode != 'repair':
        raise ValueError('Finding Repair 缺少结构化来源')
    source_run = load_run(source.source_run_id, source.source_scope_id)
    if (source_run.run_id, source_run.scope_id) != (source.source_run_id, source.source_scope_id):
        raise ValueError('来源 Run 的内部身份不一致')
    current, findings = _source_material(source_run, source.source_finding_ids,
        load_finding=load_finding, artifacts=artifacts, rule_resolver=rule_resolver)
    if state.scope_id != current.source_scope_id or state.run_id == current.source_run_id:
        raise ValueError('Finding Repair 必须是同 scope 的独立新 Run')
    if (state.project_id is not None and state.project_id != current.source_project_id
            or state.repo_id is not None and state.repo_id != current.source_repo_id):
        raise ValueError('Finding Repair 项目/仓库与来源不一致')
    for field in ('source_project_id', 'source_repo_id', 'source_base_commit',
                  'source_workspace_base_commit', 'source_manifest', 'source_repo_snapshot_ref',
                  'source_repo_snapshot', 'source_rule_snapshot_ref', 'source_rule_snapshot_hash',
                  'source_rule_snapshot', 'source_rule_refs', 'source_evidence_refs', 'source_evidence_hashes',
                  'replay_plan', 'failure_feedback'):
        if getattr(source, field) != getattr(current, field):
            raise ValueError('Finding Repair 来源已变化或无法确认：' + field)
    mutable_fields = {'status', 'repair_run_id', 'patch_hash', 'verification_refs'}
    for frozen, readable in zip(source.findings, current.findings):
        if {key: value for key, value in frozen.items() if key not in mutable_fields} != {
                key: value for key, value in readable.items() if key not in mutable_fields}:
            raise ValueError('Finding Repair 的问题正文/来源已经变化：' + readable['id'])
    if not source.revision_rebound and source.target_base_commit != source.source_base_commit:
        raise ValueError('Finding Repair 未显式重新绑定 revision')
    return findings


def rebind_finding_repair_revision(state: RunState, *, repo_snapshot: dict,
        repo_snapshot_ref: str, artifacts: Artifacts) -> RunState:
    """Bind an explicitly exported revision and discard all execution conclusions."""
    if state.mode != 'repair' or state.repair_source is None:
        raise ValueError('只有 Finding Repair 可以显式绑定 revision')
    if (repo_snapshot.get('scope_id') != state.scope_id
            or repo_snapshot.get('repo_id') != state.repair_source.source_repo_id
            or repo_snapshot.get('project_id', state.project_id or state.scope_id) != (state.project_id or state.scope_id)):
        raise ValueError('重新绑定的源码快照不属于同一项目/仓库')
    readable = artifacts.json(state.scope_id, state.run_id, repo_snapshot_ref)
    if readable != repo_snapshot:
        raise ValueError('重新绑定的源码快照引用/正文不一致')
    commit = repo_snapshot.get('commit')
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', commit):
        raise ValueError('重新绑定的 revision 不可读')
    if not repo_snapshot.get('workspace_repository') or not repo_snapshot.get('files'):
        raise ValueError('重新绑定需要新导出的独立工作区快照')
    reset = derive_run(state, mode='repair', rule_ids=state.rule_ids,
        additional_rule_ids=state.additional_rule_ids, run_id=state.run_id)
    source = state.repair_source.model_copy(deep=True, update={'target_base_commit': commit,
        'revision_rebound': commit != state.repair_source.source_base_commit})
    return RunState.model_validate({**reset.model_dump(mode='json'),
        'parent_run_id': state.parent_run_id, 'revision': state.revision + 1,
        'repair_source': source, 'repo_snapshot_ref': repo_snapshot_ref,
        'source_manifest': digest(repo_snapshot), 'memory_snapshot_ref': '',
        'agent_instructions_ref': None, 'agent_instructions_hash': None, 'agent_instructions_path': None})
