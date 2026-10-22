import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefix.agents.claude_sdk_adapter import ClaudeSdkAdapter, SdkExecutionRequest
from tracefix.agents.claude_sdk_config import ClaudeSdkConfig, load_claude_sdk_config


@dataclass
class SystemMessage:
    subtype: str = 'init'
    data: dict = field(default_factory=lambda: {'session_id': 'sdk-session'})


@dataclass
class ResultMessage:
    session_id: str = 'sdk-session'
    subtype: str = 'success'
    is_error: bool = False
    usage: dict = field(default_factory=lambda: {'input_tokens': 3, 'output_tokens': 4})
    total_cost_usd: float = 0.01
    num_turns: int = 1
    structured_output: object = field(default_factory=lambda: {'summary': '完成候选', 'unresolved': []})
    terminal_reason: str | None = None
    errors: list = field(default_factory=list)


class FakeClient:
    def __init__(self, options, messages):
        self.options = options
        self.messages = messages
        self.prompts = []
        self.connected = False
        self.disconnected = False
        self.interrupted = False

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            if isinstance(message, Exception):
                raise message
            yield message

    async def disconnect(self):
        self.disconnected = True

    async def interrupt(self):
        self.interrupted = True


def request(tmp_path, **updates):
    values = dict(execution_id='execution-1', session_id='session-1', input_hash='frozen-input',
        role='coder', prompt='修复冻结问题', workspace=tmp_path,
        config=ClaudeSdkConfig(backend='claude_sdk', model_id='claude-sonnet-4-6',
            credential_ref='env:TEST_ANTHROPIC_KEY', worker_image='tracefix-sdk:0.2.168',
            egress_allowlist=['https://api.anthropic.com']),
        tools=['Read', 'Edit', 'Bash', 'Agent'])
    values.update(updates)
    return SdkExecutionRequest(**values)


def adapter(messages):
    clients = []

    def factory(options):
        client = FakeClient(options, messages)
        clients.append(client)
        return client

    sdk = SimpleNamespace(ClaudeAgentOptions=lambda **values: SimpleNamespace(**values),
        AgentDefinition=lambda **values: SimpleNamespace(**values))
    return ClaudeSdkAdapter(sdk=sdk, client_factory=factory), clients


def test_backend_default_and_missing_config_fail_before_launch():
    assert load_claude_sdk_config({}).backend == 'legacy'
    with pytest.raises(ValidationError, match='模型'):
        load_claude_sdk_config({'TRACEFIX_CODING_BACKEND': 'claude_sdk'})


@pytest.mark.parametrize('updates', [
    {'credential_ref': 'actual-secret'}, {'worker_image': 'tracefix-sdk:latest'},
    {'egress_allowlist': []}, {'egress_allowlist': ['https://*.anthropic.com']},
    {'egress_allowlist': ['https://user:secret@example.test']}, {'model_id': 'sonnet'},
])
def test_config_rejects_unfrozen_or_unsafe_values(tmp_path, updates):
    original = request(tmp_path).config.model_dump()
    with pytest.raises(ValidationError):
        ClaudeSdkConfig(**(original | updates))


SDK_ENV = {'TRACEFIX_CODING_BACKEND': 'claude_sdk', 'TRACEFIX_SDK_WORKER_IMAGE': 'sha256:' + 'a' * 64}


def test_dedicated_base_url_and_key_come_from_env_file_settings():
    config = load_claude_sdk_config(SDK_ENV | {'TRACEFIX_CLAUDE_BASE_URL': 'https://gateway.example/anthropic/',
        'TRACEFIX_CLAUDE_API_KEY': 'dedicated-secret', 'TRACEFIX_CLAUDE_MODEL': 'gateway-coder',
        'TRACEFIX_BASE_URL': 'https://api.deepseek.com', 'TRACEFIX_API_KEY': 'legacy-secret',
        'TRACEFIX_TEXT_MODEL': 'deepseek-v4-flash'})
    assert config.base_url == 'https://gateway.example/anthropic'
    assert config.credential_ref == 'env:TRACEFIX_CLAUDE_API_KEY'
    assert config.model_id == 'gateway-coder'
    assert config.egress_allowlist == ['https://gateway.example']
    assert 'dedicated-secret' not in config.model_dump_json()


def test_same_origin_gateway_reuses_existing_key_and_model():
    config = load_claude_sdk_config(SDK_ENV | {'TRACEFIX_CLAUDE_BASE_URL': 'https://api.deepseek.com/anthropic',
        'TRACEFIX_BASE_URL': 'https://api.deepseek.com', 'TRACEFIX_API_KEY': 'legacy-secret',
        'TRACEFIX_TEXT_MODEL': 'deepseek-v4-flash'})
    assert config.credential_ref == 'env:TRACEFIX_API_KEY'
    assert config.model_id == 'deepseek-v4-flash'
    assert config.resolve_credential({'TRACEFIX_API_KEY': 'legacy-secret'}) == 'legacy-secret'


