import asyncio
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from tracefix.model.chat import stream_tool_chat
from tracefix.model import protocol
from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapterError
from tracefix.runtime.contracts import Phase
from tracefix.tools.core import ToolPipeline, ToolRegistry, ToolSpec


def sse(delta=None, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {},
        'finish_reason': finish}]}, ensure_ascii=False) + '\n\n').encode()


class Frames(httpx.AsyncByteStream):
    def __init__(self, values, before=None):
        self.values, self.before = values, before

    async def __aiter__(self):
        for index, value in enumerate(self.values):
            if self.before:
                await self.before(index)
            yield value


def reply(frames, before=None):
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                         stream=Frames(frames, before))


def pipeline(handler):
    spec = ToolSpec('Read', 'Read a public fixture',
                    {'type': 'object', 'properties': {}, 'additionalProperties': False})
    return ToolPipeline(ToolRegistry([spec]), {'Read': handler}, Phase.DIAGNOSE)


def transport(monkeypatch, responses):
    requests = []
    client_type = httpx.AsyncClient

    async def handle(request):
        requests.append(json.loads(request.content))
        return responses.pop(0)

    def client(boundary):
        return client_type(transport=protocol.BoundaryTransport(httpx.MockTransport(handle), boundary),
            event_hooks={'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    monkeypatch.setenv('TRACEFIX_API_KEY', 'fixture-key')
    return requests


async def test_chat_streams_before_finish_and_keeps_tool_result_in_next_request(monkeypatch):
    displayed = asyncio.Event()

    async def before(index):
        if index == 1:
            await asyncio.wait_for(displayed.wait(), 1)

    calls = [{'index': 0, 'id': 'read-1', 'type': 'function',
              'function': {'name': 'Read', 'arguments': '{}'}}]
    requests = transport(monkeypatch, [
        reply([sse({'reasoning_content': 'private reasoning'}), sse({'tool_calls': calls}),
               sse(finish='tool_calls'), b'data: [DONE]\n\n']),
        reply([sse({'content': '文'}), sse({'content': '字'}),
               sse(finish='stop'), b'data: [DONE]\n\n'], before)])
    events = []
    tool_events = []

    async def read(arguments, call_id):
        return {'text': 'tool body visible to model'}

    tools = pipeline(read)
    tools.emit = lambda kind, payload: tool_events.append((kind, payload))
    async for event in stream_tool_chat('read please', [], 'scope', None, tools):
        events.append(event)
        if event.get('delta') == '文':
            displayed.set()
    assert ''.join(event.get('delta', '') for event in events) == '文字'
    assert all('reasoning' not in event for event in events)
    assistant, tool = requests[1]['messages'][-2:]
    assert assistant['tool_calls'][0]['id'] == tool['tool_call_id'] == 'read-1'
    assert 'tool body visible to model' in tool['content']
    turn = events[-1]['turn_messages']
    assert [message['role'] for message in turn] == ['user', 'assistant', 'tool', 'assistant']
    assert turn[1]['reasoning_content'] == 'private reasoning'
    assert [kind for kind, payload in tool_events] == ['tool.started', 'tool.completed']
    assert tool_events[-1][1]['receipt']['isError'] is False


async def test_chat_does_not_execute_unfinished_tool_stream(monkeypatch):
    calls = [{'index': 0, 'id': 'read-1', 'type': 'function',
              'function': {'name': 'Read', 'arguments': '{}'}}]
    transport(monkeypatch, [reply([sse({'tool_calls': calls})])])

    async def read(arguments, call_id):
        pytest.fail('incomplete stream must not execute a tool')

    with pytest.raises(PydanticAIAdapterError, match='响应未完成') as raised:
        async for event in stream_tool_chat('read', [], 'scope', None, pipeline(read)):
            assert 'turn_messages' not in event
    assert raised.value.status == 'UNKNOWN_OPERATION'


async def test_chat_tool_failure_receipt_is_terminal_and_preserved(monkeypatch):
    calls = [{'index': 0, 'id': 'read-1', 'type': 'function',
              'function': {'name': 'Read', 'arguments': '{}'}}]
    requests = transport(monkeypatch, [
        reply([sse({'tool_calls': calls}), sse(finish='tool_calls'), b'data: [DONE]\n\n']),
        reply([sse({'content': 'cannot read'}), sse(finish='stop'), b'data: [DONE]\n\n'])])

    async def read(arguments, call_id):
        raise PermissionError('read blocked')

    events = []
    with pytest.raises(PydanticAIAdapterError) as raised:
        async for event in stream_tool_chat('read', [], 'scope', None, pipeline(read)):
            events.append(event)
    assert any(event.get('failed') == 1 for event in events)
    assert len(requests) == 1
    assert raised.value.category == 'tool_execution'
    assert 'read blocked' in str(raised.value.details)


async def test_chat_sends_only_the_user_message_and_still_persists_audit(monkeypatch, tmp_path):
    requests = transport(monkeypatch, [reply([
        sse({'content': 'public reply'}), sse(finish='stop'), b'data: [DONE]\n\n'])])
    # library 只提供审计库路径；Chat 不再检索知识文档，也不再产生 sources 事件。
    library = SimpleNamespace(path=tmp_path / 'chat-audit.sqlite3')
    events = [event async for event in stream_tool_chat('question', [], 'scope', library,
                                                       pipeline(lambda *args: {}))]
    assert all('sources' not in event for event in events)
    assert ''.join(event.get('delta', '') for event in events) == 'public reply'
    assert '检索参考文档' not in str(requests) and '知识文档' not in str(requests)
    with sqlite3.connect(library.path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM chat_model_exchanges').fetchone()[0] == 1


async def test_chat_rejects_empty_final_response(monkeypatch):
    transport(monkeypatch, [reply([sse(finish='stop'), b'data: [DONE]\n\n'])])
    with pytest.raises(PydanticAIAdapterError, match='未返回正文'):
        async for event in stream_tool_chat('question', [], 'scope', None, pipeline(lambda *args: {})):
            assert 'turn_messages' not in event
