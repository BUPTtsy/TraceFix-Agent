"""为独立增量携带 Finding 来源和重新冻结的验收契约。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import re
from types import SimpleNamespace

from tracefix.runtime.contracts import digest
from tracefix.runtime.finding_acceptance import (
    FrozenFindingAcceptance, _validate_current, freeze_finding_acceptance,
)
from tracefix.service.contracts import ServiceRequest
from tracefix.service.finding import RunBoundFindings, strict_acceptance_adapter, validate_acceptance
from tracefix.service.resources import RunServices


class HarnessFindingRunServices(RunServices):
    async def reference_documents(self, state):
        if not self._prepared:
            await self.prepare()
        return await super().reference_documents(state)


def validate_finding_service_factory(engine, state):
    services = getattr(engine, 'runtime_services', None)
    configured = getattr(services, 'finding_acceptance', None)
    if state.repair_source is None or not (state.finding_acceptance_ref or configured is not None):
        return
    if type(services) not in {RunServices, HarnessFindingRunServices}:
        raise PermissionError('Finding 增量需要可重建的子 Run 专属验收服务')
    if (services.run_id != state.run_id or services.repo_id != state.scope_id
            or services._close_task is not None or configured is None
            or services.session is None or services.session.engine is not engine
            or services.session.artifacts is not engine.artifacts):
        raise PermissionError('Finding 增量原验收服务身份或生命周期无效')
    if services.request.github_reads or services._resources or services.connector is not None:
        raise PermissionError('Finding 增量原服务包含异步上下文或活动资源，无法安全重建')
    request = ServiceRequest.model_validate(services.request.model_dump(mode='json'))
    if (request.run_id != state.run_id or request.repo_id != state.scope_id
            or configured.run_id != state.run_id or configured.repo_id != state.scope_id
            or configured.frozen is None or request.finding_acceptance is None
            or configured.frozen.acceptance != request.finding_acceptance):
        raise PermissionError('Finding 增量原服务请求与冻结验收身份不一致')
    if state.finding_acceptance_ref:
        persisted = FrozenFindingAcceptance(**engine.get(state, state.finding_acceptance_ref))
        if persisted != configured.frozen or persisted.acceptance_hash != state.finding_acceptance_hash:
            raise PermissionError('Finding 增量原服务冻结验收与工件不一致')
    library = getattr(engine, 'rule_library', None)
    if isinstance(library, RunBoundFindings) and library.services is not services:
        raise PermissionError('Finding 增量规则库不属于当前验收服务')


def _child_service(engine, services, child, source, frozen):
    snapshot = services.request.repository_snapshot.model_dump(exclude_none=True)
    repository = deepcopy(snapshot['repository_config'])
    repository.update(commit=source['commit'], frozen_run_id=child.run_id)
    profile = repository.get('profile')
    if isinstance(profile, dict) and 'source_commit' in profile:
        profile['source_commit'] = source['commit']
    repository['config_digest'] = digest({key: value for key, value in repository.items()
        if key not in {'config_digest', 'frozen_run_id', 'snapshot_digest'}})
    repository['snapshot_digest'] = digest({key: value for key, value in repository.items()
        if key != 'snapshot_digest'})
    snapshot.update(commit=source['commit'], config_digest=repository['config_digest'],
                    repository_config=repository)
    if 'snapshot' in snapshot:
        snapshot['snapshot'] = deepcopy(repository)
    payload = services.request.model_dump(mode='json')
    payload.update(run_id=child.run_id, repository_snapshot=snapshot,
                   finding_acceptance=frozen.acceptance.model_dump(mode='json'))
    if payload['execution'].get('spec') is not None:
        payload['execution']['spec'] = frozen.acceptance.spec.model_dump(mode='json')
    request = ServiceRequest.model_validate(payload)
    accepted = strict_acceptance_adapter(request, child, snapshot)
    accepted = validate_acceptance(accepted, request, child)
    if accepted.frozen != frozen:
        raise PermissionError('Finding 增量服务重建改变了冻结验收')
    result = HarnessFindingRunServices(request, services.settings,
        connector_factory=services.connector_factory,
        acceptance_runner_factory=services.acceptance_runner_factory)
    result.finding_acceptance = accepted
    result.bind_session(SimpleNamespace(engine=engine, artifacts=engine.artifacts))
    return result


def carry_finding_acceptance(engine, parent, child, source):
    child = child.model_copy(deep=True, update={
        'finding_acceptance_ref': None, 'finding_acceptance_hash': None,
        'finding_reproduction_ref': None, 'finding_verification_ref': None,
        'finding_acceptance_evidence_refs': [], 'repair_source': None,
    })
    if parent.repair_source is None:
        return child
    if child.run_id == parent.run_id or child.scope_id != parent.scope_id:
        raise PermissionError('Finding 增量必须使用同 scope 的独立 Run')
    services = getattr(engine, 'runtime_services', None)
    configured = getattr(services, 'finding_acceptance', None)
    if configured is not None:
        validate_finding_service_factory(engine, parent)
    if source.get('scope_id') != child.scope_id or source.get('repo_id') != parent.repair_source.source_repo_id:
        raise PermissionError('Finding 增量源码不属于冻结来源项目或仓库')
    frozen = None
    if parent.finding_acceptance_ref:
        frozen = FrozenFindingAcceptance(**engine.get(parent, parent.finding_acceptance_ref))
        if frozen.acceptance_hash != parent.finding_acceptance_hash:
            raise PermissionError('父 Run Finding 验收摘要不一致')
    elif configured is not None:
        frozen = configured.frozen
    target = source.get('commit')
    previous = parent.repair_source.target_base_commit or parent.repair_source.source_base_commit
    if not isinstance(target, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', target):
        raise PermissionError('Finding 增量缺少冻结输入提交')
    if target != previous and (frozen is None or not frozen.acceptance.allow_revision_rebind):
        raise PermissionError('Finding 增量改变 revision 缺少 allow_revision_rebind 授权')
    child.repair_source = parent.repair_source.model_copy(deep=True, update={
        'target_base_commit': target,
        'revision_rebound': target != parent.repair_source.source_base_commit,
    })
    child.repo_id = parent.repo_id
    if frozen is None:
        return child
    parent_frozen = freeze_finding_acceptance(frozen.acceptance, parent, {'commit': previous})
    if parent_frozen != frozen:
        raise PermissionError('父 Run Finding 验收来源已变化')
    accepted = _validate_current(frozen, parent, engine.artifacts)
    if child.test_spec_hash and child.test_spec_hash != accepted.spec_hash:
        raise PermissionError('Finding 增量 TestSpec 不得改变冻结验收断言')
    payload = accepted.model_dump(mode='json')
    payload.update(run_id=child.run_id, commit=target)
    rebound = freeze_finding_acceptance(payload, child, source)
    child.test_spec_hash = rebound.acceptance.spec_hash
    _validate_current(rebound, child, engine.artifacts)
    child.test_spec_ref = engine.put(child, rebound.acceptance.spec.model_dump(mode='json'), name='Finding增量冻结规范')
    child.finding_acceptance_ref = engine.put(child, asdict(rebound), name='Finding增量冻结验收')
    child.finding_acceptance_hash = rebound.acceptance_hash
    if configured is not None:
        child_services = _child_service(engine, services, child, source, rebound)
        library = getattr(engine, 'rule_library', None)
        if isinstance(library, RunBoundFindings):
            engine.rule_library = RunBoundFindings(library.library, child_services)
        engine.runtime_services = child_services
    return child
