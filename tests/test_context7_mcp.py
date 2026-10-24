import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
import httpx
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig, load_claude_sdk_config
from tracefix.connectors import context7_mcp as connector_module
from tracefix.connectors.context7_mcp import (
    CONTEXT7_ORIGIN, CONTEXT7_TOOLS, CONTEXT7_URL, Context7MCPConfig,
    Context7MCPConnector, Context7MCPError, context7_enabled, sdk_context7_servers,
)
from tracefix.runtime.contracts import Phase, digest
from tracefix.tools.context7 import Context7QueryInput, Context7ResolveInput, register_context7_tools
from tracefix.tools.core import ToolPipeline, ToolRegistry, ToolRejected, build_tool


ROOT = Path(__file__).resolve().parents[1]
SDK_ENV = {'TRACEFIX_CLAUDE_MODEL': 'claude-sonnet-4-6',
    'TRACEFIX_CLAUDE_CREDENTIAL_REF': 'env:ANTHROPIC_API_KEY',
    'TRACEFIX_SDK_WORKER_IMAGE': 'sha256:' + 'a' * 64}


def load_script(relative, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def listing():
    return ListToolsResult(tools=[Tool(name=name, inputSchema={
        'type': 'object', 'properties': {field: {'type': 'string'} for field in fields},
        'required': list(fields), 'additionalProperties': False})
        for name, fields in CONTEXT7_TOOLS.items()])


@pytest.fixture
def protocol(monkeypatch):
    state = SimpleNamespace(events=[], listing=listing(), error=False, content='useEffect 示例',
        fail=False, delay=False, calls=[])

    @asynccontextmanager
    async def transport(url, **kwargs):
        assert url == CONTEXT7_URL
        assert kwargs['http_client'].follow_redirects is False
        state.events.append('transport.open')
        try:
            yield (None, None, None)
        finally:
            state.events.append('transport.close')

    class Session:
        def __init__(self, *streams):
            pass

        async def __aenter__(self):
            state.events.append('session.open')
            return self

        async def __aexit__(self, *error):
            state.events.append('session.close')

        async def initialize(self):
            if state.fail:
                raise RuntimeError('private-provider-response')
            return SimpleNamespace(serverInfo=SimpleNamespace(name='Context7', version='test'))

        async def list_tools(self, cursor=None):
            state.events.append('list')
            return state.listing

        async def send_ping(self):
            state.events.append('ping')

        async def call_tool(self, name, arguments):
            state.calls.append((name, arguments))
            if state.delay:
                await asyncio.Event().wait()
            return CallToolResult(isError=state.error, content=[TextContent(type='text', text=state.content)])

    monkeypatch.setattr(connector_module, 'streamable_http_client', transport)
    monkeypatch.setattr(connector_module, 'ClientSession', Session)
    return state


async def test_protocol_probe_checks_both_tools_ping_and_closes(protocol):
    report = await Context7MCPConnector().probe()
    assert report['status'] == 'passed'
    assert report['tools'] == sorted(CONTEXT7_TOOLS)
    assert protocol.calls == []
    assert protocol.events == ['transport.open', 'session.open', 'list', 'ping',
        'session.close', 'transport.close']


async def test_document_response_has_bounded_content_and_provenance(protocol):
    protocol.content = '文' * 1000
    connector = Context7MCPConnector(Context7MCPConfig(max_response_bytes=1024))
    document = await connector.call('query-docs', {'libraryId': '/facebook/react', 'query': 'useEffect'})
    assert protocol.calls == [('query-docs', {'libraryId': '/facebook/react', 'query': 'useEffect'})]
    assert document['untrusted'] is True
    assert document['reproduction_status'] == 'not_evidence_of_reproduction'
    assert document['truncated'] is True
    assert document['provenance']['returned_bytes'] <= 1024
    assert document['provenance']['original_bytes'] == 3000
    assert document['provenance']['content_digest'] == digest(document['content'])
    assert document['provenance']['library_id'] == '/facebook/react'
    assert document['provenance']['retrieved_at']


@pytest.mark.parametrize('kind,code', [
    ('missing_tool', 'tool_contract_invalid'), ('wrong_schema', 'tool_contract_invalid'),
    ('remote_error', 'remote_tool_error'), ('empty', 'empty_response'), ('network', 'request_failed'),
])
async def test_failure_is_explicit_and_does_not_expose_provider_response(protocol, kind, code):
    if kind == 'missing_tool':
        protocol.listing.tools.pop()
    if kind == 'wrong_schema':
        protocol.listing.tools[0].inputSchema['properties']['query']['type'] = 'number'
    if kind == 'remote_error':
        protocol.error = True
    if kind == 'empty':
        protocol.content = ''
    if kind == 'network':
        protocol.fail = True
    with pytest.raises(Context7MCPError) as failure:
        await Context7MCPConnector().call('query-docs', {'libraryId': '/facebook/react', 'query': 'hooks'})
    assert failure.value.code == code
    assert 'private-provider-response' not in str(failure.value)
    assert protocol.events[-2:] == ['session.close', 'transport.close']


async def test_cancellation_closes_session_and_is_not_converted_to_tool_failure(protocol):
    protocol.delay = True
    task = asyncio.create_task(Context7MCPConnector().call('query-docs', {
        'libraryId': '/facebook/react', 'query': 'hooks'}))
    while not protocol.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert protocol.events[-2:] == ['session.close', 'transport.close']


async def test_arbitrary_tool_is_rejected_before_connecting(protocol):
    with pytest.raises(Context7MCPError, match='tool_not_allowed'):
        await Context7MCPConnector().call('execute', {})
    assert protocol.events == []


@pytest.mark.parametrize('url', ['http://mcp.context7.com/mcp', 'https://evil.example/mcp',
    'https://key@mcp.context7.com/mcp', CONTEXT7_URL + '?key=value'])
def test_only_official_endpoint_is_accepted(url):
    with pytest.raises(ValueError):
        Context7MCPConfig(url=url)


def test_sdk_configuration_freezes_mcp_origin_without_changing_payload_schema():
    config = load_claude_sdk_config(SDK_ENV)
    assert config.context7_enabled is True
    assert CONTEXT7_ORIGIN in config.egress_allowlist
    assert 'context7_enabled' not in config.model_dump()
    assert sdk_context7_servers(config) == {'context7': {'type': 'http', 'url': CONTEXT7_URL}}
    restored = ClaudeSdkConfig.model_validate(config.model_dump())
    assert sdk_context7_servers(restored) == sdk_context7_servers(config)
    assert restored.config_hash == config.config_hash
    disabled = load_claude_sdk_config(SDK_ENV | {'TRACEFIX_CONTEXT7_ENABLED': '0'})
    assert sdk_context7_servers(disabled) == {}
    original = ClaudeSdkConfig.model_validate({**disabled.model_dump()})
    assert original.config_hash == disabled.config_hash


def test_explicit_sdk_allowlist_is_respected():
    with pytest.raises(ValueError, match='TRACEFIX_SDK_EGRESS_ALLOWLIST'):
        load_claude_sdk_config(SDK_ENV | {'TRACEFIX_SDK_EGRESS_ALLOWLIST': 'https://api.anthropic.com'})
    configured = SDK_ENV | {'TRACEFIX_SDK_EGRESS_ALLOWLIST': 'https://api.anthropic.com,' + CONTEXT7_ORIGIN}
    assert load_claude_sdk_config(configured).context7_enabled
    assert not load_claude_sdk_config(configured | {'TRACEFIX_CONTEXT7_ENABLED': 'false'}).context7_enabled
    with pytest.raises(ValueError):
        context7_enabled({'TRACEFIX_CONTEXT7_ENABLED': 'invalid'})


def test_nested_transport_failures_keep_stable_error_codes():
    response = httpx.Response(429, request=httpx.Request('POST', CONTEXT7_URL))
    error = httpx.HTTPStatusError('private-provider-response', request=response.request, response=response)
    nested = ExceptionGroup('transport', [ExceptionGroup('session', [error])])
    assert connector_module._failure_code(nested) == 'rate_limited'
    assert connector_module._failure_code(ExceptionGroup('session', [
        Context7MCPError('remote_tool_error')])) == 'remote_tool_error'
    assert connector_module._failure_code(TimeoutError()) == 'timeout'


def test_main_runtime_exposes_context7_tools_with_normal_policy_filters(tmp_path, monkeypatch):
    from tracefix.runtime.contracts import Decision
    from tracefix.runtime.smoke import make_engine
    from tracefix.tools.handlers import build_runtime_tools

    monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '1')
    engine, state = make_engine(tmp_path / 'engine')
    state = state.model_copy(update={'phase': Phase.EXPLORE})
    runtime = build_runtime_tools(engine, state, Decision)
    assert runtime.registry.contains('Context7ResolveLibraryId', Phase.EXPLORE)
    assert runtime.registry.contains('Context7QueryDocs', Phase.EXPLORE)
    spec = runtime.registry.get('Context7QueryDocs')
    assert spec.side_effect == 'read' and spec.parallel_safe
    worker = build_runtime_tools(engine, state, Decision, context={
        'worker_depth': 1, 'worker_allowed_tools': []})
    assert not worker.registry.contains('Context7QueryDocs')


