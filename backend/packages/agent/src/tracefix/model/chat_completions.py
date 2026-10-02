"""适配 Chat Completions 传输，工具权限由运行时策略单独处理。"""
import copy

from tracefix.model.streaming import CompletionStream


class IncompleteCompletion(RuntimeError):
    def __init__(self, response, body, cause):
        super().__init__(str(cause))
        self.response = response
        self.body = body
        self.cause = cause


class ChatCompletionsAdapter:
    # 隔离 HTTP/SSE 细节；工具授权由运行时负责。
    async def request(self, client, url, headers, payload, on_delta=None):
        # 非流式响应直接解析 JSON；流式响应统一交给 CompletionStream 校验。
        if not payload.get('stream'):
            response = await client.post(url, headers=headers, json=payload)
            try:
                body = response.json()
            except (ValueError, TypeError):
                body = {'raw_text': response.content.decode(errors='replace')}
            return response, body
        async with client.stream('POST', url, headers=headers, json=payload) as response:
            if response.status_code != 200:
                await response.aread()
                try:
                    body = response.json()
                except (ValueError, TypeError):
                    body = {'raw_text': response.content.decode(errors='replace')}
                return response, body
            completion = CompletionStream(on_delta=on_delta)
            try:
                body = await completion.read(response)
            except Exception as error:
                raise IncompleteCompletion(response, completion.result(), error) from error
            await response.aclose()
            underlying = getattr(response.stream, '_stream', None)
            if underlying is not None and hasattr(underlying, 'aclose'):
                await underlying.aclose()
            try:
                response.stream.closed = True
            except (AttributeError, TypeError):
                pass
            return response, body

    @staticmethod
    def assistant_message(message, *, tool_calls=False):
        # 续接工具历史时保留供应商 reasoning 字段，保证消息结构完整。
        result = {'role': 'assistant', 'content': message.get('content')}
        if tool_calls:
            result['tool_calls'] = copy.deepcopy(message['tool_calls'])
        for field in ('reasoning_content', 'reasoning'):
            if field in message:
                result[field] = copy.deepcopy(message[field])
        return result


def reasoning_records(body):
    # 兼容两个供应商字段，并按 choice 保留原结构，供独立 artifact 审计。
    records = []
    if not isinstance(body, dict) or not isinstance(body.get('choices'), list):
        return records
    for index, choice in enumerate(body['choices']):
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        fields = {field: copy.deepcopy(message[field]) for field in ('reasoning_content', 'reasoning')
                  if message.get(field) is not None}
        if fields:
            records.append({'choice_index': choice.get('index', index), **fields})
    return records