@pytest.mark.parametrize('updates', [
    {'TRACEFIX_CLAUDE_BASE_URL': 'https://other.example', 'TRACEFIX_CLAUDE_MODEL': 'gateway-coder'},
    {'TRACEFIX_CLAUDE_MODEL': 'claude-sonnet-4-6'},
])
def test_existing_key_is_not_sent_to_another_origin(updates):
    with pytest.raises(ValidationError, match='凭据'):
        load_claude_sdk_config(SDK_ENV | {'TRACEFIX_BASE_URL': 'https://api.deepseek.com',
            'TRACEFIX_API_KEY': 'legacy-secret', 'TRACEFIX_TEXT_MODEL': 'deepseek-v4-flash'} | updates)


def test_direct_anthropic_defaults_to_anthropic_origin():
    config = load_claude_sdk_config(SDK_ENV | {'TRACEFIX_CLAUDE_MODEL': 'claude-sonnet-4-6',
        'TRACEFIX_CLAUDE_API_KEY': 'dedicated-secret'})
    assert config.base_url == ''
    assert config.egress_allowlist == ['https://api.anthropic.com']


@pytest.mark.parametrize('base_url', ['http://gateway.example', 'https://user:secret@gateway.example',
                                      'https://outside.example', 'https://gateway.example/?key=1'])
def test_base_url_must_be_https_without_credentials_and_allowlisted(tmp_path, base_url):
    original = request(tmp_path).config.model_dump()
    with pytest.raises(ValidationError, match='base_url'):
        ClaudeSdkConfig(**(original | {'base_url': base_url, 'egress_allowlist': ['https://gateway.example']}))


def test_empty_base_url_keeps_frozen_config_digest(tmp_path):
    config = request(tmp_path).config
    frozen = config.model_dump(mode='json')
    assert 'base_url' not in frozen
    assert ClaudeSdkConfig.model_validate(frozen).config_hash == config.config_hash
    gateway = config.model_copy(update={'base_url': 'https://api.anthropic.com/gateway'})
    assert gateway.model_dump(mode='json')['base_url'] == 'https://api.anthropic.com/gateway'
    assert gateway.config_hash != config.config_hash


async def test_gateway_base_url_and_model_aliases_reach_cli_environment(tmp_path):
    instance, clients = adapter([SystemMessage(), ResultMessage()])
    gateway = request(tmp_path).config.model_copy(update={'base_url': 'https://api.anthropic.com/gateway'})
    await instance.start(request(tmp_path, config=gateway))
    environment = clients[0].options.env
    assert environment['ANTHROPIC_BASE_URL'] == 'https://api.anthropic.com/gateway'
    assert {environment[name] for name in ('ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
        'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL')} == {gateway.model_id}
    assert 'ANTHROPIC_API_KEY' not in environment and 'ANTHROPIC_AUTH_TOKEN' not in environment
    direct, direct_clients = adapter([SystemMessage(), ResultMessage()])
    await direct.start(request(tmp_path, execution_id='execution-direct'))
    assert 'ANTHROPIC_BASE_URL' not in direct_clients[0].options.env


def test_credential_reference_is_not_serialized_as_secret(tmp_path):
    config = request(tmp_path).config
    assert config.resolve_credential({'TEST_ANTHROPIC_KEY': 'fixture-secret'}) == 'fixture-secret'
    assert 'fixture-secret' not in config.model_dump_json()
    with pytest.raises(ValueError, match='拒绝启动'):
        config.resolve_credential({})


@pytest.mark.parametrize('updates', [
    {'tools': ['Read', 'AskUserQuestion']}, {'tools': ['Read', 'Read']},
    {'role': 'initializer', 'tools': ['Read', 'Bash']},
    {'mcp_servers': {'uncontrolled': {'type': 'stdio', 'command': 'bash'}}},
    {'mcp_servers': {'uncontrolled': {'type': 'http', 'url': 'https://other.test/mcp'}}},
])
def test_request_rejects_interaction_and_uncontrolled_capabilities(tmp_path, updates):
    with pytest.raises(ValidationError):
        request(tmp_path, **updates)