async def test_harness_binds_context7_for_initializer_and_coder_from_frozen_config(tmp_path, monkeypatch):
    from tests import test_harness_graph as fixtures

    def config(**fields):
        return ClaudeSdkConfig(**(fields | {'egress_allowlist': [
            'https://api.anthropic.com', CONTEXT7_ORIGIN]}))

    monkeypatch.setattr(fixtures, 'ClaudeSdkConfig', config)
    with TemporaryDirectory(prefix='c7-', dir=Path.cwd().anchor) as directory:
        engine, state, harness, executor = await fixtures.create_harness_engine(Path(directory))
        monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '0')
        output = await engine.run(state)
        assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
        assert {request.role for request in executor.requests} == {'initializer', 'coder'}
        for request in executor.requests:
            assert request.mcp_servers == {'context7': {'type': 'http', 'url': CONTEXT7_URL}}
            assert 'mcp__context7__query-docs' in request.system_prompt
            assert CONTEXT7_ORIGIN in request.config.egress_allowlist


async def test_real_tool_pipeline_invokes_connector_and_returns_untrusted_context(protocol, monkeypatch):
    monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '1')
    registry, handlers = ToolRegistry(), {}

    def bind(name, description, input_model, handler, **options):
        definition = build_tool(name, description, input_model, handler, **options)
        registry.register(definition.spec)
        handlers[name] = definition.handler

    register_context7_tools(None, None, {}, bind)
    pipeline = ToolPipeline(registry, handlers, Phase.EXPLORE)
    result = await pipeline.execute('Context7QueryDocs', {
        'libraryId': '/facebook/react', 'query': 'useEffect cleanup'}, 'docs-call')
    assert protocol.calls[0][0] == 'query-docs'
    assert result.executed and not result.is_error
    assert result.result['untrusted'] is True


