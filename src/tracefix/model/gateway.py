"""OpenAI wire protocol, explicit JSON validation, bounded retries and real usage."""
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
                 max_output_tokens=4096, timeout=90, max_attempts=3,
                 max_retry_delay=60):
        self.base_url = (base_url or os.getenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')).rstrip('/')
        self.key = key or os.getenv('TRACEFIX_API_KEY', '')
        self.text_model = text_model or os.getenv('TRACEFIX_TEXT_MODEL', 'deepseek-v4-flash')
        self.vision_model = vision_model or os.getenv('TRACEFIX_VISION_MODEL', 'deepseek-v4-flash-vision-exp')
        self.max_output_tokens, self.timeout = max_output_tokens, timeout
        self.max_attempts = max(1, int(max_attempts))
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
            for attempt in range(self.max_attempts):
                request_record = {'url': self.base_url + '/chat/completions',
                                  'headers': {'Authorization': '[REDACTED]'},
                                  'json': copy.deepcopy(payload),
                                  'logical_exchange_id': logical_exchange_id,
                                  'attempt': attempt + 1}
                exchange = None
                if on_attempt:
                    exchange = on_attempt(model, request_record, attempt + 1)
                try:
                    response = await client.post(self.base_url + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + self.key}, json=payload)
                except httpx.TimeoutException as e:
                    timeout_error = {'type': type(e).__name__, 'category': 'timeout',
                                     'message': f'模型请求超时：{type(e).__name__}', 'retryable': False, 'will_retry': False,
                                     'original_message': sanitize(str(e)),
                                     'status': 'UNKNOWN_OPERATION', 'operation_status': 'UNKNOWN_OPERATION',
                                     'billing_status': 'unknown', 'request_status': 'unknown',
                                     'requires_manual_review': True,
                                     'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, timeout_error)
                    # Unknown billed cost is an accounting failure, not a free retry.
                    raise ModelError('模型调用超时；计费用量未知，需要核对后处理',
                                     status='UNKNOWN_OPERATION', category='timeout',
                                     details=timeout_error) from e
                except httpx.RequestError as e:
                    request_sent = not isinstance(e, httpx.ConnectError)
                    request_status = 'UNKNOWN_OPERATION' if request_sent else 'WAITING_NETWORK'
                    request_error = {'type': type(e).__name__, 'category': 'network',
                                     'message': f'网络请求失败：{type(e).__name__}', 'retryable': False, 'will_retry': False,
                                     'original_message': sanitize(str(e)),
                                     'status': request_status, 'operation_status': request_status,
                                     'billing_status': 'unknown' if request_sent else 'not_confirmed',
                                     'request_status': 'unknown' if request_sent else 'not_sent',
                                     'requires_manual_review': request_sent,
                                     'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, request_error)
                    raise ModelError('模型连接失败；请求结果未知，请核对后处理' if request_sent else
                                     '模型连接失败；请求尚未发送，等待网络恢复',
                                     status=request_status, category='network',
                                     details=request_error) from e
                try:
                    raw = response.json()
                except (ValueError, TypeError):
                    raw = {'raw_text': response.content.decode(errors='replace')}
                if on_response:
                    on_response(exchange, {'http_status': response.status_code,
                        'headers': dict(response.headers), 'body': raw,
                        'logical_exchange_id': logical_exchange_id})
                if response.status_code in {429, 500, 502, 503, 504}:
                    retry_after = _retry_after_seconds(response.headers)
                    raw_delay = retry_after if retry_after is not None else min(2 ** attempt, 2)
                    delay = min(raw_delay, self.max_retry_delay)
                    will_retry = attempt + 1 < self.max_attempts
                    category = 'rate_limited' if response.status_code == 429 else 'service_unavailable'
                    error = {'type': 'HTTPError', 'category': category,
                             'http_status': response.status_code, 'retryable': will_retry,
                             'will_retry': will_retry, 'retry_after_seconds': retry_after,
                             'retry_delay_seconds': delay if will_retry else None,
                             'status': 'RETRYING' if will_retry else 'FAILED',
                             'operation_status': 'RETRYING' if will_retry else 'FAILED',
                             'billing_status': 'unknown', 'request_status': 'response_received',
                             'requires_manual_review': not will_retry,
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    if will_retry:
                        await asyncio.sleep(delay)
                        continue
                if response.status_code not in {200, 429, 500, 502, 503, 504} and on_error:
                    on_error(exchange, {'type': 'HTTPError', 'category': 'http_error',
                        'http_status': response.status_code, 'retryable': False,
                        'will_retry': False, 'status': 'FAILED', 'operation_status': 'FAILED',
                        'billing_status': 'unknown', 'request_status': 'response_received',
                        'requires_manual_review': False,
                        'logical_exchange_id': logical_exchange_id})
                if response.status_code != 200:
                    category = ('rate_limited' if response.status_code == 429 else
                                'service_unavailable' if response.status_code in {500, 502, 503, 504}
                                else 'http_error')
                    raise ModelError(f'模型返回 HTTP {response.status_code}；请检查接口地址、模型名称与凭证',
                                     status='FAILED', category=category,
                                     details={'http_status': response.status_code,
                                              'operation_status': 'FAILED',
                                              'billing_status': 'unknown',
                                              'logical_exchange_id': logical_exchange_id})
                usage = raw.get('usage') if isinstance(raw, dict) else None
                if (not isinstance(usage, dict) or type(usage.get('total_tokens')) is not int
                        or usage['total_tokens'] < 0):
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
                if on_usage:
                    on_usage(usage)
                try:
                    choice = raw['choices'][0]
                    if not isinstance(choice, dict):
                        raise TypeError('choices[0] 必须是对象')
                except (KeyError, IndexError, TypeError) as e:
                    error = {'type': 'ProtocolError', 'category': 'response_validation',
                             'message': '缺少 choices[0]', 'retryable': False,
                             'will_retry': False, 'status': 'FAILED', 'operation_status': 'FAILED',
                             'billing_status': 'known', 'request_status': 'response_received',
                             'requires_manual_review': False,
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    raise ModelError('模型响应缺少 choices', status='FAILED',
                                     category='response_validation', details=error) from e
                if choice.get('finish_reason') != 'stop':
                    error = {'type': 'ModelError', 'category': 'incomplete_output',
                             'status': 'FAILED', 'billing_status': 'known', 'retryable': False,
                             'finish_reason': choice.get('finish_reason'),
                             'logical_exchange_id': logical_exchange_id}
                    if on_error:
                        on_error(exchange, error)
                    raise ModelError(f"模型输出不完整：{choice.get('finish_reason')}",
                                     category='incomplete_output', details=error)
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
                    if on_error:
                        on_error(exchange, {'type': 'ModelOutputError', 'stage': 'output_validation',
                                            'message': detail, 'retryable': attempt + 1 < self.max_attempts,
                                            'will_retry': attempt + 1 < self.max_attempts,
                                            'status': 'RETRYING' if attempt + 1 < self.max_attempts else 'FAILED',
                                            'operation_status': 'RETRYING' if attempt + 1 < self.max_attempts else 'FAILED',
                                            'billing_status': 'known', 'request_status': 'response_received',
                                            'requires_manual_review': False,
                                            'logical_exchange_id': logical_exchange_id})
                    if attempt + 1 == self.max_attempts:
                        raise ModelOutputError(f'模型输出规范校验失败，{self.max_attempts} 次尝试已耗尽；未执行无效输出：'+detail) from e
                    payload['messages'].append({'role': 'user', 'content':
                        '上一次输出未通过校验，未被执行。请依据原始目标、页面观测和 schema 重新输出完整 JSON。'
                        '不得改变目标或放宽断言来消除错误。具体校验错误：'+detail})
                    continue
                return ModelResult(value, usage, raw.get('model', model), choice['finish_reason'])
        raise ModelError('重试预算已耗尽', status='FAILED', category='retry_budget',
                         details={'status': 'FAILED', 'category': 'retry_budget'})


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
