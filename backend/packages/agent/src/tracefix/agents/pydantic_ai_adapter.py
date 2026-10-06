"""PydanticAI model execution with persistence and tools governed by host ports."""
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

from tracefix.model.contracts import ModelError, ModelResult
from tracefix.runtime.contracts import digest

if TYPE_CHECKING:
    from tracefix.runtime.tools import ToolSpec


OutputT = TypeVar('OutputT', bound=BaseModel)


class PydanticAIAdapterError(ModelError):
    """Classified failure with audit context, never an instruction to retry tools."""

    def __init__(self, message, *, category, status='FAILED', details=None):
        super().__init__(message, category=category, status=status, details=details)
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
        from tracefix.model.history import ModelProtocol
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
            details=ModelProtocol.audit_value({**inherited,
                     'message_history': ModelProtocol.message_history(self.messages),
                     'tool_calls': self.tool_calls,
                     'retry_context': self.retry_context, **details}))


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
    """Model execution for existing schemas and an externally governed tool port.

    ``generate`` returns ``ModelResult`` containing the supplied schema or raises
    ``PydanticAIAdapterError``. Tools use existing ``ToolSpec`` definitions and
    ``tool_executor(name, arguments, tool_call_id)`` or ``ToolPipeline``. The
    port retains scope, evidence, approval and policy enforcement. Read-only
    tools may run in parallel; effectful tools are sequential and never retried
    by the adapter. ``on_event(kind, payload)`` may be synchronous or
    asynchronous. PydanticAI handles bounded output correction within one run;
    tool failures and interrupted streams terminate the run. No adapter state
    is persisted.
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
        from tracefix.runtime.tools import ToolProtocolError, ToolRegistry, ToolRejected, ToolSpec

        trace = _RunTrace(messages=copy.deepcopy(list(message_history or ())))
        if schema is not str and (not isinstance(schema, type) or not issubclass(schema, BaseModel)):
            raise trace.failure('configuration', '输出 schema 必须为现有 Pydantic 模型')
        tools = tuple(tools)
        tool_pipeline = runtime_options.get('tool_pipeline')
        tool_registry = runtime_options.get('tool_registry') or getattr(tool_pipeline, 'registry', None)
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
                    phases=frozenset({context.get('phase') or 'DIAGNOSE'})),)
        names = set()
        for spec in tools:
            if (not isinstance(spec, ToolSpec) or not spec.enabled
                    or (spec.approval != 'never' and tool_pipeline is None)):
                raise trace.failure('configuration', '适配器拒绝未授权或禁用工具')
            if spec.wire_name in names:
                raise trace.failure('configuration', '工具模型名称重复：' + spec.wire_name)
            names.add(spec.wire_name)
        tool_registry = ToolRegistry(tools)
        phase = context.get('phase') or getattr(tool_pipeline, 'phase', 'DIAGNOSE')
        concurrency = runtime_options.get('max_concurrency', 4)
        if type(concurrency) is not int or concurrency < 1:
            raise trace.failure('configuration', '工具并发上界必须为正整数')
        read_slots = asyncio.Semaphore(concurrency)
        if on_event is not None and not callable(on_event):
            raise trace.failure('configuration', 'on_event 必须为 callable')
        client = None
        boundary = None
        completed_results = {}
        try:
            library = importlib.import_module('pydantic_ai')
            tool_library = importlib.import_module('pydantic_ai.tools')
            exceptions = importlib.import_module('pydantic_ai.exceptions')
        except ImportError as error:
            raise trace.failure('unavailable',
                'PydanticAI 执行不可用：请安装 pydantic-ai 并确认其依赖完整',
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
            nonlocal client, boundary
            if not all(hasattr(self.model, field) for field in ('base_url', 'text_model', 'key')):
                return self.model
            if not getattr(self.model, 'key', ''):
                raise PydanticAIAdapterError('未配置 TRACEFIX_API_KEY', category='configuration',
                                             details={'requires_manual_review': False})
            try:
                from pydantic_ai.models.openai import OpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider
                from openai import AsyncOpenAI
                from tracefix.model.protocol import create_http_client, RequestBoundary
                boundary = RequestBoundary(self.model, schema, context, tool_registry,
                    phase, {
                        **runtime_options, 'agent_instructions': agent_instructions,
                        'on_event': on_event, 'initial_history': copy.deepcopy(trace.messages),
                        'framework_output_tool_names': framework_output_tool_names})
                client = create_http_client(boundary)
                provider = OpenAIProvider(openai_client=AsyncOpenAI(base_url=self.model.base_url,
                    api_key=self.model.key, http_client=client, max_retries=0))
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
                        content = item.get('content', '')
                        if isinstance(content, list):
                            converted_content = []
                            for part in content:
                                if part.get('type') == 'text' and isinstance(part.get('text'), str):
                                    converted_content.append(part['text'])
                                elif part.get('type') == 'image_url':
                                    converted_content.append(message_library.ImageUrl(
                                        url=part['image_url']['url']))
                                else:
                                    raise ValueError('历史用户消息包含不支持的内容')
                            content = converted_content
                        converted.append(message_library.ModelRequest(parts=[
                            message_library.UserPromptPart(content=content)]))
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
                    raise trace.failure('tool_protocol', '工具参数校验失败', error,
                        tool_call_id=call_id, tool_name=spec.name, arguments=arguments) from error
                record = {'tool_call_id': call_id, 'tool_name': spec.name,
                          'arguments': copy.deepcopy(arguments), 'reused': False, **snapshot}
                trace.tool_calls.append(record)
                await emit('tool.started', record)
                identity = (spec.name, json.dumps(arguments, sort_keys=True, default=str))
                cached = completed_results.get(call_id)
                if cached is not None:
                    if cached['identity'] != identity:
                        failure = trace.failure('tool_protocol', 'tool_call_id 不可复用于不同参数',
                            tool_call_id=call_id, tool_name=spec.name, arguments=arguments)
                        await emit_failure(record, failure)
                        raise failure
                    record['reused'] = True
                    result = copy.deepcopy(cached['result'])
                else:
                    try:
                        async def dispatch():
                            if tool_pipeline is not None and tool_pipeline.registry.contains(spec.name, tool_pipeline.phase):
                                return await tool_pipeline.execute(spec.name, arguments, call_id)
                            if callable(tool_executor):
                                return await _resolve(tool_executor(spec.name, arguments, call_id))
                            raise PydanticAIAdapterError('模型工具未绑定执行端口', category='tool_execution',
                                details={'tool_call_id': call_id, 'tool_name': spec.name})
                        if spec.parallel_safe and spec.side_effect in {'none', 'read'}:
                            async with read_slots:
                                result = await dispatch()
                        else:
                            result = await dispatch()
                    except Exception as error:
                        failure = trace.failure('tool_execution', '工具执行失败', error,
                            tool_call_id=call_id, tool_name=spec.name, arguments=arguments,
                            status=('UNKNOWN_OPERATION' if spec.side_effect in {'write', 'external'}
                                and not isinstance(error, (ToolRejected, ToolProtocolError, PermissionError))
                                and not hasattr(error, 'status') else getattr(error, 'status', 'FAILED')))
                        await emit_failure(record, failure)
                        raise failure from error
                record['result'] = result
                receipt = result.model_dump(mode='json', by_alias=True) if isinstance(result, BaseModel) else result
                if isinstance(receipt, str):
                    try:
                        receipt = json.loads(receipt)
                    except ValueError:
                        pass
                failed = isinstance(receipt, dict) and (receipt.get('isError') or receipt.get('is_error'))
                failure = None
                if failed:
                    error_details = receipt.get('error') or {}
                    rejected_submission = (spec.submission and receipt.get('executed') is False
                                           and error_details.get('status', 'FAILED') == 'FAILED')
                    failure = trace.failure('output_validation' if rejected_submission else 'tool_execution',
                        '提交被拒绝，需要补充诊断上下文' if rejected_submission else '工具返回失败回执',
                        tool_call_id=call_id, tool_name=spec.name, result=result, **{
                            key: value for key, value in error_details.items()
                            if key not in {'tool_call_id', 'tool_name', 'result',
                                           'category', 'message', 'cause'}})
                    if rejected_submission:
                        failure.details.update(submission_rejected=True, requires_manual_review=False)
                        failure.details.pop('message_history', None)
                        failure.details.pop('tool_calls', None)
                        failure.details['retry_context'] = [{key: value for key, value in item.items()
                            if key != 'messages'} for item in trace.retry_context]
                if not failed:
                    completed_results[call_id] = {'identity': identity, 'result': copy.deepcopy(result)}
                if boundary is not None:
                    boundary.completed[call_id] = (identity, result)
                    from tracefix.model.history import ModelProtocol
                    boundary.tool_records.append(ModelProtocol.audit_value(record))
                callback = runtime_options.get('on_tool_result')
                if callback is not None:
                    try:
                        metadata = await _resolve(callback(boundary.exchange if boundary else None, {
                            'message': {'role': 'tool', 'tool_call_id': call_id,
                                        'name': spec.wire_name,
                                        'content': (result.to_content() if hasattr(result, 'to_content')
                                                    else json.dumps(receipt, ensure_ascii=False, default=str))},
                            'tool_call_id': call_id, 'receipt': receipt,
                            'reused': record['reused'],
                            'logical_exchange_id': boundary.logical_id if boundary else None,
                            'tool_round': boundary.tool_round if boundary else 0}))
                        if boundary is not None and isinstance(metadata, dict):
                            boundary.result_refs[call_id] = metadata
                    except Exception as error:
                        if failure is not None:
                            failure.details['callback_error'] = str(error)
                            await emit_failure(record, failure)
                            raise failure from error
                        raise trace.failure('event_callback', '工具结果回调失败', error,
                            event_kind='tool.completed', tool_call_id=call_id,
                            tool_name=spec.name, result=copy.deepcopy(result)) from error
                if failed:
                    await emit_failure(record, failure)
                    raise failure
                await emit('tool.completed', record)
                images = receipt.get('images') if isinstance(receipt, dict) else None
                if images:
                    messages = importlib.import_module('pydantic_ai.messages')
                    return library.ToolReturn(return_value={key: value for key, value in receipt.items()
                                              if key != 'images'}, content=[messages.ImageUrl(
                        url=item['image_url']['url']) for item in images])
                if isinstance(result, BaseModel):
                    return result.model_dump(mode='json', by_alias=True)
                return result.to_content() if hasattr(result, 'to_content') else result

            sequential = not (spec.parallel_safe and spec.side_effect in {'none', 'read'})
            try:
                tool = tool_library.Tool.from_schema(execute, name=spec.wire_name,
                    description=spec.description, json_schema=spec.parameters,
                    takes_ctx=True, sequential=sequential)
            except TypeError:
                tool = tool_library.Tool.from_schema(execute, name=spec.wire_name,
                    description=spec.description, json_schema=spec.parameters,
                    takes_ctx=True)
            tool.max_retries = 0
            tool.sequential = sequential
            return tool

        submission_spec = next((spec for spec in tools if spec.submission
                                and spec.submission_schema is not None), None)
        output_type = schema
        framework_output_tool_names = set()
        if submission_spec is not None:
            output_type = [library.ToolOutput(schema, name='final_result'), library.ToolOutput(
                submission_spec.submission_schema, name=submission_spec.wire_name,
                max_retries=0)]
            framework_output_tool_names.update({'final_result', submission_spec.wire_name})
        elif schema is not str and hasattr(library, 'PromptedOutput'):
            output_type = library.PromptedOutput(schema, template=False)
        elif schema is not str:
            framework_output_tool_names.add('final_result')

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
            except (ModelError, exceptions.UnexpectedModelBehavior, ValidationError):
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if any(isinstance(cause, (ModelError,
                                         exceptions.UnexpectedModelBehavior, ValidationError))
                       for cause in _causes(error)):
                    raise
                raise trace.failure('stream_interrupted', '模型流式响应中断', error) from error

        try:
            if trace.messages and isinstance(trace.messages[0], dict) and all(
                    isinstance(message, dict) and 'role' in message for message in trace.messages):
                from tracefix.model.history import ModelProtocol
                try:
                    trace.messages = ModelProtocol._wire_history(trace.messages, tool_registry, phase)
                    restored = ModelProtocol._completed_history(trace.messages, tool_registry, phase)
                    for call_id, (identity, content) in restored.items():
                        spec = tool_registry.get_wire(identity[0], phase)
                        try:
                            receipt = json.loads(content)
                        except ValueError:
                            receipt = content
                        completed_results[call_id] = {
                            'identity': (spec.name, identity[1]), 'result': content}
                    if tool_pipeline is not None:
                        for call_id, entry in completed_results.items():
                            try:
                                receipt = json.loads(entry['result'])
                            except ValueError:
                                continue
                            if isinstance(receipt, dict) and 'call_id' in receipt:
                                tool_pipeline.completed_calls[call_id] = (entry['identity'], receipt)
                except (ValueError, TypeError) as error:
                    raise trace.failure('tool_protocol', '工具历史校验失败：' + str(error), error) from error
            prompt = to_json(copy.deepcopy(context)).decode('utf-8')
            if all(hasattr(self.model, name) for name in ('base_url', 'text_model', 'key')) and schema is not str:
                from tracefix.model.prompts import serialize_request
                prompt = serialize_request(schema, context)
            if trace.messages:
                prompt = None
            if runtime_options.get('preserve_resumed_request') or schema is str and trace.messages:
                prompt = None
            if image:
                try:
                    message_library = importlib.import_module('pydantic_ai.messages')
                    if prompt is not None:
                        prompt = [prompt, message_library.BinaryContent(data=image, media_type='image/png')]
                except (ImportError, TypeError, ValueError) as error:
                    raise trace.failure('configuration', '图片输入无法转换为 PydanticAI 内容', error) from error
            bound_tools = {spec.name: bind(spec) for spec in tools}
            agent = library.Agent(provider_model(), output_type=output_type,
                instructions=agent_instructions, tools=[tool for name, tool in bound_tools.items()
                    if submission_spec is None or name != submission_spec.name],
                retries=self.output_retries)
            if validate_output is not None or submission_spec is not None:
                model_retry = getattr(exceptions, 'ModelRetry', RuntimeError)
                @agent.output_validator
                async def host_output_validator(run_context, output):
                    submitting = (submission_spec is not None
                        and getattr(run_context, 'tool_name', None) == submission_spec.wire_name)
                    if submitting:
                        await bound_tools[submission_spec.name].function(run_context,
                            **output.model_dump(mode='json', by_alias=True))
                        if tool_pipeline is not None and tool_pipeline.submission_value is not None:
                            output = tool_pipeline.submission_value
                    try:
                        checked = output if isinstance(output, schema) else schema.model_validate(output)
                        if validate_output is not None:
                            await _resolve(validate_output(checked))
                        return checked
                    except Exception as error:
                        if isinstance(error, ModelError):
                            raise
                        if submitting:
                            raise trace.failure('output_validation', '提交完成后输出校验失败，需人工复核',
                                error, status='UNKNOWN_OPERATION') from error
                        raise model_retry(str(error)) from error
            await emit('adapter.started', {'schema': getattr(schema, '__name__', str(schema)),
                       'message_history': trace.messages, 'output_retries': self.output_retries})
            run_options = {'message_history': history_messages(),
                'event_stream_handler': stream_events if getattr(self.model, 'stream', True) else None}
            if runtime_options.get('model_settings') is not None:
                run_options['model_settings'] = runtime_options['model_settings']
            result = await agent.run(prompt, **run_options)
            trace.messages = copy.deepcopy(result.all_messages())
            trace.context = None
            output = result.output if schema is str else schema.model_validate(result.output)
            await emit('adapter.completed', {'output': output, 'message_history': trace.messages,
                       'tool_calls': trace.tool_calls, 'retry_context': trace.retry_context})
            usage = getattr(result, 'usage', {})
            if hasattr(usage, 'model_dump'):
                usage = usage.model_dump(mode='json')
            elif hasattr(usage, 'total_tokens'):
                usage = {key: copy.deepcopy(value) for key, value in vars(usage).items()
                         if not key.startswith('_')} if hasattr(usage, '__dict__') else {}
                usage.setdefault('total_tokens', getattr(result.usage, 'total_tokens', 0))
                usage.setdefault('request_tokens', usage.get('input_tokens', 0))
                usage.setdefault('response_tokens', usage.get('output_tokens', 0))
            elif callable(usage):
                usage = usage()
            elif not isinstance(usage, dict):
                usage = {'total_tokens': getattr(usage, 'total_tokens', 0)}
            if boundary is not None:
                usage = copy.deepcopy(boundary.usage) or usage
            model_revision = getattr(getattr(result, 'response', None), 'model_name',
                                     getattr(self.model, 'text_model', type(self.model).__name__))
            finish_reason = getattr(getattr(result, 'response', None), 'finish_reason', None) or 'stop'
            if boundary is not None:
                model_revision = boundary.last_body.get('model') or model_revision
                finish_reason = boundary.last_body['choices'][0].get('finish_reason') or finish_reason
            return ModelResult(output, usage, model_revision, finish_reason)
        except asyncio.CancelledError:
            if boundary is not None and boundary.request_count:
                await boundary.fail('cancelled', '模型调用已取消，已发送请求的结果未知',
                                    status='UNKNOWN_OPERATION', request_status='unknown')
            raise
        except Exception as error:
            causes = list(_causes(error))
            adapted = next((cause for cause in causes
                            if isinstance(cause, PydanticAIAdapterError)), None)
            if adapted is not None:
                if boundary is not None:
                    adapted.details.update(usage=copy.deepcopy(boundary.usage),
                        raw_usage=copy.deepcopy(boundary.raw_usage),
                        logical_exchange_id=boundary.logical_id, attempt=boundary.request_count,
                        tool_round=boundary.tool_round)
                    await boundary.report(adapted)
                raise adapted
            host_error = next((cause for cause in causes if isinstance(cause, ModelError)
                or all(hasattr(cause, field) for field in ('category', 'status', 'details'))), None)
            if host_error is not None:
                failure = trace.failure(host_error.category, str(host_error), host_error)
                if boundary is not None:
                    await boundary.report(host_error)
                raise failure from error
            if trace.context is not None:
                trace.observe(trace.context)
            unexpected = next((cause for cause in causes
                               if isinstance(cause, exceptions.UnexpectedModelBehavior)), None)
            if any(isinstance(cause, ValidationError) for cause in causes):
                category, message = 'output_validation', '模型输出不符合现有 schema'
            elif isinstance(error, (httpx.ReadError,
                                    httpx.ReadTimeout, httpx.RemoteProtocolError)):
                category, message = 'stream_interrupted', '模型调用被中断'
            elif unexpected is not None and (
                    str(unexpected).startswith('Exceeded maximum output retries')
                    or str(unexpected).startswith('Exceeded maximum retries')
                    and 'for output validation' in str(unexpected)):
                category, message = 'output_validation', '模型输出校验重试已耗尽'
            else:
                category, message = 'model', 'PydanticAI 模型调用失败'
            if boundary is not None:
                audited = await boundary.fail(category, message, error)
                raise trace.failure(category, message, audited) from error
            raise trace.failure(category, message, error) from error
        finally:
            if client is not None:
                await client.aclose()
