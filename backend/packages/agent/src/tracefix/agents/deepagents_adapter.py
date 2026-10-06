"""只读调查组件；宿主 Worker 负责版本、权限、证据、锁、取消和超时。

注入 callable 必须由宿主保证只读、快照一致性及真实路径（包括 symlink）隔离。
Read 接收 path，Grep 接收 pattern/path，Glob 接收 pattern/allowed_files。
本模块不持有 RunState、workspace、artifact、事件存储或父 Run。
"""
from __future__ import annotations

import importlib
import inspect
import json
from collections.abc import Callable, Iterator, Mapping
from fnmatch import fnmatchcase
from typing import Any, Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from tracefix.runtime.contracts import Contract
from tracefix.runtime.worker import SubtaskResult


def _relative_path(path: str, *, pattern: bool = False) -> str:
    if (not isinstance(path, str) or not path or '\\' in path or ':' in path
            or any(ord(character) < 32 for character in path)
            or any(part in {'', '.', '..'} for part in path.split('/'))
            or (not pattern and any(character in path for character in '*?[]'))):
        raise ValueError('仅接受规范的项目相对路径')
    return path


class DeepAgentsInvestigation(Contract):
    """宿主提供的冻结调查范围；不包含任何运行时状态或存储句柄。"""

    model_config = ConfigDict(extra='forbid', frozen=True)

    goal: str = Field(min_length=1)
    phase: Literal['DIAGNOSE', 'EXPLORE'] = 'DIAGNOSE'
    allowed_files: tuple[str, ...]
    allowed_evidence_refs: tuple[str, ...]
    source_manifest: str = Field(min_length=1)
    worker_generation: int = Field(ge=0, strict=True)

    @field_validator('allowed_files')
    @classmethod
    def validate_files(cls, paths):
        return tuple(dict.fromkeys(_relative_path(path) for path in paths))


