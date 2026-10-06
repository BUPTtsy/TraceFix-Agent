"""Optional typed model calls; execution and persistence stay in injected ports."""
from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_core import to_json

from tracefix.model.contracts import ModelResult
from tracefix.runtime.contracts import digest

if TYPE_CHECKING:
    from tracefix.runtime.tools import ToolSpec


OutputT = TypeVar('OutputT', bound=BaseModel)


class PydanticAIAdapterError(RuntimeError):
    """Classified failure with audit context, never an instruction to retry tools."""

    def __init__(self, message, *, category, status='FAILED', details=None):
        super().__init__(message)
        self.category = category
        self.status = status
        self.details = {**(details or {}), 'category': category, 'status': status,
                        'retryable': False, 'will_retry': False}
        if status == 'UNKNOWN_OPERATION':
            self.details['requires_manual_review'] = True


@dataclass
class _RunTrace:
    messages: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    retry_context: list = field(default_factory=list)
    context: Any = None

    def observe(self, context):
        self.context = context
        self.messages = copy.deepcopy(getattr(context, 'messages', self.messages))
        snapshot = {'retry': getattr(context, 'retry', 0),
                    'retries': copy.deepcopy(getattr(context, 'retries', {})),
                    'tool_call_id': getattr(context, 'tool_call_id', None),
                    'messages': copy.deepcopy(self.messages)}
        if not self.retry_context or snapshot != self.retry_context[-1]:
            self.retry_context.append(snapshot)
        return snapshot

    def failure(self, category, message, cause=None, **details):
        if self.context is not None:
            self.observe(self.context)
        selected = next((error for error in _causes(cause)
                         if getattr(error, 'status', None) == 'UNKNOWN_OPERATION'
                         or (getattr(error, 'details', {}) or {}).get('status') == 'UNKNOWN_OPERATION'),
                        cause) if cause is not None else None
        inherited = dict(getattr(selected, 'details', {}) or {})
        status = getattr(selected, 'status', inherited.get('status', details.get('status', 'FAILED')))
        if category == 'stream_interrupted':
            status = 'UNKNOWN_OPERATION'
        return PydanticAIAdapterError(message, category=category, status=status,
            details={**inherited, 'message_history': copy.deepcopy(self.messages),
                     'tool_calls': copy.deepcopy(self.tool_calls),
                     'retry_context': copy.deepcopy(self.retry_context), **details})


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


