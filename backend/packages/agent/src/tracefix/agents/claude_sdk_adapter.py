import asyncio
import dataclasses
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tracefix.agents.claude_sdk_config import CLI_VERSION, SDK_VERSION, ClaudeSdkConfig
from tracefix.runtime.contracts import Contract


NATIVE_TOOLS = frozenset({'Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash', 'Agent'})
READONLY_TOOLS = ('Read', 'Glob', 'Grep')
OUTPUT_SCHEMA = {'type': 'object', 'properties': {
    'summary': {'type': 'string'}, 'unresolved': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['summary', 'unresolved'], 'additionalProperties': False}


class SdkExecutionRequest(Contract):
    execution_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    input_hash: str = Field(min_length=1)
    role: Literal['initializer', 'coder']
    prompt: str = Field(min_length=1)
    workspace: Path
    config: ClaudeSdkConfig
    tools: list[str]
    system_prompt: str = ''
    sdk_session_id: str | None = None
    transcript_path: Path | None = None
    feedback_ref: str | None = None
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=lambda: OUTPUT_SCHEMA.copy())

    @model_validator(mode='after')
    def validate_request(self):
        if self.config.backend != 'claude_sdk':
            raise ValueError('SDK 请求必须冻结 claude_sdk 后端')
        if not self.tools or len(set(self.tools)) != len(self.tools) or not set(self.tools) <= NATIVE_TOOLS:
            raise ValueError('SDK 原生工具必须使用明确且受限的工具集合')
        if self.role == 'initializer' and not set(self.tools) <= {*READONLY_TOOLS, 'Agent'}:
            raise ValueError('initializer 不得直接修改源码或执行 Bash')
        if self.output_schema.get('type') != 'object':
            raise ValueError('SDK 结构化结果必须为 object schema')
        import jsonschema
        jsonschema.Draft202012Validator.check_schema(self.output_schema)
        for name, server in self.mcp_servers.items():
            if not name or set(server) != {'type', 'url'} or server.get('type') not in {'http', 'sse'}:
                raise ValueError('仅支持宿主冻结的受控远端 MCP 绑定')
            parsed = urlsplit(server['url'])
            origin = f'{parsed.scheme}://{parsed.netloc}'
            if parsed.username or parsed.password or origin not in self.config.egress_allowlist:
                raise ValueError('MCP 绑定超出已冻结出口')
        return self


class ExecutionResult(Contract):
    execution_id: str
    status: Literal['SUCCEEDED', 'FAILED', 'STOPPED', 'UNKNOWN']
    sdk_session_id: str | None = None
    summary: str = ''
    unresolved: list[str] = Field(default_factory=list)
    observed_usage: dict[str, Any] = Field(default_factory=dict)
    transcript_ref: str | None = None
    workspace_snapshot_ref: str | None = None
    error_code: str | None = None
    structured_output: dict[str, Any] | None = None


class StopReceipt(Contract):
    execution_id: str
    stop_request_id: str
    interrupt_sent: bool
    writers_stopped: bool = False
    status: Literal['INTERRUPTING', 'UNKNOWN']


class ExecutionInspection(Contract):
    execution_id: str
    status: str
    sdk_session_id: str | None = None
    active: bool = False


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _message_value(message):
    if dataclasses.is_dataclass(message):
        return dataclasses.asdict(message)
    if hasattr(message, 'model_dump'):
        return message.model_dump(mode='json')
    return {name: value for name, value in vars(message).items() if not name.startswith('_')}


def _redact(value):
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    secret = os.environ.get('ANTHROPIC_API_KEY', '')
    if secret:
        encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
    return json.loads(encoded)


