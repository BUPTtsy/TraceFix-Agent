import asyncio
import json
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest

from tracefix.cli.main import chat_jsonl
from tracefix.model.chat import stream_tool_chat
from tracefix.model import protocol
from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapterError
from tracefix.runtime.contracts import Phase
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolSpec


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
    async for event in stream_tool_chat('read please', [], 'scope', None, tools, False):
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
        async for event in stream_tool_chat('read', [], 'scope', None, pipeline(read), False):
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
        async for event in stream_tool_chat('read', [], 'scope', None, pipeline(read), False):
            events.append(event)
    assert any(event.get('failed') == 1 for event in events)
    assert len(requests) == 1
    assert raised.value.category == 'tool_execution'
    assert 'read blocked' in str(raised.value.details)


async def test_chat_filters_hidden_retrieval_records_from_refs_and_model_request(monkeypatch):
    requests = transport(monkeypatch, [reply([
        sse({'content': 'public reply'}), sse(finish='stop'), b'data: [DONE]\n\n'])])
    documents = SimpleNamespace(search=lambda *args: [
        {'id': 'public', 'title': 'visible', 'version': 1, 'excerpt': 'public document'},
        {'id': 'hidden', 'title': 'private scores', 'version': 1, 'held_out': True, 'excerpt': 'secret score'},
        {'id': 'encoded', 'title': 'hidden JSON', 'version': 1,
         'excerpt': '{"metadata":{"split":"held_out"},"score":999}'}])
    events = [event async for event in stream_tool_chat('question', [], 'scope', documents,
                                                       pipeline(lambda *args: {}))]
    assert events[0]['sources'] == [{'id': 'public', 'title': 'visible', 'version': 1}]
    assert 'secret score' not in str(requests) and 'score' not in str(requests)


async def test_chat_rejects_empty_final_response(monkeypatch):
    transport(monkeypatch, [reply([sse(finish='stop'), b'data: [DONE]\n\n'])])
    with pytest.raises(PydanticAIAdapterError, match='未返回正文'):
        async for event in stream_tool_chat('question', [], 'scope', None, pipeline(lambda *args: {}), False):
            assert 'turn_messages' not in event


class InputQueue:
    def __init__(self):
        import queue
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get(timeout=3)

    def send(self, request):
        self.lines.put(json.dumps(request) + '\n')


async def wait_event(output, kind, message_id=None):
    for attempt in range(100):
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        if any(value['type'] == kind and (message_id is None or value.get('message_id') == message_id)
               for value in values):
            return values
        await asyncio.sleep(0.01)
    raise AssertionError(f'missing {kind}: {output.getvalue()}')


async def test_jsonl_starts_empty_preserves_whole_turn_and_cancels_without_history(tmp_path, monkeypatch):
    histories = []
    input_queue, output = InputQueue(), StringIO()
    monkeypatch.setattr('tracefix.cli.main.sys.stdin', input_queue)
    monkeypatch.setattr('tracefix.cli.main.sys.stdout', output)
    monkeypatch.setattr('tracefix.cli.main.load_projects', lambda path: {})
    monkeypatch.setattr('tracefix.cli.main.ScopeResolver',
        lambda projects, path: SimpleNamespace(context=lambda scope: SimpleNamespace(active_scope=scope)))
    monkeypatch.setattr('tracefix.model.chat_tools.build_chat_tools', lambda *args, **kwargs: None)

    async def fake_chat(message, history, project, library, tools, use_knowledge):
        histories.append((message, list(history)))
        yield {'delta': '正'}
        if message == 'cancel':
            await asyncio.Event().wait()
        yield {'delta': '文'}
        yield {'turn_messages': [{'role': 'user', 'content': message},
            {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'fixture'}]},
            {'role': 'tool', 'tool_call_id': 'fixture', 'content': 'result body'},
            {'role': 'assistant', 'content': '正文'}]}

    monkeypatch.setattr('tracefix.cli.main.stream_tool_chat', fake_chat)
    args = SimpleNamespace(project='scope', projects='fixture.yaml',
        console_db=tmp_path / 'console.sqlite3', data=str(tmp_path), chat_session='fresh-session')
    runner = asyncio.create_task(chat_jsonl(args))
    await wait_event(output, 'chat.ready')
    input_queue.send({'id': 'first', 'message': 'hello'})
    await wait_event(output, 'chat.finished', 'first')
    input_queue.send({'id': 'abort', 'message': 'cancel'})
    await wait_event(output, 'chat.delta', 'abort')
    input_queue.send({'type': 'cancel', 'id': 'abort'})
    await wait_event(output, 'chat.cancelled', 'abort')
    input_queue.send({'id': 'next', 'message': 'next'})
    await wait_event(output, 'chat.finished', 'next')
    assert histories[0][1] == []
    assert len(histories[2][1]) == 4 and histories[2][1][2]['role'] == 'tool'
    input_queue.send({'type': 'clear'})
    await wait_event(output, 'chat.cleared')
    input_queue.send({'id': 'clear', 'message': 'after clear'})
    await wait_event(output, 'chat.finished', 'clear')
    assert histories[-1][1] == []
    input_queue.send({'type': 'quit'})
    await runner
    assert not any('reasoning' in value for value in output.getvalue().splitlines())
