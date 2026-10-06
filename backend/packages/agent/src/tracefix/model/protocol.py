"""Request auditing and strict wire validation around the framework provider."""
import asyncio
import copy
import inspect
import json
from uuid import uuid4

import httpx

from tracefix.model.contracts import ModelError, ModelOutputError
from tracefix.model.prompts import serialize_request, system_instructions
from tracefix.model.streaming import CompletionStream, StreamProtocolError
from tracefix.runtime.contracts import digest


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


def causes(error):
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


class RequestBoundary:
    def __init__(self, gateway, schema, context, registry, phase, options):
        self.gateway, self.schema, self.context = gateway, schema, context
        self.registry, self.phase, self.options = registry, phase, options
        self.logical_id = uuid4().hex
        self.request_count = 0
        self.tool_round = 0
        self.exchange = None
        self.requests, self.responses, self.tool_records = [], [], []
        self.completed = {}
        self.result_refs = copy.deepcopy(options.get('tool_result_refs') or {})
        self.usage = {}
        self.raw_usage = []
        self.active_stream = None
        self.last_body = {}
        self.last_history = []
        self.reported_errors = set()

    async def callback(self, name, *arguments):
        function = self.options.get(name)
        return await resolve(function(*arguments)) if function else None

    async def before(self, request):
        payload = json.loads(request.content)
        preserve = self.options.get('preserve_resumed_request') and self.request_count == 0
        provider = self.options.get('context_provider')
        if provider and not preserve:
            self.context = await resolve(provider())
        if self.schema is not str and not preserve:
            system = system_instructions(self.schema, self.context,
                agent_instructions=self.options.get('agent_instructions'),
                native_tools=bool(self.registry.specs))
            if any(spec.name.startswith('agent.') for spec in self.registry.specs):
                from tracefix.workers.prompt_policy import SUPERVISOR_DELEGATION_POLICY
                policy = SUPERVISOR_DELEGATION_POLICY
                for spec in self.registry.specs:
                    policy = policy.replace(spec.name, spec.wire_name)
                system += '\n<supervisor_tool_protocol>\n' + policy + '\n</supervisor_tool_protocol>'
            systems = [message for message in payload['messages'] if message['role'] == 'system']
            if systems:
                systems[0]['content'] = system
            self.gateway._replace_context(payload['messages'], serialize_request(self.schema, self.context))
        if self.gateway.vision_model and any(isinstance(message.get('content'), list)
                and any(part.get('type') == 'image_url' for part in message['content'])
                for message in payload['messages']):
            payload['model'] = self.gateway.vision_model
        for message in payload['messages']:
            if message.get('role') != 'assistant':
                continue
            ids = {call['id'] for call in message.get('tool_calls', [])}
            for raw in reversed(self.responses):
                for choice in raw.get('choices', []):
                    assistant = choice.get('message', {})
                    raw_ids = {call['id'] for call in assistant.get('tool_calls', [])}
                    if ids and ids == raw_ids:
                        for field in ('reasoning', 'reasoning_content'):
                            if field in assistant:
                                message[field] = copy.deepcopy(assistant[field])
                        break
        self.last_history = copy.deepcopy(payload['messages'])
        assembler = self.options.get('context_assembler')
        if assembler is not None:
            manifest, compacted = self.gateway._budget_payload(payload, self.schema,
                self.context, assembler, self.result_refs,
                self.registry.contains('context.expand', self.phase), preserve=preserve)
            await self.callback('on_context', manifest, compacted)
            if manifest['request_tokens'] > assembler.available:
                from tracefix.knowledge.assembler import ContextWindowError
                raise ContextWindowError(manifest['request_tokens'], assembler.available,
                                         ['system', 'schema', 'tools', 'tool_history'])
        self.request_count += 1
        record = {'url': str(request.url), 'headers': {'Authorization': '[REDACTED]'},
                  'json': copy.deepcopy(payload), 'logical_exchange_id': self.logical_id,
                  'attempt': self.request_count, 'tool_round': self.tool_round,
                  'unprojected_messages': self.last_history,
                  'tool_result_refs': copy.deepcopy(self.result_refs)}
        self.requests.append(record)
        self.exchange = await self.callback('on_attempt', payload['model'], record, self.request_count)
        content = json.dumps(payload, ensure_ascii=False).encode()
        request._content = content
        request.stream = httpx.ByteStream(content)
        request.headers['content-length'] = str(len(content))

    async def received(self, response):
        if response.is_success and json.loads(response.request.content).get('stream'):
            if 'text/event-stream' not in response.headers.get('content-type', '').lower():
                raise await self.fail('stream_incomplete', '模型流 Content-Type 无效',
                                     status='UNKNOWN_OPERATION')
            stream = AuditedStream(response.stream, response, self)
            response.stream = stream
            self.active_stream = stream
        else:
            await response.aread()
            try:
                body = response.json()
            except ValueError:
                body = {'raw_text': response.text}
            await self.finish_response(response, body, validate=response.is_success)

    async def finish_response(self, response, body, *, complete=True, validate=True):
        self.last_body = body
        self.responses.append(copy.deepcopy(body))
        usage = body.get('usage')
        if isinstance(usage, dict):
            usage = copy.deepcopy(usage)
            if 'total_tokens' not in usage and all(type(usage.get(key)) is int
                                                   for key in ('prompt_tokens', 'completion_tokens')):
                usage['total_tokens'] = usage['prompt_tokens'] + usage['completion_tokens']
            if type(usage.get('total_tokens')) is int and usage['total_tokens'] >= 0:
                self.raw_usage.append(usage)
                for key, value in usage.items():
                    if type(value) is int:
                        self.usage[key] = self.usage.get(key, 0) + value
                await self.callback('on_usage', usage)
        await self.callback('on_response', self.exchange, {
            'http_status': response.status_code, 'headers': dict(response.headers),
            'body': copy.deepcopy(body), 'stream_incomplete': not complete,
            'logical_exchange_id': self.logical_id, 'attempt': self.request_count,
            'tool_round': self.tool_round, 'messages': copy.deepcopy(self.last_history)})
        if complete and validate:
            choices = body.get('choices')
            if not isinstance(choices, list) or len(choices) != 1:
                raise await self.fail('malformed_response', '模型响应必须包含一个完整 choice')
            choice = choices[0]
            message = choice.get('message')
            if not isinstance(message, dict):
                raise await self.fail('malformed_response', '模型响应缺少完整消息')
            calls = message.get('tool_calls')
            if calls:
                if choice.get('finish_reason') != 'tool_calls':
                    raise await self.fail('tool_protocol', '工具响应缺少成功终止原因')
                output_names = set(self.options.get('framework_output_tool_names') or ())
                output_calls = [call for call in calls
                                if isinstance(call, dict)
                                and isinstance(call.get('function'), dict)
                                and call['function'].get('name') in output_names]
                runtime_calls = [call for call in calls if call not in output_calls]
                if output_calls and runtime_calls:
                    raise await self.fail('tool_protocol', '结构化输出不能与运行时工具混合')
                if runtime_calls:
                    if self.tool_round >= self.gateway.max_tool_rounds:
                        raise await self.fail('tool_protocol', '模型工具轮次已达到上限')
                    try:
                        self.registry.validate_batch(runtime_calls, self.phase, self.completed,
                                                     wire_names=True)
                    except (ValueError, TypeError) as error:
                        raise await self.fail('tool_protocol', str(error), error)
                    self.tool_round += 1
            elif choice.get('finish_reason') != 'stop' or message.get('refusal'):
                raise await self.fail('incomplete_output', '模型响应未正常结束',
                                     finish_reason=choice.get('finish_reason'))
            elif self.schema is str and not (message.get('content') or '').strip():
                raise await self.fail('output_validation', '对话模型未返回正文')

    async def fail(self, category, message, cause=None, *, status='FAILED', **details):
        selected = next((error for error in causes(cause)
                         if getattr(error, 'status', None) == 'UNKNOWN_OPERATION'), cause) if cause else None
        inherited = dict(getattr(selected, 'details', {}) or {})
        status = getattr(selected, 'status', status)
        raw = {**inherited, **details, 'category': category, 'status': status,
               'operation_status': status, 'message': message, 'retryable': False, 'will_retry': False,
               'requires_manual_review': status == 'UNKNOWN_OPERATION',
               'logical_exchange_id': self.logical_id, 'attempt': self.request_count,
               'tool_round': self.tool_round, 'message_history': copy.deepcopy(self.last_history),
               'tool_results': copy.deepcopy(self.tool_records),
               'usage': copy.deepcopy(self.usage), 'raw_usage': copy.deepcopy(self.raw_usage)}
        if 'request_status' not in raw:
            raw['request_status'] = 'response_received' if self.responses else 'unknown'
        raw['billing_status'] = 'known' if self.raw_usage else 'unknown'
        error_type = ModelOutputError if category in {'output_validation', 'tool_protocol'} else ModelError
        error = error_type(message, category=category, status=status, details=raw)
        await self.report(error)
        return error

    async def report(self, error):
        if id(error) not in self.reported_errors:
            self.reported_errors.add(id(error))
            await self.callback('on_error', self.exchange, error.details)