async def test_start_uses_strict_tools_isolation_and_readonly_agents(tmp_path):
    instance, clients = adapter([SystemMessage(), ResultMessage()])
    events = []
    original = request(tmp_path, transcript_path=tmp_path / 'transcript.jsonl')
    result = await instance.start(original, lambda event, payload: events.append((event, payload)))
    assert result.status == 'SUCCEEDED'
    assert result.sdk_session_id == 'sdk-session'
    assert result.structured_output == {'summary': '完成候选', 'unresolved': []}
    assert result.observed_usage['usage']['input_tokens'] == 3
    options = clients[0].options
    assert options.tools == original.tools
    assert options.setting_sources == [] and options.strict_mcp_config is True
    assert options.permission_mode == 'dontAsk'
    assert options.agents['readonly'].tools == ['Read', 'Glob', 'Grep']
    assert options.agents['readonly'].mcpServers == []
    assert {'Bash', 'Write', 'Edit', 'Agent'} <= set(options.agents['readonly'].disallowedTools)
    assert 'AskUserQuestion' in options.disallowed_tools
    assert options.env['CLAUDE_CODE_DISABLE_AUTO_MEMORY'] == '1'
    assert 'CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS' not in options.env
    assert not getattr(options, 'enable_file_checkpointing', False)
    assert clients[0].disconnected
    assert (tmp_path / 'transcript.jsonl').read_text(encoding='utf-8').count('\n') == 2
    assert 'sdk.session.bound' in [event for event, payload in events]
    assert (await instance.inspect(original.execution_id)).status == 'SUCCEEDED'
    assert await instance.start(original) == result
    assert len(clients) == 1
    with pytest.raises(ValueError, match='冻结请求'):
        await instance.start(original.model_copy(update={'prompt': '另一个目标'}))


async def test_custom_initializer_schema_is_preserved(tmp_path):
    schema = {'type': 'object', 'properties': {'tasks': {'type': 'array'}}, 'required': ['tasks']}
    instance, clients = adapter([ResultMessage(structured_output={'tasks': []})])
    result = await instance.start(request(tmp_path, role='initializer', tools=['Read'], output_schema=schema))
    assert result.status == 'SUCCEEDED'
    assert result.structured_output == {'tasks': []}
    assert clients[0].options.output_format['schema'] == schema


@pytest.mark.parametrize(('messages', 'status', 'code'), [
    ([SystemMessage()], 'UNKNOWN', 'SDK_RESULT_MISSING'),
    ([ResultMessage(structured_output=None)], 'FAILED', 'SDK_STRUCTURED_OUTPUT_INVALID'),
    ([ResultMessage(is_error=True, subtype='error_max_turns')], 'FAILED', 'SDK_ERROR_MAX_TURNS'),
    ([RuntimeError('fixture secret')], 'FAILED', 'SDK_EXECUTION_FAILED'),
])
async def test_errors_preserve_workspace_transcript_without_legacy_fallback(tmp_path, messages, status, code):
    (tmp_path / 'source.py').write_text('candidate', encoding='utf-8')
    instance, clients = adapter(messages)
    result = await instance.start(request(tmp_path, transcript_path=tmp_path / 'transcript.jsonl'))
    assert result.status == status and result.error_code == code
    assert (tmp_path / 'source.py').read_text() == 'candidate'
    assert result.transcript_ref
    assert clients[0].disconnected
    assert 'fixture secret' not in result.model_dump_json()


async def test_resume_requires_registered_identity_and_never_changes_feedback(tmp_path):
    instance, clients = adapter([SystemMessage(), ResultMessage(terminal_reason='aborted_tools')])
    original = request(tmp_path)
    assert (await instance.start(original)).status == 'STOPPED'
    with pytest.raises(ValueError, match='新反馈'):
        await instance.resume(original.execution_id, 'forged-feedback')
    await instance.resume(original.execution_id)
    assert clients[1].options.resume == 'sdk-session'
    assert clients[1].prompts == ['继续当前已冻结任务，从会话中断位置推进；不得重复已完成的操作。']


async def test_resume_in_new_adapter_loads_persisted_execution(tmp_path):
    instance, clients = adapter([ResultMessage()])
    original = request(tmp_path, sdk_session_id='persisted-session')
    instance._execution_loader = lambda identity: original.model_dump(mode='json')
    result = await instance.resume(original.execution_id)
    assert result.status == 'SUCCEEDED'
    assert clients[0].options.resume == 'persisted-session'


async def test_unknown_execution_cannot_resume(tmp_path):
    instance, clients = adapter([SystemMessage()])
    original = request(tmp_path)
    await instance.start(original)
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        await instance.resume(original.execution_id)


async def test_interrupt_does_not_claim_process_tree_stopped(tmp_path):
    instance, clients = adapter([])
    original = request(tmp_path)
    instance._clients[original.execution_id] = FakeClient(None, [])
    from tracefix.agents.claude_sdk_adapter import ExecutionInspection
    instance._inspections[original.execution_id] = ExecutionInspection(
        execution_id=original.execution_id, status='RUNNING', active=True)
    receipt = await instance.interrupt(original.execution_id, 'stop-1')
    assert receipt.interrupt_sent and not receipt.writers_stopped
    assert receipt.status == 'INTERRUPTING'
    assert (await instance.interrupt('missing', 'stop-2')).status == 'UNKNOWN'


