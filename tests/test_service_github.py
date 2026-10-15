import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents, Tool

from tracefix.connectors import GitHubContext, GitHubMCPConnector, GitHubMCPError, GitHubReadRequest
from tracefix.runtime.contracts import RunState
from tracefix.service.config import OFFICIAL_GITHUB_IMAGE, ServiceSettings
from tracefix.service.contracts import ServiceRequest
from tracefix.service.resources import RunServices
from tests.test_github_mcp import COMMIT, MockSession, config, mcp_mock
from tests.test_service_contracts import request_payload


def official_file(content='export const value = 1;', uri=None, structured=True):
    resource = {'type': 'resource', 'resource': {'uri': uri or f'repo://owner/app/sha/{COMMIT}/contents/src/value.ts',
                                              'mimeType': 'text/plain', 'text': content}}
    blocks = [{'type': 'text', 'text': 'successfully downloaded text file (SHA: file-blob-sha)'}, resource]
    return CallToolResult(content=[TextContent(type='text', text=blocks[0]['text']),
        EmbeddedResource(type='resource', resource=TextResourceContents(**resource['resource']))],
        structuredContent={'content': blocks} if structured else None)


def test_actual_official_schema_uses_commit_sha_and_minimal_detail():
    connector = GitHubMCPConnector(config(), lambda ref: {})
    repository = {'owner': {'type': 'string'}, 'repo': {'type': 'string'}}
    connector._discover(SimpleNamespace(tools=[
        Tool(name='get_commit', inputSchema={'type': 'object', 'properties': {**repository,
            'sha': {'type': 'string'}, 'detail': {'type': 'string', 'enum': ['none', 'stats', 'full_patch']}},
            'required': ['owner', 'repo', 'sha']}),
        Tool(name='get_file_contents', inputSchema={'type': 'object', 'properties': {**repository,
            'path': {'type': 'string'}, 'ref': {'type': 'string'}, 'sha': {'type': 'string'}},
            'required': ['owner', 'repo']}),
        Tool(name='issue_read', inputSchema={'type': 'object', 'properties': {**repository,
            'issue_number': {'type': 'number'}, 'method': {'type': 'string', 'enum': ['get', 'get_comments']}},
            'required': ['owner', 'repo', 'issue_number', 'method']})]))
    assert connector._arguments('commit', {})['detail'] == 'none'
    arguments = connector._arguments('file', {'path': 'src/value.ts'})
    assert arguments['sha'] == arguments['ref'] == COMMIT
    assert connector._arguments('issue', {'number': 9})['method'] == 'get'


@pytest.mark.parametrize('structured', [False, True])
def test_official_embedded_file_response_preserves_commit_provenance(structured):
    connector = GitHubMCPConnector(config(), lambda ref: {})
    request = GitHubReadRequest('file', path='src/value.ts')
    payload = connector._payload(official_file(structured=structured), kind='file', request=request)
    connector._verify_payload(payload, 'file', request)
    assert payload['content'] == 'export const value = 1;'
    assert payload['html_url'].endswith(COMMIT + '/src/value.ts')


@pytest.mark.parametrize('uri', [
    f'repo://other/app/sha/{COMMIT}/contents/src/value.ts',
    f'repo://owner/other/sha/{COMMIT}/contents/src/value.ts',
    'repo://owner/app/sha/' + 'b' * 40 + '/contents/src/value.ts',
    'repo://owner/app/refs/heads/main/contents/src/value.ts',
    f'repo://owner/app/sha/{COMMIT}/contents/src/other.ts',
    f'repo://owner/app/sha/{COMMIT}/contents/src/value.ts?token=evil',
])
def test_official_adaptation_never_relaxes_source_binding(uri):
    connector = GitHubMCPConnector(config(), lambda ref: {})
    with pytest.raises(GitHubMCPError, match='仓库/commit/path'):
        connector._payload(official_file(uri=uri), kind='file',
                           request=GitHubReadRequest('file', path='src/value.ts'))


