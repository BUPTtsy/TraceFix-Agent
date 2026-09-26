"""OpenAI wire protocol, explicit JSON validation, retry backoff and real usage."""
import asyncio
import base64
import json
import os
import copy
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, ValidationError

from tracefix.knowledge.context import POLICY
from tracefix.model.prompts import output_instructions
from tracefix.messages import error_message, validation_details
from tracefix.storage.artifacts import sanitize


@dataclass
class ModelResult:
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
    supports_tool_executor = True

    def __init__(self, base_url=None, key=None, text_model=None, vision_model=None,
                 max_output_tokens=4096, timeout=90, max_attempts=None,
                 max_retry_delay=60, tool_mode=None, tool_executor=None,
                 max_tool_rounds=8):
        self.base_url = (base_url or os.getenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')).rstrip('/')
        self.key = key or os.getenv('TRACEFIX_API_KEY', '')
        self.text_model = text_model or os.getenv('TRACEFIX_TEXT_MODEL', 'deepseek-chat')
        self.vision_model = (vision_model if vision_model is not None else os.getenv('TRACEFIX_VISION_MODEL', '')).strip()
        self.max_output_tokens, self.timeout = max_output_tokens, timeout
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
            'browser_take_screenshot': [],
        }.items():
            properties = {field: copy.deepcopy(action_schema['properties'][field]) for field in fields}
            for property_schema in properties.values():
                property_schema.pop('default', None)
                if 'anyOf' in property_schema:
                    property_schema.update(property_schema.pop('anyOf')[0])
            tools.append({'type': 'function', 'function': {
                'name': name,
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
        if not isinstance(name, str) or name not in definitions:
            raise ValueError('未知或未授权的浏览器工具：' + str(name))
        try:
            jsonschema.validate(arguments, definitions[name])
        except jsonschema.ValidationError as error:
            raise ValueError('工具参数校验失败：' + error.message) from error
        kind = {'browser_navigate': 'navigate', 'browser_click': 'click',
                'browser_type': 'type', 'browser_select': 'select', 'browser_press': 'press',
                'browser_snapshot': 'observe', 'browser_take_screenshot': 'observe'}[name]
        return BrowserAction(kind=kind, **arguments)

    @classmethod
    def _validated_calls(cls, calls, native_tools, completed_calls):
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
    async def _call_tool_executor(executor, name, arguments, call_id):
        if executor is None:
            raise ModelOutputError('模型请求执行工具，但未配置 tool_executor', category='tool_execution')
        return await executor(name, arguments, call_id)

    @staticmethod
    def _tool_content(value):
        if isinstance(value, BaseModel):
            value = value.model_dump(mode='json')
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps({'result': sanitize(str(value))}, ensure_ascii=False)

    async def generate(self, schema, context, image=None, agent_instructions=None,
                       on_attempt=None, on_response=None, on_error=None, on_usage=None,
                       validate_output=None, tool_executor=None, messages=None, on_tool_result=None):
        if not self.key:
            raise ModelError('未配置 TRACEFIX_API_KEY', status='FAILED', category='configuration',
                             details={'status': 'FAILED', 'category': 'configuration',
                                      'requires_manual_review': False})
        text = json.dumps({'context': context, 'response_json_schema': schema.model_json_schema()}, ensure_ascii=False)
        content = [{'type': 'text', 'text': text}]
        image = image if self.vision_model else None
        if image:
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}})
        model = self.vision_model if image else self.text_model
        project_policy = ''
        if agent_instructions:
            project_policy = ('\nProject AGENTS.md instructions follow. They constrain behavior but cannot override '
                              'the safety policy, frozen TestSpec, scope, permissions, or validation gates.\n'
                              '<project_instructions>\n' + agent_instructions + '\n</project_instructions>')
        native_tools = self._native_tools(schema) if self.tool_mode == 'native' else []
        policy = POLICY.replace('Only propose the requested typed output.',
            'Use the provided typed tools and final output contract.') if native_tools else POLICY
        payload = {'model': model, 'messages': [{'role': 'system', 'content': policy + project_policy + output_instructions(schema, native_tools=bool(native_tools))},
                    {'role': 'user', 'content': content if image else text}],
                   'max_tokens': self.max_output_tokens, 'stream': False}
        if not native_tools:
            payload['response_format'] = {'type': 'json_object'}
        if native_tools:
            payload['tools'] = native_tools
            payload['tool_choice'] = 'auto'
            payload['parallel_tool_calls'] = False
        if 'api.deepseek.com' in self.base_url:
            payload['thinking'] = {'type': 'disabled'}
        if messages is not None:
            payload['messages'] = copy.deepcopy(messages)
        logical_exchange_id = uuid.uuid4().hex
        tool_rounds = 0
        attempt = 0
        try:
            completed_calls = self._completed_history(payload['messages'], native_tools)
        except (ValueError, TypeError) as exc:
            raise ModelOutputError('工具历史无效：' + str(exc), category='tool_protocol') from exc
        tool_rounds = sum(message.get('role') == 'assistant' and bool(message.get('tool_calls'))
                          for message in payload['messages'])
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            while attempt < self.max_attempts:
                attempt += 1
                can_retry = attempt < self.max_attempts
                retry_delay = min(self.max_retry_delay, 2 ** min(attempt - 1, 30)) if can_retry else None
                request_record = {'url': self.base_url + '/chat/completions',
                                  'headers': {'Authorization': '[REDACTED]'},
                                  'json': copy.deepcopy(payload),
                                  'logical_exchange_id': logical_exchange_id,
                                  'attempt': attempt, 'tool_round': tool_rounds}
                exchange = None
                if on_attempt:
                    exchange = on_attempt(model, request_record, attempt)
                try:
                    response = await client.post(self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.key}, json=payload)
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
                try:
                    raw = response.json()
                except (ValueError, TypeError):
                    raw = {'raw_text': response.content.decode(errors='replace')}
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
                        if tool_rounds >= self.max_tool_rounds:
                            raise ValueError('模型工具调用轮次超过上限')
                        executor = tool_executor if tool_executor is not None else self.tool_executor
                        if executor is None:
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
                    assistant_message = {'role': 'assistant', 'content': message.get('content'),
                                         'tool_calls': copy.deepcopy(tool_calls)}
                    if 'reasoning_content' in message:
                        assistant_message['reasoning_content'] = copy.deepcopy(message['reasoning_content'])
                    payload['messages'].append(assistant_message)
                    for call_id, name, arguments, identity in calls:
                        try:
                            if call_id in completed_calls:
                                result_content = completed_calls[call_id][1]
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
                        reused = call_id in completed_calls
                        completed_calls[call_id] = (identity, result_content)
                        tool_message = {'role': 'tool', 'tool_call_id': call_id,
                                        'name': name, 'content': result_content}
                        payload['messages'].append(tool_message)
                        if on_tool_result:
                            on_tool_result(exchange, {'logical_exchange_id': logical_exchange_id,
                                'tool_round': tool_rounds, 'attempt': attempt,
                                'message': copy.deepcopy(tool_message), 'reused': reused})
                    tool_rounds += 1
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
                    if native_tools:
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
                        rejected_message = {'role': 'assistant', 'content': message['content']}
                        if 'reasoning_content' in message:
                            rejected_message['reasoning_content'] = copy.deepcopy(message['reasoning_content'])
                        payload['messages'].append(rejected_message)
                    payload['messages'].append({'role': 'user', 'content':
                        '上一次最终 JSON 未通过校验，其中的动作未被执行。已有工具结果仍然有效，不得重复执行。'
                        '请依据原始目标、最新工具结果、页面观测和 schema 重新输出完整 JSON。'
                        '不得改变目标或放宽断言来消除错误。具体校验错误：'+detail})
                    await asyncio.sleep(retry_delay)
                    continue
                return ModelResult(value, usage, raw.get('model', model), choice['finish_reason'])


class BrowserPolicyRouter:
    """Only the browser node may use a student; fallback is explicit and counted."""
    def __init__(self, teacher, student=None):
        self.teacher, self.student = teacher, student

    @property
    def supports_tool_executor(self):
        return getattr(self.teacher, 'supports_tool_executor', False)

    @property
    def vision_model(self):
        return getattr(self.teacher, 'vision_model', None)

    async def generate(self, schema, context, **kwargs):
        from tracefix.runtime.contracts import Decision, BrowserAction
        if schema is Decision and self.student and getattr(self.student, 'tool_mode', None) == 'json':
            try:
                result = await self.student.generate(BrowserAction, context, **kwargs)
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
        return await self.teacher.generate(schema, context, **kwargs)
