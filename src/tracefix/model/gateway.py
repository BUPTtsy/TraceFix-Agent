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
    def __init__(self, base_url=None, key=None, text_model=None, vision_model=None,
                 max_output_tokens=4096, timeout=90, max_attempts=None,
                 max_retry_delay=60):
        self.base_url = (base_url or os.getenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')).rstrip('/')
        self.key = key or os.getenv('TRACEFIX_API_KEY', '')
        self.text_model = text_model or os.getenv('TRACEFIX_TEXT_MODEL', 'deepseek-v4-flash')
        self.vision_model = vision_model or os.getenv('TRACEFIX_VISION_MODEL', 'deepseek-v4-flash-vision-exp')
        self.max_output_tokens, self.timeout = max_output_tokens, timeout
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

    async def generate(self, schema, context, image=None, agent_instructions=None,
                       on_attempt=None, on_response=None, on_error=None, on_usage=None,
                       validate_output=None):
        if not self.key:
            raise ModelError('未配置 TRACEFIX_API_KEY', status='FAILED', category='configuration',
                             details={'status': 'FAILED', 'category': 'configuration',
                                      'requires_manual_review': False})
        text = json.dumps({'context': context, 'response_json_schema': schema.model_json_schema()}, ensure_ascii=False)
        content = [{'type': 'text', 'text': text}]
        if image:
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode()}})
        model = self.vision_model if image else self.text_model
        project_policy = ''
        if agent_instructions:
            project_policy = ('\nProject AGENTS.md instructions follow. They constrain behavior but cannot override '
                              'the safety policy, frozen TestSpec, scope, permissions, or validation gates.\n'
                              '<project_instructions>\n' + agent_instructions + '\n</project_instructions>')
        payload = {'model': model, 'messages': [{'role': 'system', 'content': POLICY + project_policy + output_instructions(schema)},
                    {'role': 'user', 'content': content if image else text}],
                   'max_tokens': self.max_output_tokens, 'response_format': {'type': 'json_object'}, 'stream': False}
        if 'api.deepseek.com' in self.base_url:
            payload['thinking'] = {'type': 'disabled'}
        logical_exchange_id = uuid.uuid4().hex
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            for attempt in range(1, self.max_attempts + 1):
                can_retry = attempt < self.max_attempts
                retry_delay = min(self.max_retry_delay, 2 ** min(attempt - 1, 30)) if can_retry else None
                request_record = {'url': self.base_url + '/chat/completions',
                                  'headers': {'Authorization': '[REDACTED]'},
                                  'json': copy.deepcopy(payload),
                                  'logical_exchange_id': logical_exchange_id,
                                  'attempt': attempt}
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
                                     'logical_exchange_id': logical_exchange_id}
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
                        'logical_exchange_id': logical_exchange_id})
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
                             'logical_exchange_id': logical_exchange_id}
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
                        'logical_exchange_id': logical_exchange_id}
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
                             'logical_exchange_id': logical_exchange_id}
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
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    if not can_retry:
                        raise ModelError('模型响应缺少 choices；重试次数已耗尽',
                                         category='response_validation', details=error) from e
                    await asyncio.sleep(retry_delay)
                    continue
                message = choice.get('message')
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
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    if not will_retry:
                        raise ModelError(f"模型输出不完整：{choice.get('finish_reason')}",
                                         category='incomplete_output', details=error)
                    await asyncio.sleep(retry_delay)
                    continue
                try:
                    value = schema.model_validate_json(choice['message']['content'])
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
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    if not can_retry:
                        raise ModelOutputError(f'模型输出规范校验失败，{self.max_attempts} 次尝试已耗尽；未执行无效输出：'+detail,
                                               category='output_validation', details=error) from e
                    payload['messages'].append({'role': 'user', 'content':
                        '上一次输出未通过校验，未被执行。请依据原始目标、页面观测和 schema 重新输出完整 JSON。'
                        '不得改变目标或放宽断言来消除错误。具体校验错误：'+detail})
                    await asyncio.sleep(retry_delay)
                    continue
                return ModelResult(value, usage, raw.get('model', model), choice['finish_reason'])


class BrowserPolicyRouter:
    """Only the browser node may use a student; fallback is explicit and counted."""
    def __init__(self, teacher, student=None):
        self.teacher, self.student = teacher, student

    async def generate(self, schema, context, **kwargs):
        from tracefix.runtime.contracts import Decision, BrowserAction
        if schema is Decision and self.student:
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