def _causes(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(getattr(current, 'exceptions', ()))
        linked = current.__cause__ or current.__context__
        if linked is not None:
            pending.append(linked)


class PydanticAIAdapter:
    """Opt-in adapter for existing schemas and an externally governed tool port.

    ``generate`` returns the supplied schema type or raises
    ``PydanticAIAdapterError``. Tools use existing ``ToolSpec`` definitions and
    ``tool_executor(name, arguments, tool_call_id)``; only read/none tools with
    approval='never' are accepted. The port retains scope, evidence and policy
    enforcement. ``on_event(kind, payload)`` may be synchronous or asynchronous.
    PydanticAI handles bounded output correction within one run; tool failures
    and interrupted streams terminate the run. No adapter state is persisted.
    """

    def __init__(self, model: Any, *, output_retries: int = 1):
        if type(output_retries) is not int or output_retries < 0:
            raise PydanticAIAdapterError('output_retries 必须为非负整数',
                                        category='configuration')
        self.model = model
        self.output_retries = output_retries

    async def generate(self, schema: type[OutputT], context: Any, *,
                       tools: Sequence[ToolSpec] = (), tool_executor: Callable | None = None,
                       on_event: Callable | None = None, message_history: Sequence | None = None,
                       agent_instructions: str | None = None, image: bytes | None = None,
                       **runtime_options) -> Any:
        from tracefix.runtime.tools import ToolSpec

        trace = _RunTrace(messages=copy.deepcopy(list(message_history or ())))
        if schema is not str and (not isinstance(schema, type) or not issubclass(schema, BaseModel)):
            raise trace.failure('configuration', '输出 schema 必须为现有 Pydantic 模型')
        tools = tuple(tools)
        tool_registry = runtime_options.get('tool_registry')
        tool_pipeline = runtime_options.get('tool_pipeline')
        context_provider = runtime_options.get('context_provider')
        validate_output = runtime_options.get('validate_output')
        if tool_registry is not None:
            phase = context.get('phase') or 'DIAGNOSE'
            for spec in tool_registry.visible(phase):
                if spec not in tools:
                    tools += (spec,)
        if getattr(self.model, 'additional_tools', None):
            for spec in self.model.additional_tools:
                if spec not in tools:
                    tools += (spec,)
        if (getattr(self.model, 'tool_mode', None) == 'native'
                and getattr(schema, '__name__', None) in {'BrowserAction', 'Decision'}):
            for definition in getattr(self.model, '_native_tools', lambda _schema: [])(schema):
                function = definition['function']
                if any(spec.wire_name == function['name'] for spec in tools):
                    continue
                tools += (ToolSpec(
                    'browser.' + function['name'][len('Browser'):].lower(),
                    function['description'], function['parameters'],
                    side_effect='external', idempotency_key=digest,
                    phases=frozenset({context.get('phase', 'EXPLORE')})),)
        names = set()
        for spec in tools:
            if (not isinstance(spec, ToolSpec) or not spec.enabled
                    or (spec.approval != 'never' and tool_pipeline is None)):
                raise trace.failure('configuration', '适配器拒绝未授权或禁用工具')
            if spec.wire_name in names:
                raise trace.failure('configuration', '工具模型名称重复：' + spec.wire_name)
            names.add(spec.wire_name)
        if on_event is not None and not callable(on_event):
            raise trace.failure('configuration', 'on_event 必须为 callable')
        client = None
        try:
            library = importlib.import_module('pydantic_ai')
            tool_library = importlib.import_module('pydantic_ai.tools')
            exceptions = importlib.import_module('pydantic_ai.exceptions')
        except ImportError as error:
            raise trace.failure('unavailable',
                'PydanticAI 适配器不可用：请安装可选依赖 pydantic-ai 并确认其依赖完整',
                error, dependency='pydantic-ai') from error

        async def emit(kind, payload):
            if on_event is not None:
                try:
                    await _resolve(on_event(kind, copy.deepcopy(payload)))
                except Exception as error:
                    raise trace.failure('event_callback', '适配器事件回调失败', error,
                                        event_kind=kind) from error

        async def emit_failure(record, failure):
            record['error'] = failure.details
            try:
                await emit('tool.error', record)
            except PydanticAIAdapterError as callback_error:
                failure.details['callback_error'] = str(callback_error)

        if context_provider is not None:
            context = await _resolve(context_provider())

        def provider_model():
            nonlocal client
            if not all(hasattr(self.model, field) for field in ('base_url', 'text_model', 'key')):
                return self.model
            if not getattr(self.model, 'key', ''):
                raise PydanticAIAdapterError('未配置 TRACEFIX_API_KEY', category='configuration',
                                             details={'requires_manual_review': False})
            try:
                from pydantic_ai.models.openai import OpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider
                from tracefix.model.protocol import create_http_client, RequestBoundary
                boundary = RequestBoundary(self.model, schema, context, tool_registry or
                    type('Registry', (), {'specs': tools, 'contains': lambda *_: False})(),
                    context.get('phase') or 'DIAGNOSE', {
                        **runtime_options, 'agent_instructions': agent_instructions,
                        'on_event': on_event})
                client = create_http_client(boundary)
                provider = OpenAIProvider(base_url=self.model.base_url,
                    api_key=self.model.key, http_client=client)
                settings = {'max_tokens': self.model.max_output_tokens}
                if self.model.thinking is not None:
                    settings['extra_body'] = {'thinking': {'type': self.model.thinking}}
                return OpenAIChatModel(
                    self.model.vision_model if image and self.model.vision_model else self.model.text_model,
                    provider=provider, settings=settings)
            except PydanticAIAdapterError:
                raise
            except Exception as error:
                raise PydanticAIAdapterError('PydanticAI provider 初始化失败',
                    category='configuration', details={'error': str(error)}) from error

        def history_messages():
            if not trace.messages:
                return None
            if not isinstance(trace.messages[0], dict):
                return copy.deepcopy(trace.messages)
            try:
                message_library = importlib.import_module('pydantic_ai.messages')
                converted = []
                for item in trace.messages:
                    role = item.get('role')
                    if role == 'system':
                        converted.append(message_library.ModelRequest(parts=[
                            message_library.SystemPromptPart(content=item.get('content', ''))]))
                    elif role == 'user':
                        converted.append(message_library.ModelRequest(parts=[
                            message_library.UserPromptPart(content=item.get('content', ''))]))
                    elif role == 'assistant':
                        parts = []
                        if item.get('reasoning_content') or item.get('reasoning'):
                            parts.append(message_library.ThinkingPart(
                                content=item.get('reasoning_content') or item.get('reasoning')))
                        if item.get('content'):
                            parts.append(message_library.TextPart(content=item['content']))
                        for call in item.get('tool_calls', ()):
                            function = call.get('function', {})
                            parts.append(message_library.ToolCallPart(
                                tool_name=function.get('name', ''), args=function.get('arguments', '{}'),
                                tool_call_id=call.get('id', '')))
                        if parts:
                            converted.append(message_library.ModelResponse(parts=parts))
                    elif role == 'tool':
                        converted.append(message_library.ModelRequest(parts=[
                            message_library.ToolReturnPart(tool_name=item.get('name', ''),
                                content=item.get('content', ''), tool_call_id=item.get('tool_call_id', ''))]))
                if not converted and callable(self.model):
                    return copy.deepcopy(trace.messages)
                return converted
            except Exception as error:
                if isinstance(self.model, Callable) or callable(self.model):
                    return copy.deepcopy(trace.messages)
                raise PydanticAIAdapterError('工具历史无法转换为 PydanticAI 消息',
                    category='tool_protocol', details={'message_history': trace.messages}) from error

        def bind(spec):
            async def execute(run_context, **arguments):
                snapshot = trace.observe(run_context)
                call_id = snapshot['tool_call_id']
                if not isinstance(call_id, str) or not call_id.strip():
                    raise trace.failure('tool_protocol', '工具调用缺少 tool_call_id',
                                        tool_name=spec.name)
                try:
                    if context_provider is not None:
                        await _resolve(context_provider())
                    spec.validate(arguments)
                except (ValueError, TypeError) as error:
                    raise trace.failure('tool_protocol', '只读工具参数校验失败', error,
                        tool_call_id=call_id, tool_name=spec.name, arguments=arguments) from error
                record = {'tool_call_id': call_id, 'tool_name': spec.name,
                          'arguments': copy.deepcopy(arguments), **snapshot}
                trace.tool_calls.append(record)
                await emit('tool.started', record)
                try:
                    if tool_pipeline is not None and tool_pipeline.registry.contains(spec.name, tool_pipeline.phase):
                        result = await tool_pipeline.execute(spec.name, arguments, call_id)
                    elif callable(tool_executor):
                        result = await _resolve(tool_executor(spec.name, arguments, call_id))
                    else:
                        raise PydanticAIAdapterError('模型工具未绑定执行端口', category='tool_execution',
                                                     details={'tool_call_id': call_id, 'tool_name': spec.name})
                except Exception as error:
                    failure = trace.failure('tool_execution', '只读工具执行失败', error,
                        tool_call_id=call_id, tool_name=spec.name, arguments=arguments)
                    await emit_failure(record, failure)
                    raise failure from error
                record['result'] = result
                receipt = result.model_dump(by_alias=True) if isinstance(result, BaseModel) else result
                if isinstance(receipt, dict) and (receipt.get('isError') or receipt.get('is_error')):
                    error_details = receipt.get('error') or {}
                    failure = trace.failure('tool_execution', '只读工具返回失败回执',
                        tool_call_id=call_id, tool_name=spec.name, result=result, **{
                            key: value for key, value in error_details.items()
                            if key not in {'tool_call_id', 'tool_name', 'result',
                                           'category', 'message', 'cause'}})
                    await emit_failure(record, failure)
                    raise failure
                callback = runtime_options.get('on_tool_result')
                if callback is not None:
                    await _resolve(callback(None, {
                        'message': {'role': 'tool', 'tool_call_id': call_id,
                                    'name': spec.wire_name,
                                    'content': (result.to_content() if hasattr(result, 'to_content')
                                                else json.dumps(receipt, ensure_ascii=False, default=str))},
                        'tool_call_id': call_id, 'receipt': receipt, 'reused': False}))
                await emit('tool.completed', record)
                if isinstance(result, BaseModel):
                    return result.model_dump(mode='json', by_alias=True)
                return result.to_content() if hasattr(result, 'to_content') else result

            tool = tool_library.Tool.from_schema(execute, name=spec.wire_name,
                description=spec.description, json_schema=spec.parameters,
                takes_ctx=True)
            tool.max_retries = 0
            if hasattr(tool, 'sequential'):
                tool.sequential = not (spec.parallel_safe and spec.side_effect in {'none', 'read'})
            return tool

        submission_spec = next((spec for spec in tools if spec.submission
                                and spec.submission_schema is not None), None)
        output_type = schema
        if submission_spec is not None:
            try:
                output_module = importlib.import_module('pydantic_ai')
                output_type = [schema, output_module.ToolOutput(
                    submission_spec.submission_schema, name=submission_spec.wire_name,
                    max_retries=0)]
            except AttributeError:
                output_type = schema

        async def stream_events(run_context, events):
            trace.observe(run_context)
            try:
                async for event in events:
                    snapshot = trace.observe(run_context)
                    part = getattr(event, 'part', None)
                    payload = {**snapshot, 'event': event,
                               'event_kind': getattr(event, 'event_kind', type(event).__name__),
                               'tool_call_id': getattr(part, 'tool_call_id', snapshot['tool_call_id'])}
                    if getattr(part, 'part_kind', None) == 'retry-prompt':
                        trace.retry_context.append(payload)
                        await emit('adapter.retry', payload)
                    await emit('model.stream', payload)
            except (PydanticAIAdapterError, exceptions.UnexpectedModelBehavior, ValidationError):
                raise
            except (Exception, asyncio.CancelledError) as error:
                if any(isinstance(cause, (PydanticAIAdapterError,
                                         exceptions.UnexpectedModelBehavior, ValidationError))
                       for cause in _causes(error)):
                    raise
                raise trace.failure('stream_interrupted', '模型流式响应中断', error) from error

        try:
            prompt = to_json(copy.deepcopy(context)).decode('utf-8')
            if image:
                try:
                    message_library = importlib.import_module('pydantic_ai.messages')
                    prompt = [message_library.BinaryContent(data=image, media_type='image/png'),
                              prompt]
                except (ImportError, TypeError, ValueError) as error:
                    raise trace.failure('configuration', '图片输入无法转换为 PydanticAI 内容', error) from error
            agent = library.Agent(provider_model(), output_type=output_type,
                instructions=agent_instructions, tools=[bind(spec) for spec in tools],
                retries=self.output_retries)
            if validate_output is not None:
                model_retry = getattr(exceptions, 'ModelRetry', RuntimeError)
                @agent.output_validator
                async def host_output_validator(run_context, output):
                    try:
                        checked = output if isinstance(output, schema) else schema.model_validate(output)
                        result = validate_output(checked)
                        if inspect.isawaitable(result):
                            await result
                        return checked
                    except Exception as error:
                        raise model_retry(str(error)) from error
            await emit('adapter.started', {'schema': getattr(schema, '__name__', str(schema)),
                       'message_history': trace.messages, 'output_retries': self.output_retries})
            result = await agent.run(prompt, message_history=history_messages(),
                                     event_stream_handler=stream_events)
            trace.messages = copy.deepcopy(result.all_messages())
            trace.context = None
            output = result.output if schema is str else schema.model_validate(result.output)
            await emit('adapter.completed', {'output': output, 'message_history': trace.messages,
                       'tool_calls': trace.tool_calls, 'retry_context': trace.retry_context})
            usage = getattr(result, 'usage', {})
            if hasattr(usage, 'model_dump'):
                usage = usage.model_dump(mode='json')
            elif hasattr(usage, 'total_tokens'):
                usage = {'total_tokens': usage.total_tokens,
                         'request_tokens': getattr(usage, 'request_tokens', 0),
                         'response_tokens': getattr(usage, 'response_tokens', 0)}
            elif callable(usage):
                usage = usage()
            elif not isinstance(usage, dict):
                usage = {'total_tokens': getattr(usage, 'total_tokens', 0)}
            model_revision = getattr(getattr(result, 'response', None), 'model_name',
                                     getattr(self.model, 'text_model', type(self.model).__name__))
            finish_reason = getattr(getattr(result, 'response', None), 'finish_reason', None) or 'stop'
            return ModelResult(output, usage, model_revision, finish_reason)
        except (Exception, asyncio.CancelledError) as error:
            causes = list(_causes(error))
            adapted = next((cause for cause in causes
                            if isinstance(cause, PydanticAIAdapterError)), None)
            if adapted is not None:
                raise adapted
            host_error = next((cause for cause in causes
                               if getattr(cause, 'category', None) in
                               {'stream_interrupted', 'tool_execution', 'tool_protocol'}), None)
            if host_error is not None:
                raise trace.failure(host_error.category, str(host_error), host_error) from error
            if trace.context is not None:
                trace.observe(trace.context)
            unexpected = next((cause for cause in causes
                               if isinstance(cause, exceptions.UnexpectedModelBehavior)), None)
            if any(isinstance(cause, ValidationError) for cause in causes):
                category, message = 'output_validation', '模型输出不符合现有 schema'
            elif isinstance(error, (asyncio.CancelledError, httpx.ReadError,
                                    httpx.ReadTimeout, httpx.RemoteProtocolError)):
                category, message = 'stream_interrupted', '模型调用被中断'
            elif unexpected is not None and (
                    str(unexpected).startswith('Exceeded maximum output retries')
                    or str(unexpected).startswith('Exceeded maximum retries')
                    and 'for output validation' in str(unexpected)):
                category, message = 'output_validation', '模型输出校验重试已耗尽'
            else:
                category, message = 'model', 'PydanticAI 模型调用失败'
            raise trace.failure(category, message, error) from error
        finally:
            if client is not None:
                await client.aclose()
