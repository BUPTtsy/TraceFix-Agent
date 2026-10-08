"""Agent 工具契约、阶段注册、完整批次校验与可审计执行。"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Generic, Literal, Protocol, TypeVar

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from tracefix.runtime.contracts import Contract, Phase, digest
from tracefix.storage.artifacts import sanitize


class ToolProtocolError(ValueError):
    pass


class ToolRejected(ValueError):
    """执行器能够确定副作用没有发生时返回的可恢复错误。"""


class ToolOutputError(ValueError):
    """工具处理器返回值不符合注册的输出契约。"""


InputT = TypeVar('InputT', bound=BaseModel)
OutputT = TypeVar('OutputT')
InputContraT = TypeVar('InputContraT', bound=BaseModel, contravariant=True)
OutputCoT = TypeVar('OutputCoT', covariant=True)


class ToolHandler(Protocol[InputContraT, OutputCoT]):
    def __call__(self, arguments: InputContraT, call_id: str
                 ) -> OutputCoT | ToolResult | Awaitable[OutputCoT | ToolResult]: ...


class ToolOperationUnknown(RuntimeError):
    # 已可能发生副作用的失败不能自动重试，需由人工检查实际执行结果。
    status = 'UNKNOWN_OPERATION'
    category = 'tool_execution'

    def __init__(self, message, *, call_id, name, operation_id=None):
        super().__init__(message)
        self.details = {'status': self.status, 'category': self.category,
                        'tool_call_id': call_id, 'tool_name': name,
                        'requires_manual_review': True, 'retryable': False}
        if operation_id:
            self.details['operation_id'] = operation_id


def model_tool_name(name):
    parts = re.split(r'[._-]+', name)
    value = ''.join(part[:1].upper() + part[1:] for part in parts if part)
    return value if len(value) <= 64 else value[:48] + digest(name)[:16]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel] | dict
    output_limit_tokens: int = 4000
    side_effect: Literal['none', 'read', 'write', 'external'] = 'read'
    phases: frozenset[Phase] = field(default_factory=lambda: frozenset(Phase))
    approval: Literal['never', 'policy', 'always'] = 'never'
    timeout_s: float = 45
    idempotency_key: Callable[[Any], str] | None = None
    parallel_safe: bool = False
    category: str = 'general'
    submission: bool = False
    submission_schema: type[BaseModel] | None = None
    output_model: type[BaseModel] | dict | None = None
    aliases: tuple[str, ...] = ()
    search_hint: str = ''
    enabled: bool = True

    def __post_init__(self):
        # 保留旧版第三个位置参数作为并行标记的调用兼容性。
        if type(self.output_limit_tokens) is bool:
            object.__setattr__(self, 'parallel_safe', self.output_limit_tokens)
            object.__setattr__(self, 'output_limit_tokens', 4000)
        if (not isinstance(self.name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', self.name)
                or not isinstance(self.description, str) or not self.description.strip()):
            raise ValueError('工具名称或说明无效')
        restricted_name = self.name.casefold()
        if (restricted_name in {'git.push', 'git_push', 'shell', 'exec', 'create_pr', 'github.create_pr'}
                or restricted_name.startswith(('shell.', 'shell_', 'exec.', 'git.push.', 'github.create_pr.'))):
            raise ValueError('禁止向模型注册发布或任意命令工具')
        if isinstance(self.input_model, dict):
            jsonschema.Draft202012Validator.check_schema(self.input_model)
            if (self.input_model.get('type') != 'object'
                    or self.input_model.get('additionalProperties') is not False):
                raise ValueError('工具输入 schema 必须为禁止额外字段的对象')
            object.__setattr__(self, 'input_model', copy.deepcopy(self.input_model))
        elif (not isinstance(self.input_model, type) or not issubclass(self.input_model, BaseModel)
              or self.input_model.model_config.get('extra') != 'forbid'):
            raise ValueError('工具输入模型必须禁止额外字段')
        if type(self.output_limit_tokens) is not int or self.output_limit_tokens < 1:
            raise ValueError('工具结果 token 上限必须为正整数')
        if self.side_effect not in {'none', 'read', 'write', 'external'}:
            raise ValueError('未知的工具副作用分类')
        phases = frozenset(Phase(phase) for phase in self.phases)
        if not phases:
            raise ValueError('工具必须声明至少一个阶段')
        object.__setattr__(self, 'phases', phases)
        if self.approval not in {'never', 'policy', 'always'}:
            raise ValueError('未知的工具审批要求')
        if (isinstance(self.timeout_s, bool) or not isinstance(self.timeout_s, (int, float))
                or not math.isfinite(self.timeout_s) or self.timeout_s <= 0):
            raise ValueError('工具超时必须为有限正数')
        if type(self.parallel_safe) is not bool or type(self.submission) is not bool:
            raise ValueError('工具并行和提交标记必须为布尔值')
        if self.side_effect in {'write', 'external'}:
            # 副作用操作必须声明幂等键并串行执行，防止重试或并发重复写入。
            if not callable(self.idempotency_key):
                raise ValueError('write/external 工具必须声明幂等键')
            if self.parallel_safe:
                raise ValueError('write/external 工具不能并行执行')
        if self.submission and (self.side_effect != 'write' or self.parallel_safe):
            raise ValueError('提交工具必须为串行本地写入')
        if self.submission_schema is not None and (not self.submission or
                not isinstance(self.submission_schema, type) or
                not issubclass(self.submission_schema, BaseModel)):
            raise ValueError('提交输出模型无效')
        if self.output_model is not None:
            if isinstance(self.output_model, dict):
                jsonschema.Draft202012Validator.check_schema(self.output_model)
                if (self.output_model.get('type') != 'object'
                        or self.output_model.get('additionalProperties') is not False):
                    raise ValueError('工具输出 schema 必须为禁止额外字段的对象')
                object.__setattr__(self, 'output_model', copy.deepcopy(self.output_model))
            elif (not isinstance(self.output_model, type)
                  or not issubclass(self.output_model, BaseModel)
                  or self.output_model.model_config.get('extra') != 'forbid'):
                raise ValueError('工具输出模型必须禁止额外字段')
        if (not isinstance(self.aliases, tuple)
                or any(not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', alias)
                       for alias in self.aliases)
                or len(set(self.aliases)) != len(self.aliases)):
            raise ValueError('工具显式别名无效')
        if not isinstance(self.search_hint, str) or len(self.search_hint) > 200:
            raise ValueError('工具搜索提示无效')
        if type(self.enabled) is not bool:
            raise ValueError('工具启用标记必须为布尔值')

    @property
    def parameters(self):
        return (copy.deepcopy(self.input_model) if isinstance(self.input_model, dict)
                else self.input_model.model_json_schema())

    def validate_output(self, value):
        if self.output_model is None:
            return value
        try:
            if isinstance(self.output_model, dict):
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode='json')
                jsonschema.validate(value, self.output_model)
                return value
            return self.output_model.model_validate(value, strict=True)
        except (ValueError, TypeError, jsonschema.ValidationError) as error:
            raise ToolOutputError('工具输出校验失败：' + sanitize(str(error))[:2000]) from error

    @property
    def parallel(self):
        return self.parallel_safe

    @property
    def wire_name(self):
        return model_tool_name(self.name)

    def to_native(self):
        return {'type': 'function', 'function': {'name': self.wire_name,
                'description': self.description, 'parameters': self.parameters}}

    def validate(self, arguments):
        if not isinstance(arguments, dict):
            raise ToolProtocolError('工具参数必须为 JSON 对象')
        try:
            jsonschema.validate(arguments, self.parameters)
            if isinstance(self.input_model, dict):
                return copy.deepcopy(arguments)
            return self.input_model.model_validate(arguments, strict=True)
        except (ValueError, TypeError, jsonschema.ValidationError) as error:
            raise ToolProtocolError('工具参数校验失败：' + sanitize(str(error))[:2000]) from error


@dataclass(frozen=True)
class ToolDefinition(Generic[InputT, OutputT]):
    spec: ToolSpec
    handler: ToolHandler[InputT, OutputT]

    def __post_init__(self):
        if not isinstance(self.spec, ToolSpec) or not callable(self.handler):
            raise TypeError('工具定义必须包含有效规格和可调用处理器')


def build_tool(name: str, description: str, input_model: type[InputT] | dict,
               handler: ToolHandler[InputT, OutputT], *, output_model: type[OutputT] | dict | None = None,
               **spec_options) -> ToolDefinition[InputT, OutputT]:
    return ToolDefinition(ToolSpec(name, description, input_model,
                                  output_model=output_model, **spec_options), handler)


@dataclass(frozen=True)
class ValidatedToolCall:
    call_id: str
    name: str
    arguments: dict
    input: BaseModel | dict
    identity: tuple[str, str]
    spec: ToolSpec


class ToolResult(Contract):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)

    call_id: str
    name: str
    result: Any = None
    is_error: bool = Field(default=False, alias='isError')
    executed: bool = False
    error: dict | None = None
    artifact_ref: str | None = None
    truncated: bool = False
    images: list[dict] = Field(default_factory=list)

    def to_content(self):
        return json.dumps(self.model_dump(mode='json', by_alias=True, exclude={'images'}), ensure_ascii=False)


class ToolRegistry:
    def __init__(self, specs=()):
        self._specs = {}
        self._wire_specs = {}
        self._legacy_specs = {}
        self._lookup_specs = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec):
        if not isinstance(spec, ToolSpec):
            raise ValueError('工具注册类型无效')
        legacy = spec.name.replace('.', '_')
        if len(legacy) > 64:
            legacy = legacy[:47] + '_' + digest(spec.name)[:16]
        names = {spec.name, spec.wire_name, legacy, *spec.aliases}
        for name in names:
            if name in self._lookup_specs:
                raise ValueError('工具名称或别名重复：' + name)
        self._specs[spec.name] = spec
        self._wire_specs[spec.wire_name] = spec
        self._legacy_specs[legacy] = spec
        self._lookup_specs.update({name: spec for name in names})
        return spec

    @property
    def specs(self):
        return tuple(self._specs.values())

    def contains(self, name, phase=None):
        spec = self._lookup_specs.get(name)
        return spec is not None and spec.enabled and (phase is None or Phase(phase) in spec.phases)

    def contains_wire(self, name, phase=None):
        spec = self._wire_specs.get(name)
        return spec is not None and spec.enabled and (phase is None or Phase(phase) in spec.phases)

    def visible(self, phase):
        phase = Phase(phase)
        return tuple(spec for spec in self._specs.values() if spec.enabled and phase in spec.phases)

    def native_tools(self, phase):
        return [spec.to_native() for spec in self.visible(phase)]

    def get(self, name, phase=None):
        spec = self._lookup_specs.get(name)
        if spec is None or not spec.enabled or phase is not None and Phase(phase) not in spec.phases:
            raise ToolProtocolError('未知或当前阶段未授权的工具：' + str(name))
        return spec

    def get_wire(self, name, phase=None):
        spec = self._wire_specs.get(name)
        if spec is None or not spec.enabled or phase is not None and Phase(phase) not in spec.phases:
            raise ToolProtocolError('未知或当前阶段未授权的工具别名：' + str(name))
        return spec

    def validate_batch(self, calls, phase, completed_calls=None, *, wire_names=False):
        if not isinstance(calls, list) or not calls:
            raise ToolProtocolError('tool_calls 必须为非空数组')
        completed_calls = completed_calls or {}
        validated, seen = [], set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ToolProtocolError('tool_call 必须为 function 类型')
            call_id, function = call.get('id'), call.get('function')
            if (not isinstance(call_id, str) or not call_id.strip() or call_id in seen
                    or not isinstance(function, dict)):
                raise ToolProtocolError('tool_call 的 id/function 无效或同批 id 重复')
            seen.add(call_id)
            name, raw_arguments = function.get('name'), function.get('arguments')
            if not isinstance(name, str) or not isinstance(raw_arguments, str):
                raise ToolProtocolError('function.name 必须是字符串，arguments 必须是 JSON 字符串')
            try:
                arguments = json.loads(raw_arguments)
            except (ValueError, TypeError) as error:
                raise ToolProtocolError('工具参数不是有效 JSON') from error
            spec = self.get_wire(name, phase) if wire_names else self.get(name, phase)
            canonical_name = spec.name
            value = spec.validate(arguments)
            identity = (canonical_name, json.dumps(arguments, sort_keys=True))
            # 相同 call_id 只能重放同一操作，禁止复用 ID 绕过回执的幂等检查。
            if call_id in completed_calls and completed_calls[call_id][0] != identity:
                raise ToolProtocolError('tool_call_id 不可复用于不同参数')
            validated.append(ValidatedToolCall(call_id, canonical_name, arguments, value, identity, spec))
        if any(call.spec.submission for call in validated) and len(validated) != 1:
            raise ToolProtocolError('提交工具必须独占一个批次')
        return validated


def estimate_tokens(text):
    return max(1, math.ceil(len(text.encode('utf-8')) / 3))


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


class ToolPipeline:
    def __init__(self, registry, handlers: Mapping[str, Callable], phase, *, operation=None,
                 emit=None, store_artifact=None, count_tokens=None, scope_check=None,
                 approve=None, resource_resolver=None):
        self.registry, self.handlers, self.phase = registry, dict(handlers), Phase(phase)
        self.operation, self.emit, self.store_artifact = operation, emit, store_artifact
        self.count_tokens = count_tokens or estimate_tokens
        self.scope_check, self.approve = scope_check, approve
        self.resource_resolver = resource_resolver
        self.submission_value = None
        self.completed_calls = {}

    async def _event(self, kind, payload):
        if self.emit:
            await _resolve(self.emit(kind, payload))

    def _submission(self, call, result):
        if call.spec.submission_schema is not None:
            value = (result.result['value'] if isinstance(result.result, dict)
                     and 'value' in result.result else call.input.model_dump(mode='json'))
            return call.spec.submission_schema.model_validate(value)
        return call.input

    def _rejected(self, call, error):
        return ToolResult(call_id=call.call_id, name=call.name, isError=True,
                          error={'type': type(error).__name__, 'message': sanitize(str(error)),
                                 'executed': False})

    async def _shape(self, call, result):
        serialized = result.to_content()
        if self.count_tokens(serialized) <= call.spec.output_limit_tokens:
            return result
        if not self.store_artifact:
            raise RuntimeError('超限工具结果必须配置完整 artifact 存储')
        # 先保存完整回执，再裁剪模型可见正文；审计和后续读取仍可定位原始结果。
        result.artifact_ref = await _resolve(self.store_artifact(result.model_dump(mode='json', by_alias=True)))
        text = json.dumps(result.result, ensure_ascii=False, default=str)
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = result.model_copy(update={'result': {'summary': text[:middle]}, 'truncated': True})
            if self.count_tokens(candidate.to_content()) <= call.spec.output_limit_tokens:
                low = middle
            else:
                high = middle - 1
        result.result, result.truncated = {'summary': text[:low]}, True
        return result

    async def _execute(self, call):
        if self.scope_check:
            await _resolve(self.scope_check())
        handler = self.handlers.get(call.name)
        if handler is None:
            raise ToolProtocolError('工具尚未绑定执行器：' + call.name)
        effect = call.spec.side_effect in {'write', 'external'}
        if effect and self.operation is None:
            raise ToolProtocolError('write/external 工具缺少 operation receipt 执行器')
        if call.spec.approval != 'never':
            # 审批在执行前完成；未配置审批器也按拒绝处理，不默认放行。
            if self.approve is None or not await _resolve(self.approve(call.spec, call.input)):
                result = self._rejected(call, ToolRejected('工具审批或策略检查未通过'))
                await self._event('tool.error', result.model_dump(mode='json', by_alias=True))
                return result
        arguments = (call.input.model_dump(mode='json')
                     if isinstance(call.input, BaseModel) else call.input)
        intent = {'tool_name': call.name,
                  'arguments': arguments, 'side_effect': call.spec.side_effect}
        if effect and self.resource_resolver:
            intent['resources'] = self.resource_resolver(call.name, arguments)

        async def perform():
            try:
                async with asyncio.timeout(call.spec.timeout_s):
                    value = await _resolve(handler(call.input, call.call_id))
                if isinstance(value, ToolResult):
                    if not value.is_error:
                        value = value.model_copy(update={'result': call.spec.validate_output(value.result)})
                    return value.model_dump(mode='json', by_alias=True)
                value = call.spec.validate_output(value)
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode='json')
                images = []
                if isinstance(value, dict) and value.get('type') == 'image' and 'base64' in value:
                    value = dict(value)
                    encoded = value.pop('base64')
                    images.append({'type': 'image_url', 'image_url': {
                        'url': 'data:' + value['mime_type'] + ';base64,' + encoded}})
                return ToolResult(call_id=call.call_id, name=call.name, result=value, images=images,
                                  executed=True).model_dump(mode='json', by_alias=True)
            except ToolRejected as error:
                return self._rejected(call, error).model_dump(mode='json', by_alias=True)

        if not effect:
            await self._event('tool.started', {'tool_call_id': call.call_id, 'intent': intent})
        try:
            if effect:
                await self._event('tool.requested', {'tool_call_id': call.call_id, 'intent': intent})
                # operation 包装负责持久化执行意图及回执，不能绕过它直接写入。
                key = call.spec.idempotency_key(call.input)
                if not isinstance(key, str) or not key.strip():
                    raise ToolProtocolError('工具幂等键必须为非空字符串')
                # 阶段提交按当前调用验收；真实写入仍使用稳定工具身份，避免跨调用重放副作用。
                prefix = call.call_id if call.spec.submission else call.name
                receipt = await self.operation(call.name, intent, perform,
                                               idempotency_key=prefix + ':' + key)
            else:
                receipt = await perform()
            result = ToolResult.model_validate(receipt).model_copy(
                update={'call_id': call.call_id, 'name': call.name})
        except Exception as error:
            await self._event('tool.error', {'tool_call_id': call.call_id, 'tool_name': call.name,
                              'error': sanitize(str(error)), 'side_effect': call.spec.side_effect})
            if hasattr(error, 'status') or type(error).__name__ == 'UnknownOperation':
                raise
            if effect and isinstance(error, (ToolRejected, PermissionError, ToolProtocolError)):
                result = self._rejected(call, error)
            elif effect:
                # 非确定拒绝的副作用失败可能已生效，转为未知操作等待人工复核。
                raise ToolOperationUnknown('工具副作用结果未知：' + sanitize(str(error)),
                                           call_id=call.call_id, name=call.name,
                                           operation_id=getattr(error, 'tracefix_operation_id', None)) from error
            if isinstance(error, (ValueError, PermissionError, FileNotFoundError, TimeoutError)):
                result = self._rejected(call, error)
            else:
                raise
        result = await self._shape(call, result)
        if not effect:
            await self._event('tool.error' if result.is_error else 'tool.completed',
                              {'tool_call_id': call.call_id, 'receipt': result.model_dump(mode='json', by_alias=True)})
        if call.spec.submission and not result.is_error:
            self.submission_value = self._submission(call, result)
        return result

    async def execute_batch(self, calls, completed_calls=None):
        completed = self.completed_calls if completed_calls is None else completed_calls
        if calls and isinstance(calls[0], ValidatedToolCall):
            wire_calls = [{'id': call.call_id, 'type': 'function', 'function': {
                'name': call.name, 'arguments': json.dumps(call.arguments)}} for call in calls]
        else:
            wire_calls = calls
        wire_names = bool(wire_calls) and all(
            isinstance(call, dict) and self.registry.contains_wire(call.get('function', {}).get('name'), self.phase)
            for call in wire_calls)
        # 整批参数与阶段权限全部通过后才执行，避免坏调用导致前面的操作先行生效。
        validated = self.registry.validate_batch(wire_calls, self.phase, completed, wire_names=wire_names)
        for call in validated:
            if call.name not in self.handlers:
                raise ToolProtocolError('工具尚未绑定执行器：' + call.name)
        results = [None] * len(validated)

        async def run_one(index):
            call = validated[index]
            if call.call_id in completed:
                saved = completed[call.call_id][1]
                result = (ToolResult.model_validate_json(saved) if isinstance(saved, str)
                          else ToolResult.model_validate(saved))
                if call.spec.submission and not result.is_error:
                    self.submission_value = self._submission(call, result)
            else:
                result = await self._execute(call)
                completed[call.call_id] = (call.identity, result.to_content())
            results[index] = result

        index = 0
        while index < len(validated):
            call = validated[index]
            # 只有相邻的安全只读调用并行；写入/外部操作形成屏障并保留批次顺序。
            if call.spec.parallel_safe and call.spec.side_effect in {'none', 'read'}:
                end = index
                while (end < len(validated) and validated[end].spec.parallel_safe
                       and validated[end].spec.side_effect in {'none', 'read'}):
                    end += 1
                try:
                    async with asyncio.TaskGroup() as group:
                        for position in range(index, end):
                            group.create_task(run_one(position))
                except ExceptionGroup as errors:
                    pending = list(errors.exceptions)
                    flattened = []
                    while pending:
                        error = pending.pop(0)
                        if isinstance(error, ExceptionGroup):
                            pending.extend(error.exceptions)
                        else:
                            flattened.append(error)
                    selected = next((error for error in flattened if hasattr(error, 'status')),
                                    flattened[0])
                    raise selected from errors
                index = end
            else:
                await run_one(index)
                index += 1
        return results

    async def execute(self, name, arguments, call_id):
        calls = [{'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments)}}]
        return (await self.execute_batch(calls))[0]
