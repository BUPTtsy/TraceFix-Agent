"""适配 Chat Completions 协议，校验结构化输出并记录重试与真实用量。"""
import asyncio
import base64
import json
import os
import copy
import math
import re
import uuid
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, ValidationError

from tracefix.model.prompts import serialize_request, system_instructions
from tracefix.messages import error_message, validation_details
from tracefix.storage.artifacts import sanitize
from tracefix.model.chat_completions import ChatCompletionsAdapter, IncompleteCompletion
from tracefix.runtime.contracts import Phase, digest
from tracefix.runtime.tools import ToolRegistry, ToolSpec, model_tool_name


@dataclass
class ModelResult:
    # 一次逻辑模型调用的结构化结果；重试和工具轮次沿用同一外层契约。
    value: BaseModel
    usage: dict
    model_revision: str
    finish_reason: str


class ModelError(RuntimeError):
    """A model call failed, with machine-readable state for run auditing."""

    def __init__(self, message, *, status='FAILED', category='model', details=None):
        super().__init__(message)
        self.status = status
        self.category = category
        self.details = details or {}


class ModelOutputError(ModelError):
    pass


def _retry_after_seconds(headers, *, now=None):
    """Parse Retry-After seconds or an HTTP date, ignoring invalid values."""
    value = next((candidate for key, candidate in headers.items()
                  if key.lower() == 'retry-after'), None)
    if value is None:
        return None
    try:
        delay = float(str(value).strip())
        if math.isfinite(delay):
            return max(0.0, delay)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(value).strip())
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (retry_at - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class Gateway:
    # 处理传输、工具协议和上下文窗口；运行时通过回调保存请求与 reasoning 审计。
    supports_tool_executor = True
    supports_streaming = True
    supports_context_assembler = True
    supports_tool_history_projection = True

    def __init__(self, base_url=None, key=None, text_model=None, vision_model=None,
                 max_output_tokens=20480, timeout=None, max_attempts=None,
                 max_retry_delay=60, tool_mode=None, tool_executor=None,
                 max_tool_rounds=40, thinking=None, stream=None, additional_tools=None):
        self.base_url = (base_url or os.getenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')).rstrip('/')
        self.key = key or os.getenv('TRACEFIX_API_KEY', '')
        self.text_model = text_model or os.getenv('TRACEFIX_TEXT_MODEL', 'deepseek-chat')
        self.vision_model = (vision_model if vision_model is not None else os.getenv('TRACEFIX_VISION_MODEL', '')).strip()
        self.max_output_tokens = max_output_tokens
        try:
            self.timeout = float(timeout if timeout is not None else os.getenv('TRACEFIX_MODEL_TIMEOUT', '240'))
        except (ValueError, TypeError) as error:
            raise ValueError('timeout must be a positive finite number') from error
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('timeout must be a positive finite number')
        configured_stream = os.getenv('TRACEFIX_STREAM', 'true') if stream is None else stream
        if isinstance(configured_stream, str) and configured_stream.lower() in {'true', 'false'}:
            configured_stream = configured_stream.lower() == 'true'
        if type(configured_stream) is not bool:
            raise ValueError('stream must be true or false')
        self.stream = configured_stream
        # thinking 默认开启；只有显式传入 disabled 或配置环境变量才关闭。
        default_thinking = 'enabled' if urlsplit(self.base_url).hostname == 'api.deepseek.com' else None
        self.thinking = thinking if thinking is not None else os.getenv('TRACEFIX_THINKING', default_thinking)
        if self.thinking is not None and self.thinking not in {'enabled', 'disabled'}:
            raise ValueError('thinking must be enabled or disabled')
        self.additional_tools = list(additional_tools or [])
        self.adapter = ChatCompletionsAdapter()
        self.tool_mode = (tool_mode or os.getenv('TRACEFIX_TOOL_MODE', 'native')).strip().lower()
        if self.tool_mode not in {'native', 'json'}:
            raise ValueError("tool_mode must be 'native' or 'json'")
        self.tool_executor = tool_executor
        try:
            parsed_rounds = int(max_tool_rounds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_tool_rounds must be a positive integer') from exc
        if (isinstance(max_tool_rounds, bool) or parsed_rounds < 1
                or isinstance(max_tool_rounds, float) and not max_tool_rounds.is_integer()):
            raise ValueError('max_tool_rounds must be a positive integer')
        self.max_tool_rounds = parsed_rounds
        configured_attempts = (os.getenv('TRACEFIX_MODEL_MAX_ATTEMPTS', '3')
                               if max_attempts is None else max_attempts)
        if isinstance(configured_attempts, bool):
            raise ValueError('max_attempts must be a positive integer')
        try:
            parsed_attempts = int(configured_attempts)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('max_attempts must be a positive integer') from exc
        if isinstance(configured_attempts, float) and not configured_attempts.is_integer():
            raise ValueError('max_attempts must be a positive integer')
        if parsed_attempts < 1:
            raise ValueError('max_attempts must be a positive integer')
        self.max_attempts = parsed_attempts
        self.max_retry_delay = max(0.0, float(max_retry_delay))

    @staticmethod
    def _native_tools(schema):
        # 工具 schema 只声明动作格式；实际执行仍受运行时策略控制。
        from tracefix.runtime.contracts import BrowserAction, Decision
        if schema not in {BrowserAction, Decision}:
            return []
        action_schema = BrowserAction.model_json_schema()
        tools = []
        for name, fields in {
            'browser_navigate': ['value'],
            'browser_click': ['observation_id', 'element_ref', 'locator'],
            'browser_type': ['observation_id', 'element_ref', 'locator', 'value'],
            'browser_select': ['observation_id', 'element_ref', 'locator', 'value'],
            'browser_press': ['observation_id', 'value'],
            'browser_snapshot': [],
        }.items():
            optional_fields = ['page_generation', 'preconditions', 'postconditions', 'wait']
            properties = {field: copy.deepcopy(action_schema['properties'][field])
                          for field in fields + optional_fields}
            for field in fields:
                property_schema = properties[field]
                property_schema.pop('default', None)
                if 'anyOf' in property_schema:
                    property_schema.update(property_schema.pop('anyOf')[0])
            tools.append({'type': 'function', 'function': {
                'name': model_tool_name(name),
                'description': 'Execute through TraceFix policy and MCP. Returns a fresh observation and screenshot evidence reference. value is the URL, text, selected value, or key for the named action.',
                'parameters': {'type': 'object', 'properties': properties,
                    'required': fields, 'additionalProperties': False, '$defs': action_schema.get('$defs', {})},
            }})
        return tools

    @classmethod
    def browser_action(cls, name, arguments):
        import jsonschema
        from tracefix.runtime.contracts import BrowserAction
        definitions = {tool['function']['name']: tool['function']['parameters']
                       for tool in cls._native_tools(BrowserAction)}
        name = model_tool_name(name) if isinstance(name, str) else name
        if not isinstance(name, str) or name not in definitions:
            raise ValueError('未知或未授权的浏览器工具：' + str(name))
        try:
            jsonschema.validate(arguments, definitions[name])
        except jsonschema.ValidationError as error:
            raise ValueError('工具参数校验失败：' + error.message) from error
        kind = {'BrowserNavigate': 'navigate', 'BrowserClick': 'click',
                'BrowserType': 'type', 'BrowserSelect': 'select', 'BrowserPress': 'press',
                'BrowserSnapshot': 'observe'}[name]
        return BrowserAction(kind=kind, **arguments)

    @classmethod
    def _validated_calls(cls, calls, native_tools, completed_calls):
        # 整批校验通过后才允许执行，避免前半批已执行而后半批存在非法调用。
        if not native_tools:
            raise ValueError('当前模型输出类型或 tool_mode 不允许调用浏览器工具')
        if not isinstance(calls, list) or not calls:
            raise ValueError('tool_calls 必须为非空数组')
        validated = []
        seen = set()
        for call in calls:
            if not isinstance(call, dict) or call.get('type') != 'function':
                raise ValueError('tool_call 必须为 function 类型')
            call_id = call.get('id')
            function = call.get('function')
            if (not isinstance(call_id, str) or not call_id.strip()
                    or call_id in seen or not isinstance(function, dict)):
                raise ValueError('tool_call 的 id/function 无效或同批 id 重复')
            seen.add(call_id)
            name = function.get('name')
            raw_arguments = function.get('arguments')
            if not isinstance(raw_arguments, str):
                raise ValueError('function.arguments 必须是 JSON 字符串')
            arguments = json.loads(raw_arguments)
            definitions = {tool['function']['name']: tool['function']['parameters'] for tool in native_tools}
            if name not in definitions:
                raise ValueError('未知或未授权的工具：' + str(name))
            import jsonschema
            try:
                jsonschema.validate(arguments, definitions[name])
            except jsonschema.ValidationError as error:
                raise ValueError('工具参数校验失败：' + error.message) from error
            if name.startswith('Browser'):
                cls.browser_action(name, arguments)
            identity = (name, json.dumps(arguments, sort_keys=True))
            if call_id in completed_calls and completed_calls[call_id][0] != identity:
                raise ValueError('tool_call_id 不可复用于不同参数')
            validated.append((call_id, name, arguments, identity))
        return validated

    @classmethod
    def _completed_history(cls, messages, native_tools):
        if not isinstance(messages, list) or not messages:
            raise ValueError('messages 必须为非空数组')
        completed = {}
        pending = {}
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError('历史消息必须为对象')
            role = message.get('role')
            if role == 'tool':
                call_id = message.get('tool_call_id')
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError('tool 消息缺少配对的 assistant tool_call')
                identity = pending.pop(call_id)
                if (not isinstance(message.get('content'), str)
                        or message.get('name', identity[0]) != identity[0]):
                    raise ValueError('tool 消息的 content/name 无效')
                completed[call_id] = (identity, message['content'])
            else:
                if pending:
                    raise ValueError('assistant tool_calls 尚未收到完整工具结果')
                if role not in {'system', 'user', 'assistant'}:
                    raise ValueError('历史消息 role 无效')
                if message.get('tool_calls') is not None:
                    if role != 'assistant':
                        raise ValueError('只有 assistant 消息可以包含 tool_calls')
                    pending = {call_id: identity for call_id, name, arguments, identity in
                               cls._validated_calls(message['tool_calls'], native_tools, completed)}
        if pending:
            raise ValueError('assistant tool_calls 缺少工具结果，不能自动重放')
        return completed

    @staticmethod
    def _wire_history(messages, registry, phase):
        history = copy.deepcopy(messages)
        if not isinstance(history, list):
            return history
        for message in history:
            if not isinstance(message, dict):
                continue
            functions = [call.get('function') for call in message.get('tool_calls', [])
                         if isinstance(call, dict)]
            if message.get('role') == 'tool' and 'name' in message:
                functions.append(message)
            for function in functions:
                if not isinstance(function, dict) or not isinstance(function.get('name'), str):
                    continue
                name = function['name']
                if registry.contains(name, phase):
                    function['name'] = registry.get(name, phase).wire_name
        return history

    @staticmethod
    async def _call_tool_executor(executor, name, arguments, call_id):
        if executor is None:
            raise ModelOutputError('模型请求执行工具，但未配置 tool_executor', category='tool_execution')
        return await executor(name, arguments, call_id)

    @staticmethod
    def _tool_content(value):
        if hasattr(value, 'to_content'):
            return value.to_content()
        if isinstance(value, BaseModel):
            value = value.model_dump(mode='json')
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({'result': sanitize(str(value))}, ensure_ascii=False)

    @staticmethod
    def _payload_tokens(counter, payload):
        bounded = copy.deepcopy(payload)
        image_count = 0
        for message in bounded.get('messages', []):
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get('type') != 'image_url':
                    continue
                image_count += 1
                part['image_url'] = {'url': '[IMAGE]'}
        return counter.count(bounded) + image_count * 4096

    @classmethod
    def _protected_tool_result(cls, value):
        if isinstance(value, list):
            return any(cls._protected_tool_result(item) for item in value)
        if not isinstance(value, dict):
            return False
        if (value.get('error') or value.get('isError') or value.get('is_error')
                or value.get('passed') is False or value.get('executed') is False):
            return True
        for field in ('status', 'operation_status', 'business_outcome'):
            status = value.get(field)
            if isinstance(status, str) and any(marker in status.casefold() for marker in
                    ('unknown', 'waiting', 'pending', 'running', 'failed', 'cancelled', 'canceled')):
                return True
        return any(cls._protected_tool_result(item) for item in value.values())

    @classmethod
    def _project_tool_message(cls, message, metadata):
        if not isinstance(message, dict) or message.get('role') != 'tool':
            return False
        reference = metadata.get('result_ref') if isinstance(metadata, dict) else None
        content = message.get('content')
        binding = metadata.get('binding') if isinstance(metadata, dict) else None
        if (not isinstance(reference, str) or not reference.strip() or not isinstance(content, str)
                or metadata.get('expandable') is not True
                or metadata.get('content_hash') != digest(content)
                or len(content) > 16000 or len(content.splitlines()) > 200
                or not isinstance(binding, dict)
                or not {'scope_id', 'source_manifest', 'patch_hash', 'environment_digest',
                        'test_spec_hash'}.issubset(binding)):
            return False
        try:
            original = json.loads(content)
        except (TypeError, ValueError):
            return False
        if not isinstance(original, dict) or cls._protected_tool_result(original):
            return False
        projected = copy.deepcopy(original)
        name = message.get('name', '')
        browser_fields = {'isError', 'is_error', 'executed', 'error', 'business_outcome',
                          'observation_ref', 'observation', 'receipt', 'operation_id',
                          'status', 'operation_status', 'artifact_ref', 'result_ref', 'truncated'}
        result_fields = {'call_id', 'name', 'result', 'isError', 'is_error', 'executed',
                         'error', 'artifact_ref', 'truncated'}
        if name in {'BrowserNavigate', 'BrowserClick', 'BrowserType', 'BrowserSelect',
                    'BrowserPress', 'BrowserSnapshot'}:
            observation = original.get('observation')
            if (not set(original).issubset(browser_fields) or original.get('executed') is not True
                    or not isinstance(observation, dict)
                    or not isinstance(observation.get('snapshot'), str)):
                return False
            projected['observation']['snapshot'] = '[历史观测正文已投影；按 tool_history_ref 展开，已结算动作不得重放]'
            omitted_fields = ['observation.snapshot']
        elif name == 'Bash':
            result = original.get('result')
            if (not set(original).issubset(result_fields)
                    or original.get('call_id') != message.get('tool_call_id')
                    or model_tool_name(str(original.get('name', ''))) != name
                    or original.get('executed') is not True or not isinstance(result, dict)
                    or not set(result).issubset({'passed', 'exit_code', 'output', 'files_changed',
                                                'discarded_changes', 'cwd'})
                    or result.get('passed') is not True or result.get('exit_code') != 0
                    or not isinstance(result.get('output'), str)):
                return False
            projected['result']['output'] = '[历史命令正文已投影；按 tool_history_ref 展开，已结算动作不得重放]'
            omitted_fields = ['result.output']
        else:
            return False
        projected['tool_history_ref'] = {
            'result_ref': reference, 'channel': 'tool_content',
            'content_hash': digest(content), 'content_length': len(content),
            'coverage': 'projected', 'omitted_fields': omitted_fields,
            'binding': copy.deepcopy(binding),
            'lookup_hint': {'tool': 'context.expand', 'ref': reference,
                            'channel': 'tool_content', 'max_chars': 16000}}
        message['content'] = json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        return True

    @staticmethod
    def _tool_history_candidates(messages):
        groups = []
        pending = set()
        indexes = []
        for index, message in enumerate(messages):
            if message.get('role') == 'assistant' and message.get('tool_calls'):
                if pending:
                    return []
                pending = {call['id'] for call in message['tool_calls']}
                indexes = []
            elif message.get('role') == 'tool':
                call_id = message.get('tool_call_id')
                if call_id not in pending:
                    return []
                pending.remove(call_id)
                indexes.append(index)
                if not pending:
                    groups.append(indexes)
            elif pending:
                return []
        if pending:
            return []
        return [index for group in groups[:-1] for index in group]

    @classmethod
    def _project_next_tool(cls, payload, tool_result_refs, counter, candidates):
        before = cls._payload_tokens(counter, payload)
        while candidates:
            index = candidates.pop(0)
            message = payload['messages'][index]
            call_id = message.get('tool_call_id')
            original_content = message.get('content')
            if not cls._project_tool_message(message, tool_result_refs.get(call_id)):
                continue
            if cls._payload_tokens(counter, payload) < before:
                return call_id
            message['content'] = original_content
        return None

    @staticmethod
    def _replace_context(messages, text):
        for message in messages:
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text':
                        part['text'] = text
                        return
            else:
                message['content'] = text
                return

    @classmethod
    def _protocol_tokens(cls, payload, schema, counter):
        overhead = copy.deepcopy(payload)
        cls._replace_context(overhead['messages'], '')
        overhead['response_json_schema'] = schema.model_json_schema()
        return cls._payload_tokens(counter, overhead)

    @classmethod
    def _budget_payload(cls, payload, schema, context, assembler, tool_result_refs,
                        can_expand, *, preserve=False):
        from tracefix.knowledge.assembler import ContextWindowError

        counter = assembler.counter
        before = cls._payload_tokens(counter, payload)
        candidates = cls._tool_history_candidates(payload['messages']) if can_expand else []
        projected = []
        assembly = None
        extra_tokens = cls._protocol_tokens(payload, schema, counter)
        if not preserve:
            while True:
                try:
                    assembly = assembler.assemble(context, extra_tokens=extra_tokens)
                    break
                except ContextWindowError:
                    call_id = cls._project_next_tool(payload, tool_result_refs, counter, candidates)
                    if call_id is None:
                        raise
                    projected.append(call_id)
                    extra_tokens = cls._protocol_tokens(payload, schema, counter)
            cls._replace_context(payload['messages'], serialize_request(schema, assembly.context))
            for correction in range(3):
                required = cls._payload_tokens(counter, payload)
                if required <= assembler.available:
                    break
                adjusted_extra = extra_tokens + required - assembler.available
                try:
                    adjusted = assembler.assemble(context, extra_tokens=adjusted_extra)
                except ContextWindowError:
                    break
                proposal = copy.deepcopy(payload)
                cls._replace_context(proposal['messages'], serialize_request(schema, adjusted.context))
                if cls._payload_tokens(counter, proposal) >= required:
                    break
                payload['messages'] = proposal['messages']
                assembly = adjusted
                extra_tokens = adjusted_extra
        required = cls._payload_tokens(counter, payload)
        while required > assembler.available:
            call_id = cls._project_next_tool(payload, tool_result_refs, counter, candidates)
            if call_id is None:
                break
            projected.append(call_id)
            required = cls._payload_tokens(counter, payload)
        if assembly is not None:
            manifest = copy.deepcopy(assembly.manifest)
        else:
            manifest = {'version': 'tracefix/context/1', 'counter': counter.name,
                        'exact_tokenizer': counter.exact, 'context_window': assembler.context_window,
                        'input_limit': assembler.available, 'tokens_before': before,
                        'tokens_after': required, 'context_hash': digest(context),
                        'blocks': [], 'workset': {}}
        manifest.update(request_tokens=required,
                        protocol_tokens=cls._protocol_tokens(payload, schema, counter),
                        image_tokens_reserved=sum(4096 for message in payload['messages']
                            if isinstance(message.get('content'), list) for part in message['content']
                            if isinstance(part, dict) and part.get('type') == 'image_url'))
        if projected:
            manifest['tool_history_projection'] = {
                'call_ids': projected, 'coverage': 'artifact_ref',
                'result_refs': {call_id: tool_result_refs[call_id]['result_ref'] for call_id in projected},
                'unprojected_call_ids': [message['tool_call_id'] for message in payload['messages']
                    if message.get('role') == 'tool' and message['tool_call_id'] not in projected]}
            manifest['tool_history_projected'] = True
        compacted = bool(projected) or bool(assembly and assembly.compacted)
        return manifest, compacted

    async def generate(self, schema, context, image=None, agent_instructions=None,
                       on_attempt=None, on_response=None, on_error=None, on_usage=None,
                       validate_output=None, tool_executor=None, messages=None, on_tool_result=None,
                       context_provider=None, on_delta=None, tool_registry=None, tool_pipeline=None,
                       context_assembler=None, on_context=None, preserve_resumed_request=False,
                       tool_result_refs=None):
        # 重试和工具轮次共享消息历史；on_response 将每次响应交给运行时持久化。
        if not self.key:
            raise ModelError('未配置 TRACEFIX_API_KEY', status='FAILED', category='configuration',
                             details={'status': 'FAILED', 'category': 'configuration',
                                      'requires_manual_review': False})
        text = serialize_request(schema, context)
        content = [{'type': 'text', 'text': text}]
        image = image if self.vision_model else None
        if image:
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}})
        model = self.vision_model if image else self.text_model
        phase = context.get('phase') or {'TestSpec': 'PREPARE', 'Decision': 'EXPLORE',
            'BrowserAction': 'EXPLORE', 'PatchProposal': 'DIAGNOSE'}.get(schema.__name__, 'DIAGNOSE')
        phase = Phase(phase)
        browser_tools = self._native_tools(schema) if self.tool_mode == 'native' else []
        offered_registry = ToolRegistry()
        if self.tool_mode == 'native':
            for tool in browser_tools:
                function = tool['function']
                internal_name = 'browser_' + re.sub(r'(?<!^)(?=[A-Z])', '_',
                    function['name'][len('Browser'):]).lower()
                offered_registry.register(ToolSpec(internal_name, function['description'],
                    function['parameters'], side_effect='external', phases=frozenset({phase}),
                    idempotency_key=digest, category='browser'))
            source_registry = tool_registry or (tool_pipeline.registry if tool_pipeline else None)
            if source_registry is not None:
                for spec in source_registry.visible(phase):
                    offered_registry.register(spec)
            for spec in self.additional_tools:
                if phase in spec.phases:
                    offered_registry.register(spec)
        native_tools = offered_registry.native_tools(phase)
        names = [tool['function']['name'] for tool in native_tools]
        if len(names) != len(set(names)):
            raise ValueError('工具名重复')
        system = system_instructions(schema, context, agent_instructions=agent_instructions,
                                     native_tools=bool(native_tools))
        delegation_policy = ''
        if any(spec.name.startswith('agent.') for spec in offered_registry.visible(phase)):
            from tracefix.workers.prompt_policy import SUPERVISOR_DELEGATION_POLICY
            delegation_policy = SUPERVISOR_DELEGATION_POLICY
            for spec in offered_registry.visible(phase):
                delegation_policy = delegation_policy.replace(spec.name, spec.wire_name)
            system += '\n<supervisor_tool_protocol>\n' + delegation_policy + '\n</supervisor_tool_protocol>'
        payload = {'model': model, 'messages': [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': content if image else text}],
                   'max_tokens': self.max_output_tokens, 'stream': self.stream}
        if self.stream:
            payload['stream_options'] = {'include_usage': True}
        if not native_tools:
            payload['response_format'] = {'type': 'json_object'}
        if native_tools:
            payload['tools'] = native_tools
            payload['tool_choice'] = 'auto'
            payload['parallel_tool_calls'] = not bool(browser_tools)
        # reasoning 是否返回取决于供应商；返回的字段经 adapter 保留，再进入运行时审计。
        if self.thinking is not None:
            payload['thinking'] = {'type': self.thinking}
        if messages is not None:
            payload['messages'] = copy.deepcopy(messages)
        logical_exchange_id = uuid.uuid4().hex
        tool_rounds = 0
        attempt = 0
        try:
            payload['messages'] = self._wire_history(payload['messages'], offered_registry, phase)
            completed_calls = self._completed_history(payload['messages'], native_tools)
            for history_message in payload['messages']:
                if history_message.get('tool_calls') is not None:
                    offered_registry.validate_batch(history_message['tool_calls'], phase,
                                                    wire_names=True)
        except (ValueError, TypeError) as exc:
            raise ModelOutputError('工具历史无效：' + str(exc), category='tool_protocol') from exc
        full_messages = copy.deepcopy(payload['messages'])
        tool_result_refs = copy.deepcopy(tool_result_refs or {})
        tool_rounds = sum(message.get('role') == 'assistant' and bool(message.get('tool_calls'))
                          for message in full_messages)
        resumed_tool_round = tool_rounds
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            while attempt < self.max_attempts:
                attempt += 1
                payload['messages'] = copy.deepcopy(full_messages)
                # 每次请求重新组装上下文；配置 provider 时同步最新引导、观测和记忆。
                preserve = (preserve_resumed_request and messages is not None
                            and tool_rounds == resumed_tool_round)
                current_context = context
                if (context_provider or messages is not None or context_assembler is not None) and not preserve:
                    current_context = context_provider() if context_provider else context
                    current_system = system_instructions(schema, current_context,
                        agent_instructions=agent_instructions, native_tools=bool(native_tools))
                    if delegation_policy:
                        current_system += '\n<supervisor_tool_protocol>\n' + delegation_policy + '\n</supervisor_tool_protocol>'
                    for system_message in payload['messages']:
                        if system_message.get('role') == 'system':
                            system_message['content'] = current_system
                            break
                    self._replace_context(payload['messages'], serialize_request(schema, current_context))
                if context_assembler is not None:
                    manifest, compacted = self._budget_payload(payload, schema, current_context,
                        context_assembler, tool_result_refs,
                        offered_registry.contains('context.expand', phase), preserve=preserve)
                    if on_context and (not preserve or compacted):
                        on_context(manifest, compacted)
                    if manifest['request_tokens'] > context_assembler.available:
                        from tracefix.knowledge.assembler import ContextWindowError
                        raise ContextWindowError(manifest['request_tokens'], context_assembler.available,
                                                 ['system', 'schema', 'tools', 'tool_history'])
                for index, wire_message in enumerate(payload['messages']):
                    if wire_message.get('role') in {'system', 'user'}:
                        full_messages[index] = copy.deepcopy(wire_message)
                can_retry = attempt < self.max_attempts
                retry_delay = min(self.max_retry_delay, 2 ** min(attempt - 1, 30)) if can_retry else None
                request_record = {'url': self.base_url + '/chat/completions',
                                  'headers': {'Authorization': '[REDACTED]'},
                                  'json': copy.deepcopy(payload),
                                  'logical_exchange_id': logical_exchange_id,
                                  'attempt': attempt, 'tool_round': tool_rounds}
                if payload['messages'] != full_messages:
                    request_record['unprojected_messages'] = copy.deepcopy(full_messages)
                if tool_result_refs:
                    request_record['tool_result_refs'] = copy.deepcopy(tool_result_refs)
                exchange = None
                if on_attempt:
                    exchange = on_attempt(model, request_record, attempt)
                try:
                    response, raw = await self.adapter.request(client, self.base_url + '/chat/completions',
                        {'Authorization': 'Bearer ' + self.key}, payload,
                        on_delta=(lambda delta: on_delta(exchange, delta)) if on_delta else None)
                except IncompleteCompletion as failure:
                    usage = failure.body.get('usage')
                    if on_response:
                        on_response(exchange, {'http_status': failure.response.status_code,
                            'headers': dict(failure.response.headers), 'body': failure.body,
                            'stream_incomplete': True, 'logical_exchange_id': logical_exchange_id,
                            'tool_round': tool_rounds, 'attempt': attempt,
                            'messages': copy.deepcopy(payload['messages'])})
                    valid_usage = (isinstance(usage, dict) and type(usage.get('total_tokens')) is int
                                   and usage['total_tokens'] >= 0)
                    if valid_usage and on_usage:
                        on_usage(usage)
                    details = {'type': type(failure.cause).__name__, 'category': 'stream_incomplete',
                        'status': 'UNKNOWN_OPERATION', 'operation_status': 'UNKNOWN_OPERATION',
                        'request_status': 'response_received', 'billing_status': 'known' if valid_usage else 'unknown',
                        'requires_manual_review': True, 'retryable': False, 'will_retry': False,
                        'logical_exchange_id': logical_exchange_id, 'tool_round': tool_rounds, 'attempt': attempt,
                        'message': sanitize(str(failure.cause))}
                    if on_error:
                        on_error(exchange, details)
                    raise ModelError('模型流未完整结束，执行结果需要核查', status='UNKNOWN_OPERATION',
                                     category='stream_incomplete', details=details) from failure
                except httpx.RequestError as e:
                    request_sent = not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout,
                                                     httpx.PoolTimeout))
                    request_status = 'UNKNOWN_OPERATION' if request_sent else 'WAITING_NETWORK'
                    retryable = not request_sent
                    will_retry = retryable and can_retry
                    delay = retry_delay if will_retry else None
                    category = 'timeout' if isinstance(e, httpx.TimeoutException) else 'network'
                    request_error = {'type': type(e).__name__, 'category': category,
                                     'message': f'网络请求失败：{type(e).__name__}', 'retryable': retryable,
                                     'will_retry': will_retry,
                                     'retry_delay_seconds': delay,
                                     'original_message': sanitize(str(e)),
                                     'status': 'RETRYING' if will_retry else request_status,
                                     'operation_status': 'RETRYING' if will_retry else request_status,
                                     'billing_status': 'unknown' if request_sent else 'not_confirmed',
                                     'request_status': 'unknown' if request_sent else 'not_sent',
                                     'requires_manual_review': request_sent,
                                     'logical_exchange_id': logical_exchange_id,
                                     'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, request_error)
                    if will_retry:
                        await asyncio.sleep(delay)
                        continue
                    raise ModelError('模型连接失败；请求结果未知，请核对后处理' if request_sent else
                                     '模型连接失败；请求尚未发送，等待网络恢复',
                                     status=request_status, category=category,
                                     details=request_error) from e
                if on_response:
                    on_response(exchange, {'http_status': response.status_code,
                        'headers': dict(response.headers), 'body': raw,
                        'logical_exchange_id': logical_exchange_id, 'tool_round': tool_rounds,
                        'attempt': attempt,
                        'messages': copy.deepcopy(payload['messages'])})
                usage = raw.get('usage') if isinstance(raw, dict) else None
                valid_usage = (isinstance(usage, dict) and type(usage.get('total_tokens')) is int
                               and usage['total_tokens'] >= 0)
                if valid_usage and on_usage:
                    on_usage(usage)
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = _retry_after_seconds(response.headers)
                    raw_delay = (retry_after if retry_after is not None
                                 else 2 ** min(attempt - 1, 30))
                    delay = min(raw_delay, self.max_retry_delay)
                    category = 'rate_limited' if response.status_code == 429 else 'service_unavailable'
                    error = {'type': 'HTTPError', 'category': category,
                             'http_status': response.status_code, 'retryable': True,
                             'will_retry': can_retry, 'retry_after_seconds': retry_after,
                             'retry_delay_seconds': delay if can_retry else None,
                             'status': 'RETRYING' if can_retry else 'FAILED',
                             'operation_status': 'RETRYING' if can_retry else 'FAILED',
                             'billing_status': 'known' if valid_usage else 'unknown',
                             'request_status': 'response_received',
                             'requires_manual_review': False,
                             'logical_exchange_id': logical_exchange_id,
                             'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    if can_retry:
                        await asyncio.sleep(delay)
                        continue
                    raise ModelError(f'模型返回 HTTP {response.status_code}；{self.max_attempts} 次尝试已耗尽',
                                     category=category, details=error)
                if response.status_code != 200:
                    error = {'type': 'HTTPError', 'category': 'http_error',
                        'http_status': response.status_code, 'retryable': False,
                        'will_retry': False, 'status': 'FAILED', 'operation_status': 'FAILED',
                        'retry_delay_seconds': None,
                        'billing_status': 'known' if valid_usage else 'unknown',
                        'request_status': 'response_received',
                        'requires_manual_review': False,
                        'logical_exchange_id': logical_exchange_id,
                        'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    raise ModelError(f'模型返回 HTTP {response.status_code}；请检查接口地址、模型名称与凭证',
                                     status='FAILED', category='http_error', details=error)
                if not valid_usage:
                    error = {'type': 'ProtocolError', 'category': 'response_validation',
                             'message': 'usage.total_tokens 缺失或无效', 'retryable': False,
                             'will_retry': False, 'status': 'UNKNOWN_OPERATION',
                             'operation_status': 'UNKNOWN_OPERATION',
                             'billing_status': 'unknown', 'request_status': 'response_received',
                             'requires_manual_review': True,
                             'logical_exchange_id': logical_exchange_id,
                             'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    raise ModelError('服务方未返回有效的用量统计信息；计费用量未知',
                                     status='UNKNOWN_OPERATION', category='response_validation',
                                     details=error)
                try:
                    choice = raw['choices'][0]
                    if not isinstance(choice, dict):
                        raise TypeError('choices[0] 必须是对象')
                except (KeyError, IndexError, TypeError) as e:
                    error = {'type': 'ProtocolError', 'category': 'response_validation',
                             'message': '缺少 choices[0]', 'retryable': True,
                             'will_retry': can_retry, 'retry_delay_seconds': retry_delay,
                             'status': 'RETRYING' if can_retry else 'FAILED',
                             'operation_status': 'RETRYING' if can_retry else 'FAILED',
                             'billing_status': 'known', 'request_status': 'response_received',
                             'requires_manual_review': False,
                             'logical_exchange_id': logical_exchange_id,
                             'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    if not can_retry:
                        raise ModelError('模型响应缺少 choices；重试次数已耗尽',
                                         category='response_validation', details=error) from e
                    await asyncio.sleep(retry_delay)
                    continue
                message = choice.get('message')
                tool_calls = message.get('tool_calls') if isinstance(message, dict) else None
                if tool_calls is not None or choice.get('finish_reason') == 'tool_calls':
                    try:
                        if (not isinstance(message, dict) or choice.get('finish_reason') != 'tool_calls'
                                or message.get('refusal')):
                            raise ValueError('tool_calls 与 finish_reason/refusal 不一致')
                        calls = self._validated_calls(tool_calls, native_tools, completed_calls)
                        validated_tools = offered_registry.validate_batch(tool_calls, phase,
                                                                         wire_names=True)
                        if tool_rounds >= self.max_tool_rounds:
                            raise ValueError('模型工具调用轮次超过上限')
                        executor = tool_executor if tool_executor is not None else self.tool_executor
                        if executor is None and any(tool_pipeline is None or
                                not tool_pipeline.registry.contains(call.spec.name, phase) for call in validated_tools):
                            raise ValueError('模型请求执行工具，但未配置 tool_executor')
                    except (ValueError, TypeError) as exc:
                        error = {'type': 'ModelOutputError', 'category': 'tool_protocol',
                            'message': sanitize(str(exc))[:2000], 'retryable': False,
                            'will_retry': False, 'retry_delay_seconds': None,
                            'status': 'FAILED', 'operation_status': 'FAILED',
                            'billing_status': 'known', 'request_status': 'response_received',
                            'requires_manual_review': False, 'logical_exchange_id': logical_exchange_id,
                            'tool_round': tool_rounds, 'attempt': attempt}
                        if on_error:
                            on_error(exchange, error)
                        raise ModelOutputError('模型工具协议校验失败：' + error['message'],
                                               category='tool_protocol', details=error) from exc
                    assistant_message = self.adapter.assistant_message(message, tool_calls=True)
                    full_messages.append(assistant_message)
                    batch_results = [None] * len(calls)
                    batch_images = {}

                    async def execute_call(index):
                        # 已完成的 call 只复用原结果，避免网络重试造成重复副作用。
                        call_id, wire_name, arguments, identity = calls[index]
                        name = validated_tools[index].spec.name
                        reused = call_id in completed_calls
                        try:
                            if reused:
                                result_content = completed_calls[call_id][1]
                            elif tool_pipeline is not None and tool_pipeline.registry.contains(name, phase):
                                result = await tool_pipeline.execute(name, arguments, call_id)
                                result_content = result.to_content()
                                if result.images:
                                    batch_images[index] = result.images
                            else:
                                result = await self._call_tool_executor(executor, name, arguments, call_id)
                                result_content = self._tool_content(result)
                        except Exception as exc:
                            status = getattr(exc, 'status', 'UNKNOWN_OPERATION')
                            error = {**getattr(exc, 'details', {}), 'type': type(exc).__name__,
                                'category': getattr(exc, 'category', 'tool_execution'),
                                'message': sanitize(str(exc))[:2000], 'status': status,
                                'operation_status': status, 'retryable': False, 'will_retry': False,
                                'requires_manual_review': True, 'billing_status': 'known',
                                'request_status': 'response_received', 'tool_call_id': call_id,
                                'logical_exchange_id': logical_exchange_id,
                                'tool_round': tool_rounds, 'attempt': attempt}
                            if on_error:
                                on_error(exchange, error)
                            if hasattr(exc, 'status'):
                                raise
                            raise ModelError('工具执行结果未知：' + error['message'],
                                status='UNKNOWN_OPERATION', category='tool_execution', details=error) from exc
                        completed_calls[call_id] = (identity, result_content)
                        batch_results[index] = (result_content, reused)

                    position = 0
                    while position < len(calls):
                        spec = validated_tools[position].spec
                        if spec.parallel_safe and spec.side_effect in {'none', 'read'}:
                            end = position
                            while end < len(calls) and validated_tools[end].spec.parallel_safe and \
                                    validated_tools[end].spec.side_effect in {'none', 'read'}:
                                end += 1
                            try:
                                async with asyncio.TaskGroup() as group:
                                    for index in range(position, end):
                                        group.create_task(execute_call(index))
                            except ExceptionGroup as errors:
                                pending_errors = list(errors.exceptions)
                                flattened = []
                                while pending_errors:
                                    error = pending_errors.pop(0)
                                    if isinstance(error, ExceptionGroup):
                                        pending_errors.extend(error.exceptions)
                                    else:
                                        flattened.append(error)
                                selected = next((error for error in flattened if hasattr(error, 'status')),
                                                flattened[0])
                                raise selected from errors
                            position = end
                        else:
                            await execute_call(position)
                            position += 1
                    for index, (call_id, name, arguments, identity) in enumerate(calls):
                        result_content, reused = batch_results[index]
                        tool_message = {'role': 'tool', 'tool_call_id': call_id,
                                        'name': name, 'content': result_content}
                        full_messages.append(tool_message)
                        if on_tool_result:
                            callback_result = on_tool_result(exchange, {'logical_exchange_id': logical_exchange_id,
                                'tool_round': tool_rounds, 'attempt': attempt,
                                'message': copy.deepcopy(tool_message), 'reused': reused})
                            if (isinstance(callback_result, dict) and callback_result.get('result_ref')
                                    and callback_result.get('expandable') is True
                                    and callback_result.get('content_hash') == digest(result_content)):
                                tool_result_refs[call_id] = copy.deepcopy(callback_result)
                    for images in batch_images.values():
                        if self.vision_model:
                            payload['model'] = self.vision_model
                            full_messages.append({'role': 'user', 'content': images})
                        else:
                            full_messages.append({'role': 'user', 'content':
                                'Read 返回了图片，但当前未配置 vision_model，无法分析图像内容。'})
                    if context.get('execution_mode') == 'batch':
                        for index, call in enumerate(validated_tools):
                            if not call.spec.submission:
                                continue
                            try:
                                rejected = json.loads(batch_results[index][0])
                            except (ValueError, TypeError):
                                continue
                            if (not isinstance(rejected, dict)
                                    or rejected.get('isError', rejected.get('is_error')) is not True
                                    or rejected.get('executed') is not False):
                                continue
                            rejection = rejected.get('error') or {}
                            detail = sanitize(str(rejection.get('message') or '阶段提交被运行时拒绝'))[:2000]
                            error = {'type': 'ModelOutputError', 'category': 'output_validation',
                                'message': detail, 'retryable': True, 'will_retry': True,
                                'retry_delay_seconds': None, 'status': 'FAILED',
                                'operation_status': 'FAILED', 'billing_status': 'known',
                                'request_status': 'response_received', 'requires_manual_review': False,
                                'executed': False, 'submission_rejected': True,
                                'tool_name': call.name, 'tool_call_id': call.call_id,
                                'logical_exchange_id': logical_exchange_id,
                                'tool_round': tool_rounds, 'attempt': attempt,
                                'rejected_submission': call.arguments}
                            if on_error:
                                on_error(exchange, error)
                            raise ModelOutputError('阶段提交未通过校验；需补充诊断上下文后重试：' + detail,
                                                   category='output_validation', details=error)
                    tool_rounds += 1
                    if tool_pipeline is not None and tool_pipeline.submission_value is not None:
                        value = schema.model_validate(tool_pipeline.submission_value.model_dump(mode='json'))
                        if validate_output:
                            validate_output(value)
                        return ModelResult(value, usage, raw.get('model', model), 'tool_calls')
                    attempt = 0
                    continue
                refusal = isinstance(message, dict) and bool(message.get('refusal'))
                if choice.get('finish_reason') != 'stop' or refusal:
                    retryable = choice.get('finish_reason') == 'length' and not refusal
                    will_retry = retryable and can_retry
                    error = {'type': 'ModelError', 'category': 'incomplete_output',
                             'status': 'RETRYING' if will_retry else 'FAILED',
                             'operation_status': 'RETRYING' if will_retry else 'FAILED',
                             'billing_status': 'known', 'retryable': retryable,
                             'will_retry': will_retry,
                             'retry_delay_seconds': retry_delay if will_retry else None,
                             'request_status': 'response_received', 'requires_manual_review': False,
                             'finish_reason': choice.get('finish_reason'),
                             'refusal': refusal,
                             'logical_exchange_id': logical_exchange_id,
                             'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    if not will_retry:
                        raise ModelError(f"模型输出不完整：{choice.get('finish_reason')}",
                                         category='incomplete_output', details=error)
                    await asyncio.sleep(retry_delay)
                    continue
                try:
                    value = schema.model_validate_json(choice['message']['content'])
                    if browser_tools:
                        action = getattr(value, 'action', value)
                        if action.kind != 'finish':
                            raise ValueError('native 模式必须通过 tools 执行动作；最终 JSON 只能使用 finish')
                    if validate_output:
                        validate_output(value)
                except (ValueError, KeyError, TypeError) as e:
                    if isinstance(e, ValidationError):
                        detail = json.dumps(validation_details(e), ensure_ascii=False)
                    else:
                        detail = error_message(e)
                    detail = sanitize(detail)[:2000]
                    error = {'type': 'ModelOutputError', 'stage': 'output_validation',
                             'category': 'output_validation', 'message': detail, 'retryable': True,
                             'will_retry': can_retry, 'retry_delay_seconds': retry_delay,
                             'status': 'RETRYING' if can_retry else 'FAILED',
                             'operation_status': 'RETRYING' if can_retry else 'FAILED',
                             'billing_status': 'known', 'request_status': 'response_received',
                             'requires_manual_review': False,
                             'logical_exchange_id': logical_exchange_id,
                             'tool_round': tool_rounds, 'attempt': attempt}
                    if on_error:
                        on_error(exchange, error)
                    if not can_retry:
                        raise ModelOutputError(f'模型输出规范校验失败，{self.max_attempts} 次尝试已耗尽；未执行无效输出：'+detail,
                                               category='output_validation', details=error) from e
                    if isinstance(message, dict) and isinstance(message.get('content'), str):
                        rejected_message = self.adapter.assistant_message(message)
                        full_messages.append(rejected_message)
                    full_messages.append({'role': 'user', 'content':
                        '上一次最终 JSON 未通过校验，其中的动作未被执行。已有工具结果仍然有效，不得重复执行。'
                        '请依据原始目标、最新工具结果、页面观测和 schema 重新输出完整 JSON。'
                        '不得改变目标或放宽断言来消除错误。具体校验错误：'+detail})
                    await asyncio.sleep(retry_delay)
                    continue
                return ModelResult(value, usage, raw.get('model', model), choice['finish_reason'])

    @staticmethod
    def delegation_tools():
        from tracefix.workers.tools import supervisor_tools
        return supervisor_tools()


class BrowserPolicyRouter:
    """Only the browser node may use a student; fallback is explicit and counted."""
    def __init__(self, teacher, student=None):
        self.teacher, self.student = teacher, student

    @property
    def supports_tool_executor(self):
        return getattr(self.teacher, 'supports_tool_executor', False)

    @property
    def supports_context_assembler(self):
        return getattr(self.teacher, 'supports_context_assembler', False)

    @property
    def supports_tool_history_projection(self):
        return any(getattr(model, 'supports_tool_history_projection', False)
                   for model in (self.teacher, self.student))

    @property
    def vision_model(self):
        return getattr(self.teacher, 'vision_model', None)

    @property
    def supports_streaming(self):
        return any(getattr(model, 'supports_streaming', False) for model in (self.teacher, self.student))

    @staticmethod
    def _generation_kwargs(model, kwargs):
        adapted = dict(kwargs)
        if not getattr(model, 'supports_tool_history_projection', False):
            adapted.pop('tool_result_refs', None)
        if not getattr(model, 'supports_streaming', False):
            adapted.pop('on_delta', None)
        return adapted

    async def generate(self, schema, context, **kwargs):
        from tracefix.runtime.contracts import Decision, BrowserAction
        if schema is Decision and self.student and getattr(self.student, 'tool_mode', None) == 'json':
            try:
                result = await self.student.generate(BrowserAction, context,
                    **self._generation_kwargs(self.student, kwargs))
                action = result.value
                if action.locator:
                    from tracefix.execution.browser import resolve_locator
                    obs = context['observation']
                    try:
                        stale = (action.observation_id != obs['id']
                                 or action.element_ref != resolve_locator(obs['snapshot'], action.locator))
                    except ValueError as e:
                        # 定位器零匹配或多匹配同样是 student 输出不可用，与 schema 校验失败一样回退教师模型。
                        raise ModelOutputError('学生模型定位器无法唯一匹配元素：'+str(e)) from e
                    if stale:
                        raise ModelError('学生模型引用的元素已过期或无法解析')
                result.value = Decision(action=action, summary='学生模型浏览器动作策略')
                return result
            except ModelError as error:
                if error.status in {'UNKNOWN_OPERATION', 'WAITING_NETWORK'}:
                    raise
                # Let the host log the externally checkable reason, never confidence.
                context = {**context, 'student_fallback_reason': str(error)}
        return await self.teacher.generate(schema, context,
            **self._generation_kwargs(self.teacher, kwargs))
