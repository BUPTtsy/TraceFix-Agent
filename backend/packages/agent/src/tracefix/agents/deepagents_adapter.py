"""DeepAgents 调查能力；宿主独占 RunState、workspace、artifact 和操作账本。

仅组合真实 TodoList、Summarization 和 PatchToolCalls middleware，不安装
Filesystem、Shell、SubAgent 或 Memory middleware。框架的内部规划和压缩缓冲
仅存活于单次调查，不创建独立 checkpoint、store 或持久化文件。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Callable, Iterator, Mapping
from fnmatch import fnmatchcase
from importlib.metadata import version
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

try:
    from deepagents.backends.protocol import FileDownloadResponse, WriteResult
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.summarization import SummarizationMiddleware
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain.agents.structured_output import ToolStrategy
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import get_buffer_string
    from langchain_core.tools import InjectedToolCallId, StructuredTool
    from langchain_openai import ChatOpenAI
except (ImportError, AttributeError) as error:
    raise RuntimeError('DeepAgents 调查依赖未安装或版本不兼容；请安装项目锁定依赖') from error

from tracefix.runtime.contracts import Contract
from tracefix.runtime.subtask_contracts import SubtaskResult
from tracefix.model.gateway import ModelError

DEEPAGENTS_VERSION = '0.4.12'


def _relative_path(path: str, *, pattern: bool = False) -> str:
    if (not isinstance(path, str) or not path or '\\' in path or ':' in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or any(part in {'', '.', '..'} for part in path.split('/'))
            or (not pattern and any(character in path for character in '*?[]'))):
        raise ValueError('仅接受规范的项目相对路径')
    return path


class DeepAgentsInvestigation(Contract):
    """宿主冻结的范围、来源和材料；模型不能修改授权。"""

    model_config = ConfigDict(extra='forbid', frozen=True, arbitrary_types_allowed=True)

    goal: str = Field(min_length=1)
    phase: Literal['DIAGNOSE', 'REVIEW'] = 'DIAGNOSE'
    allowed_files: tuple[str, ...]
    allowed_evidence_refs: tuple[str, ...]
    source_manifest: str = Field(min_length=1)
    worker_generation: int = Field(ge=0, strict=True)
    context: dict = Field(default_factory=dict)
    model_configuration: Any = Field(default=None, exclude=True, repr=False)

    @field_validator('allowed_files')
    @classmethod
    def validate_files(cls, paths):
        return tuple(dict.fromkeys(_relative_path(path) for path in paths))


class DeepAgentsError(ModelError):
    def __init__(self, message, *, category, status='FAILED', details=None):
        super().__init__(message, category=category, status=status, details=details)


class DeepAgentsBoundaryError(DeepAgentsError, PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message, category=code, details={'executed': False})
        self.code = code


class _ReadonlyTools(Mapping[str, Callable]):
    def __init__(self, request: DeepAgentsInvestigation, injected: Mapping[str, Callable]):
        self._request, self._injected = request, dict(injected)
        self._violation: DeepAgentsBoundaryError | None = None
        self._semaphore = asyncio.Semaphore(2)
        available = {'Read': self.read, 'Glob': self.glob, 'Grep': self.grep}
        self._tools = {name: available[name] for name in injected}

    def __iter__(self) -> Iterator[str]:
        return iter(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __getitem__(self, name: str) -> Callable:
        if name not in self._tools:
            self._deny('deepagents_tool_denied', '调查仅开放注入的 Read、Glob、Grep')
        return self._tools[name]

    def _deny(self, code: str, message: str):
        error = DeepAgentsBoundaryError(code, message)
        self._violation = self._violation or error
        raise error

    def check(self):
        if self._violation is not None:
            raise self._violation

    def _path(self, path: str) -> str:
        try:
            _relative_path(path)
        except ValueError:
            self._deny('deepagents_scope_denied', '调查文件路径不合法')
        if path not in self._request.allowed_files:
            self._deny('deepagents_scope_denied', '调查文件超出授权范围')
        return path

    async def _invoke(self, name: str, tool_call_id: str, **arguments):
        if name not in self._tools:
            self._deny('deepagents_tool_denied', '调查工具未获授权')
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            self._deny('deepagents_invalid_tool_id', '调查工具必须携带供应商真实 tool_call_id')
        async with self._semaphore:
            answer = self._injected[name](call_id=tool_call_id, **arguments)
            return await answer if inspect.isawaitable(answer) else answer

    async def read(self, path: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        answer = await self._invoke('Read', tool_call_id, path=self._path(path))
        if not isinstance(answer, str):
            self._deny('deepagents_invalid_result', 'Read 必须返回文本回执')
        return answer

    async def grep(self, pattern: str, path: str,
                   tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
        answer = await self._invoke('Grep', tool_call_id, pattern=pattern, path=self._path(path))
        if not isinstance(answer, str):
            self._deny('deepagents_invalid_result', 'Grep 必须返回文本回执')
        return answer

    async def glob(self, pattern: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> list[str]:
        try:
            _relative_path(pattern, pattern=True)
        except ValueError:
            self._deny('deepagents_scope_denied', 'Glob 模式不合法')
        matches = tuple(path for path in self._request.allowed_files if fnmatchcase(path, pattern))
        answer = await self._invoke('Glob', tool_call_id, pattern=pattern, allowed_files=matches)
        if not isinstance(answer, (list, tuple)) or any(not isinstance(path, str) for path in answer):
            self._deny('deepagents_invalid_result', 'Glob 必须返回文件路径列表')
        for path in answer:
            self._path(path)
            if path not in matches:
                self._deny('deepagents_scope_denied', 'Glob 返回文件超出检索范围')
        return list(answer)


class _ConversationBuffer:
    def __init__(self):
        self._history: dict[str, str] = {}

    async def adownload_files(self, paths):
        return [FileDownloadResponse(path=path, content=self._history[path].encode())
                if path in self._history else FileDownloadResponse(path=path, error='file_not_found')
                for path in paths]

    async def awrite(self, path, content):
        self._history[path] = content
        return WriteResult(path=path)

    async def aedit(self, path, old_string, new_string, replace_all=False):
        self._history[path] = self._history[path].replace(old_string, new_string,
                                                        -1 if replace_all else 1)
        return WriteResult(path=path)


class _StrictSummarization(SummarizationMiddleware):
    async def _acreate_summary(self, messages_to_summarize):
        trimmed = self._lc_helper._trim_messages_for_summary(messages_to_summarize)
        response = await self.model.ainvoke(self._lc_helper.summary_prompt.format(
            messages=get_buffer_string(trimmed)).rstrip(),
            config={'metadata': {'lc_source': 'summarization'}})
        if not response.text.strip():
            raise DeepAgentsError('上下文摘要为空', category='deepagents_summary_invalid')
        return response.text.strip()

    def _build_new_messages_with_path(self, summary, file_path):
        return super()._build_new_messages_with_path(summary, None)


class _ReadonlyBoundary(AgentMiddleware):
    def __init__(self, tools):
        self._tools = tools

    async def aafter_model(self, state, runtime):
        calls = getattr(state['messages'][-1], 'tool_calls', [])
        seen = set()
        for call in calls:
            name = call['name']
            if name not in {*self._tools, 'write_todos', 'SubtaskResult'}:
                self._tools._deny('deepagents_tool_denied', 'DeepAgents 试图调用未授权工具')
            if not call.get('id') or call['id'] in seen:
                self._tools._deny('deepagents_invalid_tool_id', '调查工具 id 缺失或重复')
            seen.add(call['id'])
            if name in {'Read', 'Grep'}:
                self._tools._path(call['args'].get('path'))
            if name == 'write_todos' and len(call['args'].get('todos', [])) > 12:
                self._tools._deny('deepagents_plan_limit', '调查规划最多包含 12 个步骤')

    async def awrap_tool_call(self, request, handler):
        if request.tool_call['name'] not in {*self._tools, 'write_todos', 'SubtaskResult'}:
            self._tools._deny('deepagents_tool_denied', 'DeepAgents 试图调用未授权工具')
        return await handler(request)


class _RequestAudit(BaseCallbackHandler):
    run_inline = True
    raise_error = True

    def __init__(self, sink: Callable | None, limit: int):
        self._sink, self._limit = sink, limit
        self._requests: dict[str, dict] = {}
        self._pending: set[str] = set()
        self.cancelled: asyncio.CancelledError | None = None
        self.usage = {'model_calls': 0, 'tokens': 0, 'cost_usd': 0.0}

    def _emit(self, event, payload):
        if self._sink:
            self._sink(event, payload)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if self.usage['model_calls'] >= self._limit:
            raise DeepAgentsError('DeepAgents 调查已达到真实请求上界', category='model_request_limit')
        identity = str(run_id)
        self._requests[identity] = {'tokens': 0, 'cost_usd': 0.0}
        self._pending.add(identity)
        self.usage['model_calls'] += 1
        self._emit('request', {'request_id': identity, 'model': serialized,
            'messages': [[message.model_dump(mode='json') for message in batch] for batch in messages]})

    def _usage(self, identity, message):
        raw = message.response_metadata.get('token_usage') or message.usage_metadata or {}
        self._record_usage(identity, raw)

    def _record_usage(self, identity, raw):
        tokens = raw.get('total_tokens', raw.get('input_tokens', 0) + raw.get('output_tokens', 0))
        cost = raw.get('cost_usd', 0) or 0
        if (type(tokens) is not int or tokens < 0 or not isinstance(cost, (int, float))
                or not math.isfinite(cost) or cost < 0):
            raise DeepAgentsError('供应商用量无效', category='model_usage_invalid',
                status='UNKNOWN_OPERATION', details={'billing_status': 'unknown', 'requires_manual_review': True})
        previous = self._requests[identity]
        delta = {'tokens': max(0, tokens - previous['tokens']),
                 'cost_usd': max(0, cost - previous['cost_usd'])}
        previous['tokens'], previous['cost_usd'] = max(tokens, previous['tokens']), max(cost, previous['cost_usd'])
        self.usage['tokens'] += delta['tokens']
        self.usage['cost_usd'] += delta['cost_usd']
        if raw:
            self._emit('usage', {'request_id': identity, 'usage': raw, 'delta': delta})

    def on_llm_new_token(self, token, *, run_id, chunk=None, **kwargs):
        if chunk is not None and hasattr(chunk, 'message'):
            self._usage(str(run_id), chunk.message)

    def on_llm_end(self, response, *, run_id, **kwargs):
        identity = str(run_id)
        for batch in response.generations:
            for generation in batch:
                if hasattr(generation, 'message'):
                    self._usage(identity, generation.message)
        self._emit('response', {'request_id': identity,
            'response': response.model_dump(mode='json')})
        self._pending.discard(identity)

    def on_llm_error(self, error, *, run_id, **kwargs):
        if isinstance(error, asyncio.CancelledError):
            self.cancelled = error
        body = getattr(error, 'body', None)
        if isinstance(body, dict) and isinstance(body.get('usage'), dict):
            self._record_usage(str(run_id), body['usage'])
        response = kwargs.get('response')
        if response is not None:
            for batch in response.generations:
                for generation in batch:
                    if hasattr(generation, 'message'):
                        self._usage(str(run_id), generation.message)
        self._emit('error', {'request_id': str(run_id), 'error': type(error).__name__,
            'category': getattr(error, 'category', 'model_transport'),
            'status': getattr(error, 'status', 'UNKNOWN_OPERATION'),
            'details': getattr(error, 'details', {'billing_status': 'unknown', 'request_status': 'sent'})})
        self._pending.discard(str(run_id))

    def cancel_pending(self, error):
        for identity in tuple(self._pending):
            self.on_llm_error(error, run_id=identity)


class _ProviderChatModel(ChatOpenAI):
    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw.get('choices', [])):
            generation.message.response_metadata['token_usage'] = raw.get('usage') or {}
            for key in ('reasoning_content', 'reasoning'):
                if key in choice.get('message', {}):
                    generation.message.additional_kwargs[key] = choice['message'][key]
        return result


def investigation_model(configuration) -> BaseChatModel:
    """按宿主模型配置构造 LangChain provider；不会调用 Gateway.generate。"""
    configuration = getattr(configuration, 'teacher', configuration)
    if isinstance(configuration, BaseChatModel):
        return configuration
    required = ('base_url', 'key', 'text_model', 'max_output_tokens', 'timeout', 'stream')
    if any(not hasattr(configuration, name) for name in required):
        raise DeepAgentsError('宿主调查模型缺少 provider 配置', category='model_configuration')
    extra = {}
    if getattr(configuration, 'thinking', None) is not None:
        extra['thinking'] = {'type': configuration.thinking}
    options = {'model': configuration.text_model, 'base_url': configuration.base_url,
        'api_key': configuration.key, 'max_tokens': configuration.max_output_tokens,
        'timeout': configuration.timeout, 'streaming': configuration.stream, 'stream_usage': True,
        'max_retries': 0, 'use_responses_api': False, 'extra_body': extra}
    if getattr(configuration, 'reasoning_effort', None) is not None:
        options['reasoning_effort'] = configuration.reasoning_effort
    return _ProviderChatModel(**options)


class DeepAgentsReadonlyAdapter:
    def __init__(self, *, readonly_tools: Mapping[str, Callable], model: BaseChatModel | None = None,
                 audit: Callable | None = None, on_result: Callable | None = None,
                 max_model_calls: int = 20, summarize_at: int = 24_000):
        if model is not None and not isinstance(model, BaseChatModel):
            raise DeepAgentsError('DeepAgents 必须接收 LangChain BaseChatModel', category='model_configuration')
        for name, tool in readonly_tools.items():
            if (name not in {'Read', 'Glob', 'Grep'} or not callable(tool)
                    or any(hasattr(tool, attribute) for attribute in ('invoke', 'ainvoke', 'get_tools'))):
                raise DeepAgentsBoundaryError('deepagents_tool_denied', '仅接受宿主注入的只读工具 callable')
        if max_model_calls < 1 or summarize_at < 1:
            raise ValueError('调查请求上界与压缩阈值必须为正数')
        self._readonly_tools, self._model = dict(readonly_tools), model
        self._audit, self._on_result = audit, on_result
        self._max_model_calls, self._summarize_at = max_model_calls, summarize_at

    async def run(self, request: DeepAgentsInvestigation) -> SubtaskResult:
        if version('deepagents') != DEEPAGENTS_VERSION:
            raise DeepAgentsError('DeepAgents 版本必须为 ' + DEEPAGENTS_VERSION, category='deepagents_version')
        request = DeepAgentsInvestigation.model_validate({
            **request.model_dump(), 'model_configuration': request.model_configuration})
        model = self._model if self._model is not None else investigation_model(request.model_configuration)
        tools = _ReadonlyTools(request, self._readonly_tools)
        audit = _RequestAudit(self._audit, self._max_model_calls)
        descriptions = {'Read': '读取一个授权文件并返回宿主回执。',
                        'Glob': '仅匹配授权文件清单。', 'Grep': '在一个授权文件中查找文本并返回宿主回执。'}
        agent = create_agent(model=model,
            tools=[StructuredTool.from_function(coroutine=tools[name], name=name,
                   description=descriptions[name]) for name in tools],
            system_prompt='只读调查并返回证据支持的中文 SubtaskResult；先规划有限调查步骤。'
                '只允许授权文件和证据。不得写入源码、执行 Shell、使用浏览器、数据库、MCP、'
                '委派、审批或发布。保留 worker_generation、source_manifest 与 content_version。',
            middleware=[TodoListMiddleware(), _StrictSummarization(model=model,
                backend=_ConversationBuffer(), trigger=('tokens', self._summarize_at),
                keep=('messages', 4), trim_tokens_to_summarize=12_000),
                PatchToolCallsMiddleware(), _ReadonlyBoundary(tools)],
            response_format=ToolStrategy(schema=SubtaskResult, handle_errors=False),
            checkpointer=None, store=None, interrupt_before=None, interrupt_after=None)
        try:
            answer = await agent.ainvoke({'messages': [{'role': 'user', 'content':
                json.dumps(request.model_dump(mode='json'), ensure_ascii=False)}]},
                {'callbacks': [audit], 'recursion_limit': self._max_model_calls * 3 + 10})
            tools.check()
            raw = answer.get('structured_response')
            if isinstance(raw, SubtaskResult):
                raw = raw.model_dump()
            result = SubtaskResult.model_validate(raw)
            self._validate_result(result, request)
            result = result.model_copy(deep=True, update={'worker_generation': request.worker_generation,
                'source_manifest': request.source_manifest, 'usage': dict(audit.usage)})
        except (DeepAgentsBoundaryError, ValidationError) as error:
            category = getattr(error, 'category', 'deepagents_invalid_result')
            result = SubtaskResult(status='rejected', conclusion='只读调查未被接受。',
                evidence_refs=[], files=[], suggested_experiments=[], unresolved=[str(error)[:1000]],
                gaps=[category], worker_generation=request.worker_generation,
                source_manifest=request.source_manifest, usage=dict(audit.usage),
                error={'category': category, 'status': 'FAILED', 'details': getattr(error, 'details', {})})
        except asyncio.CancelledError as error:
            audit.cancel_pending(error)
            raise
        except (TimeoutError, ModelError):
            raise
        except Exception as error:
            if audit.cancelled is not None:
                raise audit.cancelled from error
            if getattr(error, 'status', None) == 'UNKNOWN_OPERATION':
                raise
            known = getattr(error, 'status_code', None)
            raise DeepAgentsError(str(error), category='model_provider' if known else 'model_transport',
                status='FAILED' if known else 'UNKNOWN_OPERATION',
                details={'http_status': known, 'request_status': 'sent',
                         'billing_status': 'unknown', 'retryable': False,
                         'requires_manual_review': not bool(known)}) from error
        if self._on_result:
            delivery = self._on_result(result.model_copy(deep=True))
            if inspect.isawaitable(delivery):
                await delivery
        return result

    @staticmethod
    def _validate_result(result: SubtaskResult, request: DeepAgentsInvestigation):
        if ((result.worker_generation is not None and result.worker_generation != request.worker_generation)
                or (result.source_manifest and result.source_manifest != request.source_manifest)):
            raise DeepAgentsBoundaryError('deepagents_version_mismatch', '调查回显版本与输入快照不一致')
        paths = set(result.files)
        references = (set(result.evidence_refs) | set(result.support_refs)
                      | set(result.counterevidence_refs) | set(result.artifact_refs))
        for hypothesis in result.hypotheses:
            paths.update(candidate.path for candidate in hypothesis.candidate_paths)
            references.update(hypothesis.support_refs)
            references.update(hypothesis.counterevidence_refs)
        if not paths <= set(request.allowed_files) or not references <= set(request.allowed_evidence_refs):
            raise DeepAgentsBoundaryError('deepagents_scope_denied', '调查结果包含未授权文件或证据引用')
