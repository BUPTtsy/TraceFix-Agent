from __future__ import annotations

from dataclasses import dataclass

from tracefix.runtime.finding_source import SourceRunIdentity, create_finding_repair_run
from tracefix.service.contracts import ServiceTestSpec
from tracefix.service.results import ServiceError


@dataclass(frozen=True)
class VerifiedFindingAcceptance:
    run_id: str
    repo_id: str
    commit: str
    source_run_id: str
    source_finding_ids: tuple[str, ...]
    spec: ServiceTestSpec
    allow_revision_rebind: bool = False


def source_identities(documents, external_id):
    records = [record for record in documents.runs()
               if external_id in {record['id'], record.get('agentRunId')}]
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
    return accepted
