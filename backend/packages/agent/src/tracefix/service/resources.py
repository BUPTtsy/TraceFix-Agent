from __future__ import annotations

import asyncio
import copy

from tracefix.connectors import GitHubMCPConfig, GitHubMCPConnector, GitHubReadRequest
from tracefix.service.config import OFFICIAL_GITHUB_IMAGE
from tracefix.service.results import ServiceError


class RunServices:
    def __init__(self, request, settings, *, connector_factory=GitHubMCPConnector):
        self.run_id, self.repo_id = request.run_id, request.repo_id
        self.request, self.settings = request, settings
        self.connector_factory = connector_factory
        self.connector = None
        self._documents = []
        self._resources = []
        self._close_task = None
        self._prepared = False
        self.cleanup_errors = []

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
        return copy.deepcopy(self._documents)

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
