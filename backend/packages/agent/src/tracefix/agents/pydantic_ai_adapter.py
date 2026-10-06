"""Optional typed model calls; execution and persistence stay in injected ports."""
from __future__ import annotations

import asyncio
import copy
import importlib
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from pydantic_core import to_json

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
                       agent_instructions: str | None = None) -> OutputT:
        from tracefix.runtime.tools import ToolSpec

        trace = _RunTrace(messages=copy.deepcopy(list(message_history or ())))
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise trace.failure('configuration', '输出 schema 必须为现有 Pydantic 模型')
        tools = tuple(tools)
        names = set()
        for spec in tools:
            if (not isinstance(spec, ToolSpec) or spec.side_effect not in {'none', 'read'}
                    or spec.approval != 'never' or spec.submission or not spec.enabled):
                raise trace.failure('configuration', '适配器仅接受无需审批的只读工具')
            if spec.wire_name in names:
                raise trace.failure('configuration', '工具模型名称重复：' + spec.wire_name)
            names.add(spec.wire_name)
        if tools and not callable(tool_executor):
            raise trace.failure('configuration', '只读工具必须注入 tool_executor port')
        if on_event is not None and not callable(on_event):
            raise trace.failure('configuration', 'on_event 必须为 callable')
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

        def bind(spec):
            async def execute(run_context, **arguments):
                snapshot = trace.observe(run_context)
                call_id = snapshot['tool_call_id']
                if not isinstance(call_id, str) or not call_id.strip():
                    raise trace.failure('tool_protocol', '工具调用缺少 tool_call_id',
                                        tool_name=spec.name)
                try:
                    spec.validate(arguments)
                except (ValueError, TypeError) as error:
                    raise trace.failure('tool_protocol', '只读工具参数校验失败', error,
                        tool_call_id=call_id, tool_name=spec.name, arguments=arguments) from error
                record = {'tool_call_id': call_id, 'tool_name': spec.name,
                          'arguments': copy.deepcopy(arguments), **snapshot}
                trace.tool_calls.append(record)
                await emit('tool.started', record)
                try:
                    result = await _resolve(tool_executor(spec.name, arguments, call_id))
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
                await emit('tool.completed', record)
                return result

            tool = tool_library.Tool.from_schema(execute, name=spec.wire_name,
                description=spec.description, json_schema=spec.parameters,
                takes_ctx=True)
            tool.max_retries = 0
            if hasattr(tool, 'sequential'):
                tool.sequential = True
            return tool

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
            agent = library.Agent(self.model, output_type=schema,
                instructions=agent_instructions, tools=[bind(spec) for spec in tools],
                retries=self.output_retries)
            await emit('adapter.started', {'schema': schema.__name__,
                       'message_history': trace.messages, 'output_retries': self.output_retries})
            result = await agent.run(prompt, message_history=copy.deepcopy(trace.messages),
                                     event_stream_handler=stream_events)
            trace.messages = copy.deepcopy(result.all_messages())
            trace.context = None
            output = schema.model_validate(result.output)
            await emit('adapter.completed', {'output': output, 'message_history': trace.messages,
                       'tool_calls': trace.tool_calls, 'retry_context': trace.retry_context})
            return output
        except (Exception, asyncio.CancelledError) as error:
            causes = list(_causes(error))
            adapted = next((cause for cause in causes
                            if isinstance(cause, PydanticAIAdapterError)), None)
            if adapted is not None:
                raise adapted
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