async def test_production_sdk_is_not_constructed_in_shared_host(tmp_path, monkeypatch):
    monkeypatch.delenv('TRACEFIX_SDK_WORKER', raising=False)
    result = await ClaudeSdkAdapter().start(request(tmp_path))
    assert result.status == 'FAILED'
    assert result.unresolved == ['RuntimeError']


async def test_worker_clears_unapproved_inherited_environment(tmp_path, monkeypatch):
    from tracefix.agents import claude_sdk_worker
    from tracefix.agents.claude_sdk_adapter import ExecutionResult

    inherited = {'TRACEFIX_SDK_WORKER': '1', 'ANTHROPIC_API_KEY': 'restricted-key',
        'CLAUDE_CONFIG_DIR': str(tmp_path / 'config'), 'PATH': 'worker-path',
        'HTTPS_PROXY': 'http://egress-proxy:3128', 'ANTHROPIC_BASE_URL': 'https://unapproved.test',
        'CLAUDE_EXTRA_ENV': 'unapproved-plugin', 'LEGACY_TOKEN': 'shared-service-secret'}
    monkeypatch.setattr(claude_sdk_worker.os, 'environ', inherited)
    observed = []

    class WorkerAdapter:
        async def start(self, execution, emit):
            observed.append(dict(claude_sdk_worker.os.environ))
            return ExecutionResult(execution_id=execution.execution_id, status='SUCCEEDED')

    monkeypatch.setattr(claude_sdk_worker, 'ClaudeSdkAdapter', WorkerAdapter)
    original = request(tmp_path / 'source')
    original.workspace.mkdir()
    request_file = tmp_path / 'request.json'
    request_file.write_text(original.model_dump_json(), encoding='utf-8')
    result_file = tmp_path / 'state/result.json'
    assert await claude_sdk_worker.run_worker(request_file, result_file) == 0
    assert observed == [{'TRACEFIX_SDK_WORKER': '1', 'ANTHROPIC_API_KEY': 'restricted-key',
        'CLAUDE_CONFIG_DIR': str(tmp_path / 'config'), 'PATH': 'worker-path',
        'HTTPS_PROXY': 'http://egress-proxy:3128'}]
    assert result_file.is_file()


async def test_unconnected_frozen_mcp_fails_fast_instead_of_succeeding(tmp_path):
    binding = {'records': {'type': 'http', 'url': 'https://api.anthropic.com/mcp'}}
    init = SystemMessage(data={'session_id': 'sdk-session',
        'mcp_servers': [{'name': 'records', 'status': 'failed'}]})
    instance, clients = adapter([init, ResultMessage()])
    result = await instance.start(request(tmp_path, mcp_servers=binding))
    assert result.status == 'FAILED' and result.error_code == 'SDK_MCP_UNAVAILABLE'
    assert result.unresolved == ['records'] and result.sdk_session_id == 'sdk-session'
    assert clients[0].disconnected
    connected = SystemMessage(data={'session_id': 'sdk-session',
        'mcp_servers': [{'name': 'records', 'status': 'connected'}]})
    instance, _clients = adapter([connected, ResultMessage()])
    assert (await instance.start(request(tmp_path, mcp_servers=binding))).status == 'SUCCEEDED'


@pytest.mark.parametrize(('status', 'code'), [('SUCCEEDED', 0), ('FAILED', 1), ('STOPPED', 3), ('UNKNOWN', 4)])
async def test_worker_exit_code_distinguishes_stopped_from_failed(tmp_path, monkeypatch, status, code):
    from tracefix.agents import claude_sdk_worker
    from tracefix.agents.claude_sdk_adapter import ExecutionResult

    monkeypatch.setattr(claude_sdk_worker.os, 'environ', {'TRACEFIX_SDK_WORKER': '1',
        'ANTHROPIC_API_KEY': 'restricted-key', 'CLAUDE_CONFIG_DIR': str(tmp_path / 'config')})

    class WorkerAdapter:
        async def start(self, execution, emit):
            return ExecutionResult(execution_id=execution.execution_id, status=status)

    monkeypatch.setattr(claude_sdk_worker, 'ClaudeSdkAdapter', WorkerAdapter)
    original = request(tmp_path / 'source')
    original.workspace.mkdir()
    request_file = tmp_path / 'request.json'
    request_file.write_text(original.model_dump_json(), encoding='utf-8')
    assert await claude_sdk_worker.run_worker(request_file, tmp_path / 'state/result.json') == code