class AuditedStream(httpx.AsyncByteStream):
    def __init__(self, source, response, boundary):
        self.source, self.response, self.boundary = source, response, boundary
        self.iterator = source.__aiter__()
        self.deltas = []
        self.completion = CompletionStream(on_delta=self.deltas.append)
        self.finished = False
        self.closed = False
        self.failure = None

    async def __aiter__(self):
        try:
            async for chunk in self.iterator:
                self.completion.feed(chunk)
                for delta in self.deltas:
                    await self.boundary.callback('on_delta', self.boundary.exchange, delta)
                self.deltas.clear()
                yield chunk
            await self.finalize()
        except asyncio.CancelledError:
            await self.boundary.finish_response(self.response, self.completion.result(),
                                                complete=False, validate=False)
            raise
        except Exception as error:
            self.failure = error
            if isinstance(error, ModelError):
                raise
            await self.boundary.finish_response(self.response, self.completion.result(),
                                                complete=False, validate=False)
            raise await self.boundary.fail('stream_incomplete', '模型流未完整结束，响应未完成',
                                          error, status='UNKNOWN_OPERATION') from error

    async def finalize(self):
        if not self.finished:
            self.finished = True
            body = self.completion.finish()
            await self.boundary.finish_response(self.response, body)

    async def aclose(self):
        if not self.closed:
            self.closed = True
            await self.source.aclose()


def create_http_client(boundary):
    return httpx.AsyncClient(timeout=boundary.gateway.timeout, follow_redirects=False,
        event_hooks={'request': [boundary.before], 'response': [boundary.received]})
