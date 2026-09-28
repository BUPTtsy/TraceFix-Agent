"""Incremental chat completion events and their final response envelope."""
import copy
import json


class StreamProtocolError(ValueError):
    pass


class CompletionStream:
    def __init__(self, on_delta=None):
        self.on_delta = on_delta
        self.message = {'role': 'assistant', 'content': None}
        self.tool_calls = {}
        self.metadata = {}
        self.usage = None
        self.finish_reason = None
        self.done = False

    def result(self):
        message = copy.deepcopy(self.message)
        if self.tool_calls:
            message['tool_calls'] = [copy.deepcopy(self.tool_calls[index])
                                     for index in sorted(self.tool_calls)]
        result = {**self.metadata, 'choices': [
            {'index': 0, 'message': message, 'finish_reason': self.finish_reason}]}
        if self.usage is not None:
            result['usage'] = copy.deepcopy(self.usage)
        return result

    def _text(self, target, field, value, channel=None):
        if value is None:
            return
        if not isinstance(value, str):
            raise StreamProtocolError(field + ' 增量必须为字符串')
        target[field] = (target.get(field) or '') + value
        if value and channel and self.on_delta:
            self.on_delta({'channel': channel, 'delta': value})

    def _tools(self, calls):
        if calls is None:
            return
        if not isinstance(calls, list):
            raise StreamProtocolError('tool_calls 增量必须为数组')
        for fragment in calls:
            if not isinstance(fragment, dict):
                raise StreamProtocolError('tool_call 增量必须为对象')
            index = fragment.get('index')
            if type(index) is not int or index < 0:
                raise StreamProtocolError('tool_call 增量缺少有效 index')
            target = self.tool_calls.setdefault(index, {'function': {}})
            self._text(target, 'id', fragment.get('id'))
            if fragment.get('type') is not None:
                if fragment['type'] != 'function':
                    raise StreamProtocolError('tool_call 增量必须为 function 类型')
                target['type'] = fragment['type']
            function = fragment.get('function')
            if function is not None:
                if not isinstance(function, dict):
                    raise StreamProtocolError('function 增量必须为对象')
                self._text(target['function'], 'name', function.get('name'))
                self._text(target['function'], 'arguments', function.get('arguments'))

    def event(self, data):
        if data == '[DONE]':
            if self.finish_reason is None:
                raise StreamProtocolError('流结束时缺少 finish_reason')
            self.done = True
            return
        try:
            chunk = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise StreamProtocolError('流包含无效 JSON 事件') from exc
        if not isinstance(chunk, dict) or chunk.get('error') is not None:
            raise StreamProtocolError('流包含无效响应或服务方错误')
        for field in ('id', 'model', 'created', 'system_fingerprint'):
            if field in chunk:
                self.metadata[field] = chunk[field]
        if chunk.get('usage') is not None:
            if not isinstance(chunk['usage'], dict):
                raise StreamProtocolError('流的 usage 必须为对象')
            self.usage = chunk['usage']
        choices = chunk.get('choices')
        if not isinstance(choices, list):
            raise StreamProtocolError('流事件缺少 choices 数组')
        for choice in choices:
            if not isinstance(choice, dict) or choice.get('index') != 0:
                raise StreamProtocolError('流事件包含无效 choice index')
            delta = choice.get('delta')
            if not isinstance(delta, dict):
                raise StreamProtocolError('流事件缺少 delta 对象')
            if self.finish_reason is not None and any(value for value in delta.values()):
                raise StreamProtocolError('finish_reason 后出现新的输出增量')
            self._text(self.message, 'reasoning_content', delta.get('reasoning_content'), 'reasoning')
            self._text(self.message, 'content', delta.get('content'), 'content')
            self._text(self.message, 'refusal', delta.get('refusal'))
            self._tools(delta.get('tool_calls'))
            finish_reason = choice.get('finish_reason')
            if finish_reason is not None:
                if not isinstance(finish_reason, str) or not finish_reason:
                    raise StreamProtocolError('流包含无效 finish_reason')
                if self.finish_reason is not None and finish_reason != self.finish_reason:
                    raise StreamProtocolError('流包含冲突的 finish_reason')
                self.finish_reason = finish_reason

    async def read(self, response):
        if 'text/event-stream' not in response.headers.get('content-type', '').lower():
            raise StreamProtocolError('流响应的 Content-Type 必须为 text/event-stream')
        data_lines = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    self.event('\n'.join(data_lines))
                    data_lines = []
                    if self.done:
                        return self.result()
                continue
            if line.startswith('data:'):
                data_lines.append(line[5:].removeprefix(' '))
            elif not line.startswith((':', 'event:', 'id:', 'retry:')):
                raise StreamProtocolError('流包含无效 SSE 字段')
        if data_lines:
            self.event('\n'.join(data_lines))
        if not self.done:
            raise StreamProtocolError('流提前结束，缺少 [DONE]')
        return self.result()