def test_official_issue_wrapper_and_permission_error_are_adapted():
    connector = GitHubMCPConnector(config(), lambda ref: {})
    response = CallToolResult(content=[], structuredContent={'method': 'get', 'issue': {
        'number': 9, 'title': 'Claim', 'body': 'External claim',
        'html_url': 'https://github.com/owner/app/issues/9'}})
    payload = connector._payload(response, kind='issue', request=GitHubReadRequest('issue', number=9))
    connector._verify_payload(payload, 'issue', GitHubReadRequest('issue', number=9))
    with pytest.raises(GitHubMCPError) as caught:
        connector._payload(CallToolResult(isError=True, content=[
            TextContent(type='text', text='failed to get commit: GET https://api.github.com: 401 Bad credentials SECRET')]))
    assert caught.value.code == 'permission_denied' and 'SECRET' not in str(caught.value)


async def test_cancelled_connector_open_closes_worker_and_transport(mcp_mock, monkeypatch):
    started = asyncio.Event()

    async def wait(session):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(MockSession, 'initialize', wait)
    connector = GitHubMCPConnector(config(), lambda ref: {'GITHUB_PERSONAL_ACCESS_TOKEN': 'SECRET'})
    task = asyncio.create_task(connector.open())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert connector._worker is None and connector._state == 'closed'
    assert mcp_mock.sessions[0].closed


async def test_run_services_cache_selected_reads_and_close_once(tmp_path, monkeypatch):
    payload = request_payload()
    payload['github_reads'] = [{'source_kind': 'file', 'path': 'src/value.ts'},
        {'source_kind': 'file', 'path': 'src/value.ts'}, {'source_kind': 'issue', 'number': 9}]
    request = ServiceRequest.model_validate(payload)
    settings = ServiceSettings(tmp_path, tmp_path / 'console.sqlite3', credential_env={'deployment/alpha': 'BETA_TOKEN'})
    monkeypatch.setenv('BETA_TOKEN', 'deployment-secret')
    created = []

    class Connector:
        def __init__(self, config, resolver):
            self.config, self.resolver = config, resolver
            self.calls, self.close_count = [], 0
            created.append(self)

        async def open(self):
            assert self.resolver(self.config.credential_ref) == {'GITHUB_PERSONAL_ACCESS_TOKEN': 'deployment-secret'}

        async def read_context(self, read):
            self.calls.append(read)
            return GitHubContext(repo_id='alpha', owner='owner', repo='app', commit=COMMIT, ref='main',
                source_kind=read.source_kind, source_id=read.path or str(read.number), content='untrusted text',
                content_digest='digest', retrieved_at='2026-10-11')

        async def close(self):
            self.close_count += 1

    services = RunServices(request, settings, connector_factory=Connector)
    await services.prepare()
    state = RunState(run_id=request.run_id, scope_id=request.repo_id, goal=request.goal, url='http://app')
    first = await services.reference_documents(state)
    first[0]['content'] = 'mutated'
    second = await services.reference_documents(state)
    assert second[0]['content'] == 'untrusted text' and len(created[0].calls) == 2
    assert all(item['untrusted'] and item['reproduction_status'] == 'not_evidence_of_reproduction'
               and item['provenance']['run_id'] == request.run_id for item in second)
    assert OFFICIAL_GITHUB_IMAGE in created[0].config.args
    assert '--read-only' in created[0].config.args
    assert 'deployment-secret' not in repr(created[0].config)
    state.run_id = 'another'
    with pytest.raises(Exception, match='活动 Run'):
        await services.reference_documents(state)
    await asyncio.gather(services.aclose(), services.aclose())
    assert created[0].close_count == 1 and not services.cleanup_errors


async def test_resource_ownership_closes_reverse_order_after_partial_failure(tmp_path):
    settings = ServiceSettings(tmp_path, tmp_path / 'console.sqlite3')
    services = RunServices(ServiceRequest.model_validate(request_payload()), settings)
    order = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def close(self):
            order.append(self.name)

    services.own(Resource('runner'))
    services.own(Resource('browser'))
    await services.aclose()
    assert order == ['browser', 'runner']