class DeepAgentsBoundaryError(PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _DeepAgentsUnavailable(RuntimeError):
    pass


class _ReadonlyTools(Mapping[str, Callable]):
    def __init__(self, request: DeepAgentsInvestigation, injected: Mapping[str, Callable]):
        self._request = request
        self._injected = dict(injected)
        self._violation: DeepAgentsBoundaryError | None = None
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

    async def _invoke(self, name: str, **arguments):
        if name not in self._tools:
            self._deny('deepagents_tool_denied', '调查工具未被注入')
        answer = self._injected[name](**arguments)
        return await answer if inspect.isawaitable(answer) else answer

    async def read(self, path: str) -> str:
        answer = await self._invoke('Read', path=self._path(path))
        if not isinstance(answer, str):
            self._deny('deepagents_invalid_result', 'Read 必须返回文本')
        return answer

    async def grep(self, pattern: str, path: str) -> str:
        answer = await self._invoke('Grep', pattern=pattern, path=self._path(path))
        if not isinstance(answer, str):
            self._deny('deepagents_invalid_result', 'Grep 必须返回文本')
        return answer

    async def glob(self, pattern: str) -> list[str]:
        try:
            _relative_path(pattern, pattern=True)
        except ValueError:
            self._deny('deepagents_scope_denied', 'Glob 模式不合法')
        matches = tuple(path for path in self._request.allowed_files if fnmatchcase(path, pattern))
        if not matches:
            return []
        answer = await self._invoke('Glob', pattern=pattern, allowed_files=matches)
        if not isinstance(answer, (list, tuple)) or any(not isinstance(path, str) for path in answer):
            self._deny('deepagents_invalid_result', 'Glob 必须返回文件路径列表')
        for path in answer:
            self._path(path)
            if path not in matches:
                self._deny('deepagents_scope_denied', 'Glob 返回文件超出检索范围')
        return list(answer)


class DeepAgentsReadonlyAdapter:
    """单次调查返回 SubtaskResult，并向注入的回调交付其独立副本。

    runner 为可信注入 callable，接收 request/tools/result_type 关键字参数。
    默认 runner 仅组合 DeepAgents 的无工具 PatchToolCallsMiddleware；默认
    create_deep_agent 会附加写入及委派工具，因此不用于这个适配器。
    """

    def __init__(self, *, readonly_tools: Mapping[str, Callable],
                 on_result: Callable[[SubtaskResult], Any] | None = None,
                 runner: Callable | None = None, model: Any = None):
        for name, tool in readonly_tools.items():
            if (name not in {'Read', 'Glob', 'Grep'} or not callable(tool)
                    or any(hasattr(tool, attribute) for attribute in ('invoke', 'ainvoke', 'get_tools'))):
                raise DeepAgentsBoundaryError('deepagents_tool_denied',
                    '仅接受只读 Read、Glob、Grep callable；拒绝写入、shell、Git、浏览器、数据库和动态 MCP 工具')
        if runner is not None and not callable(runner):
            raise TypeError('runner 必须为 callable')
        if on_result is not None and not callable(on_result):
            raise TypeError('on_result 必须为 callable')
        self._readonly_tools = dict(readonly_tools)
        self._on_result = on_result
        self._runner = runner
        self._model = model

    async def run(self, request: DeepAgentsInvestigation) -> SubtaskResult:
        request = DeepAgentsInvestigation.model_validate(request.model_dump())
        tools = _ReadonlyTools(request, self._readonly_tools)
        try:
            runner = self._runner or self._run_deepagents
            answer = runner(request=request, tools=tools, result_type=SubtaskResult)
            if inspect.isawaitable(answer):
                answer = await answer
            tools.check()
            if isinstance(answer, Mapping) and 'structured_response' in answer:
                answer = answer['structured_response']
            if isinstance(answer, SubtaskResult):
                answer = answer.model_dump()
            result = SubtaskResult.model_validate(answer)
            self._validate_result(result, request)
            result = result.model_copy(deep=True, update={
                'worker_generation': request.worker_generation,
                'source_manifest': request.source_manifest})
        except _DeepAgentsUnavailable as error:
            result = self._failure(request, 'failed', 'deepagents_unavailable', str(error))
        except DeepAgentsBoundaryError as error:
            result = self._failure(request, 'rejected', error.code, str(error))
        except ValidationError:
            result = self._failure(request, 'rejected', 'deepagents_invalid_result', '调查结果不符合 SubtaskResult')
        except TimeoutError:
            raise
        except Exception as error:
            result = self._failure(request, 'failed', 'deepagents_runner_failed', type(error).__name__)
        if self._on_result is not None:
            delivery = self._on_result(result.model_copy(deep=True))
            if inspect.isawaitable(delivery):
                await delivery
        return result

    @staticmethod
    def _failure(request, status, code, message):
        return SubtaskResult(status=status, conclusion='只读调查未被接受。',
            evidence_refs=[], files=[], suggested_experiments=[], unresolved=[f'{code}: {message}'],
            gaps=[code], worker_generation=request.worker_generation, source_manifest=request.source_manifest)

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

    async def _run_deepagents(self, *, request, tools, result_type):
        """延迟导入可选组件；仅返回结构化调查，不启用独立持久化或委派。"""
        try:
            middleware_type = importlib.import_module(
                'deepagents.middleware.patch_tool_calls').PatchToolCallsMiddleware
            create_agent = importlib.import_module('langchain.agents').create_agent
            strategy_type = importlib.import_module('langchain.agents.structured_output').ToolStrategy
            structured_tool = importlib.import_module('langchain_core.tools').StructuredTool
        except (ImportError, AttributeError) as error:
            raise _DeepAgentsUnavailable('DeepAgents 可选只读组件未安装或 API 不兼容') from error
        if self._model is None:
            raise _DeepAgentsUnavailable('宿主未提供调查模型')
        descriptions = {'Read': '读取一个授权文件。', 'Glob': '仅匹配授权文件清单。',
                        'Grep': '在一个授权文件中查找文本。'}
        try:
            agent = create_agent(model=self._model,
                tools=[structured_tool.from_function(coroutine=tools[name], name=name,
                    description=descriptions[name]) for name in tools],
                system_prompt='仅进行只读代码调查、上下文摘要及假设整理，以 SubtaskResult 返回。'
                    '只能使用授权文件和证据；不得写入、执行命令、访问浏览器、数据库或 MCP、'
                    '委派子任务、审批或发布。保留 worker_generation 和 source_manifest。',
                middleware=[middleware_type()],
                response_format=strategy_type(schema=result_type, handle_errors=False),
                checkpointer=None, store=None, interrupt_before=None, interrupt_after=None)
        except (TypeError, AttributeError) as error:
            raise _DeepAgentsUnavailable('DeepAgents 可选只读组件 API 不兼容') from error
        return await agent.ainvoke({'messages': [{'role': 'user',
            'content': json.dumps(request.model_dump(mode='json'), ensure_ascii=False)}]})