def test_disabled_tools_are_not_registered(monkeypatch):
    monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '0')
    registered = []
    register_context7_tools(None, None, {}, lambda *args, **options: registered.append(args))
    assert registered == []


async def test_tool_failure_becomes_recoverable_rejection(protocol, monkeypatch):
    monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '1')
    protocol.error = True
    handlers = {}
    register_context7_tools(None, None, {}, lambda name, description, model, handler, **options:
        handlers.update({name: handler}))
    with pytest.raises(ToolRejected, match='remote_tool_error'):
        await handlers['context7.query-docs'](Context7QueryInput(libraryId='/facebook/react', query='hooks'), 'call')


def test_deployment_failure_is_nonzero_and_report_contains_no_raw_exception(tmp_path, monkeypatch, capsys):
    module = load_script('scripts/context7/deploy_context7_mcp.py', 'context7_deployment_test')
    monkeypatch.setenv('TRACEFIX_CONTEXT7_ENABLED', '1')
    monkeypatch.setattr(module, 'ROOT', tmp_path)

    async def fail(verify):
        raise RuntimeError('private-provider-response')

    monkeypatch.setattr(module, 'inspect_deployment', fail)
    path = tmp_path / 'deployment.json'
    assert module.main(['--prepare', '--report', str(path)]) == 2
    assert json.loads(path.read_text())['deployment']['status'] == 'failed'
    captured = capsys.readouterr()
    assert 'private-provider-response' not in captured.out + captured.err + path.read_text()


async def test_document_verification_failure_preserves_successful_protocol_probe(monkeypatch):
    module = load_script('scripts/context7/deploy_context7_mcp.py', 'context7_verification_test')

    async def probe(self):
        return {'status': 'passed'}

    async def call(self, name, arguments):
        raise Context7MCPError('rate_limited')

    monkeypatch.setattr(module.Context7MCPConnector, 'probe', probe)
    monkeypatch.setattr(module.Context7MCPConnector, 'call', call)
    report = await module.inspect_deployment(verify=True)
    assert report['deployment']['status'] == 'passed'
    assert report['real_read'] == {'status': 'failed', 'code': 'rate_limited'}


@pytest.mark.parametrize('arguments', [[], ['--console-only']])
def test_startup_deploys_context7_before_database_even_when_skipping_install(tmp_path, monkeypatch, arguments):
    from tests.test_web_startup import bootstrap_fixture

    module, calls, _ = bootstrap_fixture(tmp_path, monkeypatch)
    assert module.main([*arguments, '--skip-install', '--skip-build', '--plain']) == 0
    deployment = [index for index, command in enumerate(calls)
        if 'scripts/context7/deploy_context7_mcp.py' in command]
    if '--console-only' in arguments:
        assert deployment == []
    else:
        assert len(deployment) == 1
        database = next(index for index, command in enumerate(calls) if 'postgres' in command)
        assert deployment[0] < database
        assert calls[deployment[0]][-1] == '--prepare'


def test_mcp_startup_failure_prevents_database_and_services(tmp_path, monkeypatch):
    from tests.test_web_startup import bootstrap_fixture

    module, calls, _ = bootstrap_fixture(tmp_path, monkeypatch)
    original = module.run

    def fail(argv, **kwargs):
        if 'scripts/context7/deploy_context7_mcp.py' in argv:
            raise RuntimeError('Context7 MCP unavailable')
        return original(argv, **kwargs)

    monkeypatch.setattr(module, 'run', fail)
    with pytest.raises(RuntimeError, match='Context7 MCP unavailable'):
        module.main(['--skip-install', '--skip-build', '--plain'])
    assert not any('postgres' in command for command in calls)
    assert not any('scripts/bootstrap/services.py' in command for command in calls)