class ClaudeSdkAdapter:
    def __init__(self, *, sdk=None, client_factory=None, execution_loader=None, feedback_loader=None):
        self._sdk = sdk
        self._client_factory = client_factory
        self._injected = sdk is not None or client_factory is not None
        self._execution_loader = execution_loader
        self._feedback_loader = feedback_loader
        self._requests = {}
        self._clients = {}
        self._inspections = {}
        self._results = {}

    def _bindings(self):
        if self._sdk is not None:
            return self._sdk
        if os.environ.get('TRACEFIX_SDK_WORKER') != '1':
            raise RuntimeError('真实 SDK 仅允许在专用 worker 进程中构造')
        import claude_agent_sdk
        from claude_agent_sdk._cli_version import __cli_version__
        if (importlib.metadata.version('claude-agent-sdk') != SDK_VERSION or __cli_version__ != CLI_VERSION):
            raise RuntimeError('SDK 或内置 CLI 版本与冻结配置不一致')
        self._sdk = claude_agent_sdk
        return self._sdk

    def _options(self, request):
        sdk = self._bindings()
        tools = list(request.tools)
        mcp_tools = [f'mcp__{name}__*' for name in request.mcp_servers]
        return sdk.ClaudeAgentOptions(model=request.config.model_id, cwd=str(request.workspace),
            tools=tools, allowed_tools=tools + mcp_tools,
            disallowed_tools=['AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode', 'Skill'],
            permission_mode='dontAsk', setting_sources=[], strict_mcp_config=True,
            mcp_servers=request.mcp_servers,
            agents={'readonly': sdk.AgentDefinition(description='只读调查和审查',
                prompt='仅检查已提供源码并返回证据。不得修改文件、调用子 Agent 或执行命令。',
                tools=list(READONLY_TOOLS), disallowedTools=['Bash', 'Write', 'Edit', 'Agent', 'Skill'],
                model=request.config.model_id, mcpServers=[])},
            system_prompt={'type': 'preset', 'preset': 'claude_code', 'append': request.system_prompt},
            output_format={'type': 'json_schema', 'schema': request.output_schema},
            enable_file_checkpointing=True, resume=request.sdk_session_id,
            env={'CLAUDE_CONFIG_DIR': os.environ.get('CLAUDE_CONFIG_DIR', '/state/claude')})

    async def _emit(self, emit, event, payload):
        if emit:
            await _await(emit(event, payload))

    async def start(self, request, emit=None):
        request = SdkExecutionRequest.model_validate(request)
        previous = self._requests.get(request.execution_id)
        if previous is not None and previous.model_dump(mode='json') != request.model_dump(mode='json'):
            raise ValueError('同 execution 不得改变冻结请求')
        if request.execution_id in self._results:
            return self._results[request.execution_id]
        if request.execution_id in self._clients:
            raise RuntimeError('同 execution 已有活动 SDK 客户端')
        if not request.workspace.is_dir():
            raise ValueError('SDK 工作区不存在，拒绝启动')
        self._requests[request.execution_id] = request
        self._inspections[request.execution_id] = ExecutionInspection(
            execution_id=request.execution_id, status='STARTING', sdk_session_id=request.sdk_session_id)
        transcript = request.transcript_path or request.workspace.parent / 'transcript.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        result = ExecutionResult(execution_id=request.execution_id, status='FAILED',
            sdk_session_id=request.sdk_session_id, transcript_ref=str(transcript))
        client = None
        try:
            options = self._options(request)
            sdk = self._bindings()
            factory = self._client_factory or sdk.ClaudeSDKClient
            client = factory(options=options)
            self._clients[request.execution_id] = client
            await client.connect()
            self._inspections[request.execution_id].status = 'RUNNING'
            self._inspections[request.execution_id].active = True
            await self._emit(emit, 'sdk.execution.started', {'execution_id': request.execution_id})
            await client.query(request.prompt)

            async def consume():
                terminal = None
                async for message in client.receive_response():
                    payload = _redact(_message_value(message))
                    envelope = {'type': type(message).__name__, 'payload': payload}
                    with transcript.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(envelope, ensure_ascii=False, default=str) + '\n')
                        stream.flush()
                    if type(message).__name__ == 'SystemMessage' and getattr(message, 'subtype', '') == 'init':
                        result.sdk_session_id = message.data.get('session_id')
                        self._inspections[request.execution_id].sdk_session_id = result.sdk_session_id
                        await self._emit(emit, 'sdk.session.bound', {'execution_id': request.execution_id,
                            'sdk_session_id': result.sdk_session_id})
                    await self._emit(emit, 'sdk.message', envelope)
                    if type(message).__name__ == 'ResultMessage':
                        terminal = message
                return terminal

            timeout = request.config.session_timeout_seconds
            terminal = await asyncio.wait_for(consume(), timeout) if timeout else await consume()
            if terminal is None:
                result.error_code = 'SDK_RESULT_MISSING'
                result.status = 'UNKNOWN'
            else:
                result.sdk_session_id = terminal.session_id or result.sdk_session_id
                result.observed_usage = {'usage': terminal.usage or {},
                    'total_cost_usd': terminal.total_cost_usd, 'num_turns': terminal.num_turns}
                reason = getattr(terminal, 'terminal_reason', None)
                if reason in {'aborted_streaming', 'aborted_tools'}:
                    result.status = 'STOPPED'
                    result.error_code = 'SDK_INTERRUPTED'
                elif terminal.is_error or terminal.subtype != 'success':
                    result.error_code = 'SDK_' + terminal.subtype.upper()
                    result.unresolved = _redact([str(item) for item in (getattr(terminal, 'errors', None) or [])])
                else:
                    import jsonschema
                    try:
                        jsonschema.validate(terminal.structured_output, request.output_schema)
                    except jsonschema.ValidationError:
                        result.error_code = 'SDK_STRUCTURED_OUTPUT_INVALID'
                    else:
                        result.status = 'SUCCEEDED'
                        result.structured_output = _redact(terminal.structured_output)
                        result.summary = result.structured_output.get('summary', '')
                        result.unresolved = result.structured_output.get('unresolved', [])
        except asyncio.CancelledError:
            if client is not None:
                await client.interrupt()
            result.status, result.error_code = 'UNKNOWN', 'SDK_CANCELLED_WRITERS_UNCONFIRMED'
            raise
        except (TimeoutError, asyncio.TimeoutError):
            if client is not None:
                await client.interrupt()
            result.status, result.error_code = 'UNKNOWN', 'SDK_TIMEOUT_WRITERS_UNCONFIRMED'
        except Exception as error:
            result.error_code = {'CLINotFoundError': 'SDK_CLI_MISSING', 'CLIConnectionError': 'SDK_CONNECTION',
                'ProcessError': 'SDK_PROCESS', 'CLIJSONDecodeError': 'SDK_MESSAGE_INVALID'}.get(
                    type(error).__name__, 'SDK_EXECUTION_FAILED')
            result.unresolved = [type(error).__name__]
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    result.status, result.error_code = 'UNKNOWN', 'SDK_DISCONNECT_WRITERS_UNCONFIRMED'
            self._clients.pop(request.execution_id, None)
            self._inspections[request.execution_id] = ExecutionInspection(execution_id=request.execution_id,
                status=result.status, sdk_session_id=result.sdk_session_id)
        self._results[request.execution_id] = result
        return result

    async def resume(self, execution_id, feedback_ref=None, emit=None):
        request = self._requests.get(execution_id)
        if request is None and self._execution_loader:
            request = SdkExecutionRequest.model_validate(await _await(self._execution_loader(execution_id)))
        if request is None or request.execution_id != execution_id:
            raise ValueError('恢复必须从宿主持久化记录加载原 execution')
        if feedback_ref != request.feedback_ref:
            raise ValueError('恢复不得向旧 execution 注入新反馈')
        result = self._results.get(execution_id)
        if result and result.status == 'SUCCEEDED':
            return result
        if result and result.status == 'UNKNOWN':
            raise RuntimeError('执行者状态 UNKNOWN，须由宿主先核对')
        session_id = result.sdk_session_id if result else request.sdk_session_id
        if not session_id:
            raise ValueError('SDK session 缺失，须由宿主登记显式重建尝试')
        if feedback_ref and self._feedback_loader is None:
            raise ValueError('自动反馈必须通过宿主工件加载器校验')
        if feedback_ref:
            feedback = await _await(self._feedback_loader(feedback_ref))
            if not isinstance(feedback, str) or not feedback.strip():
                raise ValueError('宿主反馈工件缺失或内容无效')
        self._results.pop(execution_id, None)
        resumed = request.model_copy(update={'sdk_session_id': session_id,
            'prompt': '继续当前已冻结任务，从会话中断位置推进；不得重复已完成的操作。'})
        self._requests.pop(execution_id, None)
        return await self.start(resumed, emit)

    async def interrupt(self, execution_id, stop_request_id):
        client = self._clients.get(execution_id)
        if client is None:
            return StopReceipt(execution_id=execution_id, stop_request_id=stop_request_id,
                interrupt_sent=False, status='UNKNOWN')
        await client.interrupt()
        self._inspections[execution_id].status = 'INTERRUPTING'
        return StopReceipt(execution_id=execution_id, stop_request_id=stop_request_id,
            interrupt_sent=True, status='INTERRUPTING')

    async def inspect(self, execution_id):
        inspection = self._inspections.get(execution_id)
        if inspection:
            return inspection.model_copy()
        return ExecutionInspection(execution_id=execution_id, status='UNKNOWN')
