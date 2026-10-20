from __future__ import annotations

from dataclasses import dataclass

from tracefix.model.gateway import Gateway
from tracefix.runtime.repair_mode import resolve_repair_mode_fields
from tracefix.runtime.finding_acceptance import FrozenFindingAcceptance, freeze_finding_acceptance
from tracefix.runtime.finding_source import SourceRunIdentity, create_finding_repair_run
from tracefix.service.contracts import ServiceTestSpec
from tracefix.service.results import ServiceError


@dataclass
class VerifiedFindingAcceptance:
    run_id: str
    repo_id: str
    commit: str
    source_run_id: str
    source_finding_ids: tuple[str, ...]
    spec: ServiceTestSpec
    allow_revision_rebind: bool = False
    frozen: FrozenFindingAcceptance | None = None
    reproduction_ref: str | None = None
    verification_ref: str | None = None


def strict_acceptance_adapter(request, state, snapshot):
    try:
        frozen = freeze_finding_acceptance(request.finding_acceptance, state, snapshot)
        accepted = frozen.acceptance
        return VerifiedFindingAcceptance(run_id=request.run_id, repo_id=request.repo_id,
            commit=accepted.commit, source_run_id=accepted.source_run_id,
            source_finding_ids=tuple(item.finding_id for item in accepted.findings),
            spec=ServiceTestSpec.model_validate(accepted.spec.model_dump(mode='json')),
            allow_revision_rebind=accepted.allow_revision_rebind, frozen=frozen)
    except (ValueError, TypeError, KeyError):
        raise ServiceError('finding_acceptance_invalid', 'Finding 验收规范或冻结来源无效') from None


class RunBoundFindings:
    def __init__(self, library, services):
        self.library, self.services = library, services

    def __getattr__(self, name):
        return getattr(self.library, name)

    def save_verified_finding(self, finding):
        try:
            self.services.require_context_consumed()
        except ServiceError:
            raise ValueError('必需 GitHub 上下文未被 Engine 消费，拒绝已验证 Finding 回写') from None
        return self.library.save_verified_finding(finding)


def source_identities(documents, external_id):
    records = [record for record in documents.runs()
               if external_id in {record['id'], record.get('agentRunId')}]
    if len(records) != 1:
        raise ValueError('来源 Run 必须唯一解析')
    return [SourceRunIdentity(run_id=record['agentRunId'], scope_id=record['projectId'])
            for record in records if record.get('agentRunId') and record.get('projectId')]


def finding_repair_state(request, session, rule_resolver, rule_library):
    try:
        state = create_finding_repair_run(request.source_run_id, request.source_finding_ids,
            resolve_run_id=lambda external: source_identities(session.documents, external),
            load_run=session.store.load, load_finding=rule_library.finding,
            artifacts=session.artifacts, rule_resolver=rule_resolver,
            goal=request.goal, url=request.execution.url,
            additional_rule_ids=request.additional_rule_ids, run_id=request.run_id)
        config = request.repository_snapshot.repository_config
        if (state.scope_id != request.repo_id or state.run_id == state.repair_source.source_run_id
                or state.repair_source.source_repo_id != config['owner'] + '/' + config['repo']):
            raise ValueError
        source = session.store.load(state.repair_source.source_run_id, state.repair_source.source_scope_id)
        if source.mode == 'test':
            state = state.model_copy(update=resolve_repair_mode_fields(
                requested=request.execution.repair_mode, project_config=config, run_mode='repair',
                model=Gateway(), repair_source=state.repair_source))
        return state
    except (ValueError, OSError, KeyError, PermissionError):
        raise ServiceError('finding_source_invalid', '来源 Run/Finding 或证据闭包无效') from None


def validate_acceptance(accepted, request, state):
    if not isinstance(accepted, VerifiedFindingAcceptance) or (
            accepted.run_id != request.run_id or accepted.repo_id != request.repo_id
            or accepted.commit != request.repository_snapshot.commit
            or accepted.source_run_id != state.repair_source.source_run_id
            or accepted.source_finding_ids != tuple(request.source_finding_ids)):
        raise ServiceError('finding_acceptance_invalid', 'C 验收适配与冻结 Run/来源不一致')
    ServiceTestSpec.model_validate(accepted.spec.model_dump(mode='json'))
    if accepted.frozen is None:
        raise ServiceError('finding_acceptance_invalid', 'Finding 验收缺少严格冻结规范')
    verified = strict_acceptance_adapter(request, state, request.repository_snapshot.model_dump(exclude_none=True))
    if (accepted.frozen != verified.frozen or accepted.spec != verified.spec
            or accepted.allow_revision_rebind != verified.allow_revision_rebind
            or accepted.reproduction_ref is not None or accepted.verification_ref is not None):
        raise ServiceError('finding_acceptance_invalid', '验收适配不能继承来源或请求的成功结论')
    return accepted
