from __future__ import annotations

import asyncio
import copy
import os
from pathlib import Path
from uuid import uuid4

from tracefix.connectors import GitHubMCPConfig, GitHubMCPConnector, GitHubReadRequest
from tracefix.execution.runner import DockerRunner
from tracefix.service.config import OFFICIAL_GITHUB_IMAGE
from tracefix.service.results import ServiceError
from tracefix.runtime.contracts import Outcome, digest
from tracefix.runtime.finding_acceptance import (
    FindingAcceptanceResult, read_finding_reproduction, reproduce_finding_acceptance,
    verify_finding_acceptance,
)
from tracefix.runtime.playwright_acceptance import PlaywrightAcceptanceRunner


class RunServices:
    def __init__(self, request, settings, *, connector_factory=GitHubMCPConnector,
                 acceptance_runner_factory=PlaywrightAcceptanceRunner):
        self.run_id, self.repo_id = request.run_id, request.repo_id
        self.request, self.settings = request, settings
        self.connector_factory = connector_factory
        self.connector = None
        self._documents = []
        self._resources = []
        self._close_task = None
        self._prepared = False
        self._context_consumed = False
        self.finding_acceptance = None
        self.cleanup_errors = []
        self.session = None
        self.acceptance_runner_factory = acceptance_runner_factory

    def bind_session(self, session):
        if self.session is not None and self.session is not session:
            raise ServiceError('acceptance_binding_invalid', '验收服务不能更换所属 Session')
        self.session = session

    def _acceptance_session(self, state):
        accepted = self.finding_acceptance
        if (accepted is None or accepted.frozen is None or self.session is None
                or self.session.engine is None or state.run_id != self.run_id
                or state.scope_id != self.repo_id or self._close_task is not None):
            raise ServiceError('finding_acceptance_invalid', '验收不属于当前活动独立 Repair')
        return accepted, self.session

    async def _execute_acceptance(self, state, *, verify):
        accepted, session = self._acceptance_session(state)
        reset_path = os.getenv('TRACEFIX_SERVICE_ACCEPTANCE_RESET_PATH')
        if (not isinstance(session.engine.runner, DockerRunner)
                and (not reset_path or not reset_path.startswith('/') or reset_path.startswith('//'))):
            reference = session.artifacts.put(state.scope_id, state.run_id,
                {'outcome': str(Outcome.INCONCLUSIVE), 'reason': '部署未配置受控验收 reset endpoint'},
                label='独立验收配置缺失')
            return FindingAcceptanceResult(Outcome.INCONCLUSIVE, reference, '部署未配置受控验收 reset endpoint')
        runner = self.acceptance_runner_factory(docker_runner=session.engine.runner)
        directory = self.settings.data_root / 'finding-acceptance' / state.run_id / (
            ('verify-' if verify else 'baseline-') + uuid4().hex)
        options = {'runner': runner, 'artifacts': session.artifacts, 'artifacts_dir': directory,
            'reset_path': reset_path}
        if verify:
            if not accepted.reproduction_ref:
                raise ServiceError('finding_reproduction_missing', '验收缺少修补前独立复现')
            result = await verify_finding_acceptance(accepted.frozen, state,
                reproduction_ref=accepted.reproduction_ref, **options)
            accepted.verification_ref = result.artifact_ref
        else:
            if accepted.reproduction_ref:
                read_finding_reproduction(accepted.frozen, state, artifacts=session.artifacts,
                    reproduction_ref=accepted.reproduction_ref)
                return FindingAcceptanceResult(Outcome.BUG_CONFIRMED, accepted.reproduction_ref, '已有同一冻结独立复现')
            result = await reproduce_finding_acceptance(accepted.frozen, state, **options)
            if result.outcome == Outcome.BUG_CONFIRMED:
                accepted.reproduction_ref = result.artifact_ref
        return result

    async def reproduce_finding_acceptance(self, state):
        return await self._execute_acceptance(state, verify=False)

    async def verify_finding_acceptance(self, state):
        return await self._execute_acceptance(state, verify=True)

    def finding_acceptance_reproduced(self, state):
        try:
            accepted, session = self._acceptance_session(state)
            if not accepted.reproduction_ref:
                return False
            read_finding_reproduction(accepted.frozen, state, artifacts=session.artifacts,
                reproduction_ref=accepted.reproduction_ref)
            return True
        except (ServiceError, OSError, ValueError, KeyError, TypeError):
            return False

    def finding_acceptance_verified(self, state):
        from tracefix.runtime.finding_acceptance import _binding, _judge_report, _validate_current
        try:
            accepted, session = self._acceptance_session(state)
            if not accepted.verification_ref or not self.finding_acceptance_reproduced(state):
                return False
            spec = _validate_current(accepted.frozen, state, session.artifacts)
            proof = session.artifacts.json(state.scope_id, state.run_id, accepted.verification_ref)
            binding = _binding(accepted.frozen, spec, state, 'verify')
            if (not state.patch_hash or proof.get('binding') != binding
                    or proof.get('source_hash') != accepted.frozen.source_hash
                    or proof.get('outcome') != str(Outcome.FIX_VERIFIED) or not proof.get('evidence_hashes')):
                return False
            raw = session.artifacts.read(state.scope_id, state.run_id, proof['report_ref'])
            if digest(raw) != proof['report_hash']:
                return False
            for reference, expected_hash in proof['evidence_hashes'].items():
                path = Path(reference)
                if path.is_symlink() or digest(path.read_bytes()) != expected_hash:
                    return False
            report = session.artifacts.json(state.scope_id, state.run_id, proof['report_ref'])
            return _judge_report(spec, report, binding, 'verify')[0] == Outcome.FIX_VERIFIED
        except (ServiceError, OSError, ValueError, KeyError, TypeError):
            return False

    def own(self, resource):
        if self._close_task is not None:
            raise ServiceError('resources_closed', 'Run 资源已经关闭')
        self._resources.append(resource)

    async def prepare(self):
        if self._prepared or self._close_task is not None:
            raise ServiceError('resources_invalid', 'Run 上下文只可初始化一次')
        if self.request.github_reads:
            config = GitHubMCPConfig.from_snapshot(
                self.request.repository_snapshot.model_dump(exclude_none=True), command=('docker',),
                args=('run', '--rm', '-i', '--name', 'tf-gh-' + self.run_id,
                      '-e', 'GITHUB_PERSONAL_ACCESS_TOKEN', OFFICIAL_GITHUB_IMAGE,
                      'stdio', '--read-only', '--tools=get_commit,get_file_contents,issue_read,pull_request_read'),
                timeout_seconds=self.settings.github_timeout,
                max_response_bytes=self.settings.github_max_bytes,
                env_keys=('PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'TEMP', 'TMP'))
            self.connector = self.connector_factory(config, self.settings.resolve_credential)
            self.own(self.connector)
            await self.connector.open()
            seen = set()
            for read in self.request.github_reads:
                identity = (read.source_kind, read.path, read.number)
                if identity in seen:
                    continue
                seen.add(identity)
                context = await self.connector.read_context(GitHubReadRequest(**read.model_dump()))
                document = context.to_context_document()
                document['provenance']['run_id'] = self.run_id
                self._documents.append(document)
        self._prepared = True

    async def reference_documents(self, state):
        if (state.run_id != self.run_id or state.scope_id != self.repo_id
                or not self._prepared or self._close_task is not None):
            raise ServiceError('context_binding_invalid', 'GitHub 上下文不属于活动 Run')
        self._context_consumed = True
        return copy.deepcopy(self._documents)

    def require_context_consumed(self):
        if self.request.github_reads and not self._context_consumed:
            raise ServiceError('runtime_services_not_integrated', 'Engine 尚未消费本 Run 的 GitHub 上下文接缝')

    async def _close(self):
        for resource in reversed(self._resources):
            try:
                await asyncio.wait_for(resource.close(), 10)
            except BaseException:
                self.cleanup_errors.append({'code': 'resource_close_failed', 'resource': type(resource).__name__})
        self._resources.clear()
        self._documents.clear()

    async def aclose(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._close_task)
            raise
