import copy
import asyncio
import importlib
import inspect
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from tracefix.agents import pydantic_ai_adapter as adapter_module
from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter, PydanticAIAdapterError
from tracefix.model.chat import _history_dict
from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import BrowserAction
from tracefix.runtime.tools import ToolOperationUnknown, ToolResult, ToolSpec


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str


@pytest.fixture
def fake_pydantic_ai(monkeypatch):
    instances = []

    class UnexpectedModelBehavior(RuntimeError):
        pass

    class ModelRetry(RuntimeError):
        pass

    class Tool:
        @classmethod
        def from_schema(cls, function, *, name, description, json_schema, takes_ctx=False,
                        **kwargs):
            return SimpleNamespace(function=function, name=name, description=description,
                                   json_schema=json_schema, takes_ctx=takes_ctx, options=kwargs)

    class Agent:
        def __init__(self, model, *, output_type, tools, retries, output_retries=None,
                     instructions=None, **kwargs):
            self.model = model
            self.output_type = output_type
            self.tools = tools
            self.retries = retries
            self.output_retries = output_retries
            self.instructions = instructions
            self.options = kwargs
            self.run_count = 0
            instances.append(self)

        async def run(self, prompt, *, message_history=None, event_stream_handler=None):
            self.run_count += 1
            return await self.model(self, prompt, message_history, event_stream_handler)

    package = ModuleType('pydantic_ai')
    package.Agent = Agent
    tools = ModuleType('pydantic_ai.tools')
    tools.Tool = Tool
    exceptions = ModuleType('pydantic_ai.exceptions')
    exceptions.UnexpectedModelBehavior = UnexpectedModelBehavior
    exceptions.ModelRetry = ModelRetry
    modules = {'pydantic_ai': package, 'pydantic_ai.tools': tools,
               'pydantic_ai.exceptions': exceptions}
    original_import = importlib.import_module

    def import_module(name, package=None):
        if name in modules:
            return modules[name]
        return original_import(name, package)

    monkeypatch.setattr(adapter_module.importlib, 'import_module', import_module)
    return SimpleNamespace(instances=instances, modules=modules,
                           UnexpectedModelBehavior=UnexpectedModelBehavior,
                           ModelRetry=ModelRetry)


def completion(output=None, messages=None):
    return SimpleNamespace(output=output if output is not None else BrowserAction(kind='finish'),
                           all_messages=lambda: list(messages or []))


def readonly_tool(**kwargs):
    return ToolSpec('repo.read', 'Read repository evidence', ReadArguments, **kwargs)


def tool_context(call_id='read-1', *, retry=0, messages=None, retries=None):
    return SimpleNamespace(tool_call_id=call_id, retry=retry, messages=list(messages or []),
                           retries=dict(retries or {}))


async def execute_tool(agent, *, call_id='read-1', arguments=None, retry=0, messages=None,
                       retries=None):
    tool = agent.tools[0]
    assert tool.takes_ctx is True
    result = tool.function(tool_context(call_id, retry=retry, messages=messages, retries=retries),
                           **(arguments if arguments is not None else {'path': 'src/app.py'}))
    return await result if inspect.isawaitable(result) else result


async def test_success_returns_existing_typed_schema_and_context(fake_pydantic_ai):
    events = []
    context = {'goal': '检查页面', 'evidence_refs': ['evidence-1']}
    original_context = copy.deepcopy(context)
    expected = BrowserAction(kind='finish')

    async def model(agent, prompt, history, handler):
        assert agent.output_type is BrowserAction
        assert agent.retries == 1
        assert '检查页面' in prompt and 'evidence-1' in prompt
        return completion(expected)

    value = await PydanticAIAdapter(model).generate(BrowserAction, context,
        on_event=lambda kind, payload: events.append((kind, payload)))

    assert isinstance(value.value, BrowserAction)
    assert value.value == expected
    assert context == original_context
    assert [kind for kind, payload in events][0] == 'adapter.started'
    assert [kind for kind, payload in events][-1] == 'adapter.completed'


async def test_invalid_output_has_validation_category(fake_pydantic_ai):
    async def model(agent, prompt, history, handler):
        return completion({'kind': 'not-an-action'}, ['invalid-output-history'])

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model, output_retries=0).generate(BrowserAction, {})

    assert raised.value.category == 'output_validation'
    assert raised.value.status == 'FAILED'
    assert 'invalid-output-history' in repr(raised.value.details['message_history'])
    assert fake_pydantic_ai.instances[0].run_count == 1


async def test_readonly_tool_uses_injected_sync_port_and_preserves_receipt(fake_pydantic_ai):
    calls, events = [], []
    receipt = {'evidence_ref': 'evidence-read-1', 'text': 'existing evidence'}

    def port(name, arguments, call_id):
        calls.append((name, arguments, call_id))
        return receipt

    async def model(agent, prompt, history, handler):
        assert agent.tools[0].max_retries == 0
        assert await execute_tool(agent) == receipt
        return completion()

    value = await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
        tool_executor=port, on_event=lambda kind, payload: events.append((kind, payload)))

    assert value.value.kind == 'finish'
    assert calls == [('repo.read', {'path': 'src/app.py'}, 'read-1')]
    started = next(payload for kind, payload in events if kind == 'tool.started')
    finished = next(payload for kind, payload in events if kind == 'tool.completed')
    assert started['tool_call_id'] == finished['tool_call_id'] == 'read-1'
    assert finished['tool_name'] == 'repo.read'
    assert started['arguments'] == {'path': 'src/app.py'}
    assert finished['result'] == receipt


async def test_completed_tool_receipt_is_reused_without_reexecution(fake_pydantic_ai):
    calls, results = [], []

    def port(name, arguments, call_id):
        calls.append(call_id)
        return {'evidence_ref': 'reused-evidence'}

    async def on_result(_, payload):
        results.append(payload['reused'])

    async def model(agent, prompt, history, handler):
        assert await execute_tool(agent) == {'evidence_ref': 'reused-evidence'}
        assert await execute_tool(agent) == {'evidence_ref': 'reused-evidence'}
        return completion()

    await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
        tool_executor=port, on_tool_result=on_result)

    assert calls == ['read-1']
    assert results == [False, True]


async def test_tool_call_id_cannot_change_arguments_after_completion(fake_pydantic_ai):
    calls = []

    def port(name, arguments, call_id):
        calls.append(call_id)
        return {'evidence_ref': 'first-evidence'}

    async def model(agent, prompt, history, handler):
        await execute_tool(agent, arguments={'path': 'src/app.py'})
        await execute_tool(agent, arguments={'path': 'src/other.py'})

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
            tool_executor=port)

    assert raised.value.category == 'tool_protocol'
    assert calls == ['read-1']


async def test_async_port_and_event_callback_are_awaited(fake_pydantic_ai):
    calls, events = [], []

    async def port(name, arguments, call_id):
        calls.append(call_id)
        return {'text': 'async evidence'}

    async def callback(kind, payload):
        events.append((kind, payload))

    async def model(agent, prompt, history, handler):
        assert await execute_tool(agent) == {'text': 'async evidence'}
        assert any(kind == 'tool.completed' for kind, payload in events)
        return completion()

    await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
                                           tool_executor=port, on_event=callback)

    assert calls == ['read-1']
    assert events[-1][0] == 'adapter.completed'


async def test_tool_exception_is_classified_without_automatic_retry(fake_pydantic_ai):
    calls = []

    def port(name, arguments, call_id):
        calls.append(call_id)
        raise RuntimeError('read failed')

    async def model(agent, prompt, history, handler):
        await execute_tool(agent, messages=['before-read'], retry=1, retries={'repo.read': 1})
        pytest.fail('工具异常后不应继续返回结果')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
                                               tool_executor=port)

    assert raised.value.category == 'tool_execution'
    assert calls == ['read-1']
    assert raised.value.details['tool_calls'][0]['tool_call_id'] == 'read-1'
    assert 'before-read' in repr(raised.value.details['retry_context'])
    assert raised.value.__cause__ is not None


async def test_unknown_operation_preserves_status_and_operation_id(fake_pydantic_ai):
    calls = []

    def port(name, arguments, call_id):
        calls.append(call_id)
        raise ToolOperationUnknown('执行结果待核查', call_id=call_id, name=name,
                                   operation_id='operation-unknown-1')

    async def model(agent, prompt, history, handler):
        await execute_tool(agent)
        pytest.fail('UNKNOWN 不允许自动重试')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model, output_retries=3).generate(BrowserAction, {},
            tools=(readonly_tool(),), tool_executor=port)

    assert raised.value.category == 'tool_execution'
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['operation_id'] == 'operation-unknown-1'
    assert raised.value.details['requires_manual_review'] is True
    assert raised.value.details['retryable'] is False
    assert calls == ['read-1']
    assert fake_pydantic_ai.instances[0].run_count == 1


async def test_optional_dependency_is_loaded_only_when_running(monkeypatch):
    imports = []
    original_import = importlib.import_module

    def unavailable(name, package=None):
        if name.startswith('pydantic_ai'):
            imports.append(name)
            raise ModuleNotFoundError("No module named 'pydantic_ai'", name='pydantic_ai')
        return original_import(name, package)

    monkeypatch.setattr(adapter_module.importlib, 'import_module', unavailable)
    adapter = PydanticAIAdapter(object())
    assert imports == []

    with pytest.raises(PydanticAIAdapterError) as raised:
        await adapter.generate(BrowserAction, {})

    assert imports == ['pydantic_ai']
    assert raised.value.category == 'unavailable'
    assert 'PydanticAI' in str(raised.value)


async def test_stream_interruption_keeps_tool_results_and_retry_context(fake_pydantic_ai):
    receipt = {'evidence_ref': 'evidence-before-stream-error'}
    events, calls = [], []
    history = ['caller-history']

    async def model(agent, prompt, messages, handler):
        await execute_tool(agent, messages=['retry-history'], retry=1,
                           retries={'repo.read': 1})

        async def stream():
            yield SimpleNamespace(event_kind='part_delta', delta={'content_delta': 'partial'})
            raise httpx.ReadError('stream disconnected')

        await handler(tool_context(messages=['retry-history']), stream())
        pytest.fail('中断流不应完成')

    def port(name, arguments, call_id):
        calls.append(call_id)
        return receipt

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
            tool_executor=port, message_history=history,
            on_event=lambda kind, payload: events.append((kind, payload)))

    assert raised.value.category == 'stream_interrupted'
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['retryable'] is False
    assert raised.value.details['tool_calls'][0]['result'] == receipt
    assert 'retry-history' in repr(raised.value.details['retry_context'])
    assert calls == ['read-1']
    assert history == ['caller-history']
    assert any(kind == 'model.stream' for kind, payload in events)
    assert fake_pydantic_ai.instances[0].run_count == 1


@pytest.mark.parametrize('side_effect,submission', [('write', False), ('external', False),
                                                   ('write', True)])
async def test_effectful_or_submission_tool_uses_injected_port_without_retry(
        fake_pydantic_ai, side_effect, submission):
    calls = []
    spec = readonly_tool(side_effect=side_effect, submission=submission,
                         idempotency_key=lambda arguments: arguments.path)

    async def model(agent, prompt, history, handler):
        assert agent.tools[0].max_retries == 0
        assert agent.tools[0].sequential is True
        assert await execute_tool(agent) == {'accepted': True}
        return completion()

    value = await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(spec,),
        tool_executor=lambda name, arguments, call_id: calls.append(
            (name, arguments, call_id)) or {'accepted': True})

    assert value.value.kind == 'finish'
    assert calls == [('repo.read', {'path': 'src/app.py'}, 'read-1')]
    assert all(agent.run_count == 1 for agent in fake_pydantic_ai.instances)


async def test_tools_require_an_injected_executor(fake_pydantic_ai):
    async def model(agent, prompt, history, handler):
        await execute_tool(agent)
        pytest.fail('缺少 port 时工具调用应失败')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),))

    assert raised.value.category == 'tool_execution'
    assert all(agent.run_count == 1 for agent in fake_pydantic_ai.instances)


async def test_cancellation_is_propagated(fake_pydantic_ai):
    async def model(agent, prompt, history, handler):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await PydanticAIAdapter(model).generate(BrowserAction, {})


async def test_usage_and_concurrency_options_are_preserved(fake_pydantic_ai):
    usage = SimpleNamespace(input_tokens=5, output_tokens=3, details={'cached': 2},
                            requests=1, total_tokens=8, request_tokens=5, response_tokens=3)

    async def model(agent, prompt, history, handler):
        result = completion()
        result.usage = usage
        return result

    result = await PydanticAIAdapter(model).generate(BrowserAction, {}, max_concurrency=3)

    assert result.usage['input_tokens'] == 5
    assert result.usage['output_tokens'] == 3
    assert result.usage['details'] == {'cached': 2}
    assert result.usage['total_tokens'] == 8
    assert fake_pydantic_ai.instances[0].options['max_concurrency'] == 3


async def test_gateway_maps_legacy_history_and_constructor_port(monkeypatch):
    captured = {}

    async def generate(self, schema, context, **options):
        captured.update(options)
        return SimpleNamespace(value=BrowserAction(kind='finish'))

    monkeypatch.setattr(PydanticAIAdapter, 'generate', generate)
    port = object()
    await Gateway(key='ci', tool_executor=port).generate(BrowserAction, {},
        messages=[{'role': 'user', 'content': 'history'}])

    assert captured['message_history'] == [{'role': 'user', 'content': 'history'}]
    assert captured['tool_executor'] is port


def test_chat_history_projection_preserves_framework_tool_identity():
    messages = [
        SimpleNamespace(kind='response', parts=[SimpleNamespace(
            part_kind='tool-call', tool_name='RepoRead', args='{"path":"a"}',
            tool_call_id='call-7')]),
        SimpleNamespace(kind='request', parts=[SimpleNamespace(
            part_kind='tool-return', tool_name='RepoRead', content={'ok': True},
            tool_call_id='call-7')]),
    ]

    projected = _history_dict(messages)

    assert projected[0]['tool_calls'][0]['id'] == 'call-7'
    assert projected[1] == {'role': 'tool', 'tool_call_id': 'call-7',
                            'name': 'RepoRead', 'content': '{"ok": true}'}


async def test_retry_tool_context_and_message_history_are_preserved(fake_pydantic_ai):
    events, calls = [], []
    history = [{'tool_call_id': 'prior-read', 'result': {'evidence_ref': 'prior-evidence'}}]
    original_history = copy.deepcopy(history)
    retry_messages = [*history, {'retry': 'please correct typed output'}]

    async def model(agent, prompt, messages, handler):
        assert messages == original_history
        assert agent.retries == 2
        assert agent.tools[0].max_retries == 0
        await execute_tool(agent, call_id='retry-read', retry=1, messages=retry_messages,
                           retries={'repo.read': 1})
        return completion(messages=retry_messages)

    def port(name, arguments, call_id):
        calls.append(call_id)
        return {'evidence_ref': 'corrected-evidence'}

    await PydanticAIAdapter(model, output_retries=2).generate(BrowserAction, {},
        tools=(readonly_tool(),), tool_executor=port, message_history=history,
        on_event=lambda kind, payload: events.append((kind, payload)))

    started = next(payload for kind, payload in events if kind == 'tool.started')
    assert started['tool_call_id'] == 'retry-read'
    assert started['retry'] == 1
    assert started['messages'] == retry_messages
    assert history == original_history
    assert calls == ['retry-read']


async def test_adapter_reuse_has_no_shared_tool_or_retry_state(fake_pydantic_ai):
    calls = []
    pass_count = 0

    async def model(agent, prompt, history, handler):
        nonlocal pass_count
        pass_count += 1
        if pass_count == 1:
            await execute_tool(agent, call_id='first-call', messages=['first-run-context'])
            return completion()
        return completion({'kind': 'invalid'}, ['second-run-context'])

    adapter = PydanticAIAdapter(model, output_retries=0)
    await adapter.generate(BrowserAction, {'run': 'first'}, tools=(readonly_tool(),),
                           tool_executor=lambda name, arguments, call_id: calls.append(call_id))
    with pytest.raises(PydanticAIAdapterError) as raised:
        await adapter.generate(BrowserAction, {'run': 'second'})

    assert calls == ['first-call']
    assert raised.value.category == 'output_validation'
    assert raised.value.details['tool_calls'] == []
    assert 'first-run-context' not in repr(raised.value.details)
    assert len(fake_pydantic_ai.instances) == 2


async def test_invalid_tool_arguments_do_not_reach_port(fake_pydantic_ai):
    calls = []

    async def model(agent, prompt, history, handler):
        await execute_tool(agent, arguments={'path': 'src/app.py', 'unexpected': True})
        pytest.fail('非法参数不能继续执行工具')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
            tool_executor=lambda *arguments: calls.append(arguments))

    assert raised.value.category == 'tool_protocol'
    assert calls == []


async def test_event_callback_error_never_reexecutes_successful_tool(fake_pydantic_ai):
    calls = []
    receipt = {'evidence_ref': 'callback-failed-after-receipt'}

    def callback(kind, payload):
        if kind == 'tool.completed':
            raise RuntimeError('event sink unavailable')

    def port(name, arguments, call_id):
        calls.append(call_id)
        return receipt

    async def model(agent, prompt, history, handler):
        await execute_tool(agent)
        pytest.fail('回调失败不能继续执行模型')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
                                               tool_executor=port, on_event=callback)

    assert raised.value.category == 'event_callback'
    assert raised.value.details['tool_calls'][0]['result'] == receipt
    assert calls == ['read-1']
    assert fake_pydantic_ai.instances[0].run_count == 1


@pytest.mark.parametrize('message,category', [
    ('Exceeded maximum retries (1) for output validation', 'output_validation'),
    ('Unknown content part', 'model'),
])
async def test_model_behavior_error_inside_stream_keeps_its_category(fake_pydantic_ai,
                                                                   message, category):
    async def model(agent, prompt, history, handler):
        async def stream():
            raise fake_pydantic_ai.UnexpectedModelBehavior(message)
            yield

        await handler(tool_context(), stream())
        pytest.fail('模型协议错误不能继续完成')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {})

    assert raised.value.category == category
    assert raised.value.status == 'FAILED'
    assert fake_pydantic_ai.instances[0].run_count == 1


async def test_exception_group_preserves_unknown_tool_failure(fake_pydantic_ai):
    calls = []

    def port(name, arguments, call_id):
        calls.append(call_id)
        unknown = ToolOperationUnknown('批量读取结果待核查', call_id=call_id, name=name,
                                       operation_id='group-op')
        raise ExceptionGroup('batch', [unknown])

    async def model(agent, prompt, history, handler):
        await execute_tool(agent)
        pytest.fail('异常组中的 UNKNOWN 不允许继续模型调用')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
                                               tool_executor=port)

    assert raised.value.category == 'tool_execution'
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['operation_id'] == 'group-op'
    assert raised.value.details['retryable'] is False
    assert calls == ['read-1']
    assert fake_pydantic_ai.instances[0].run_count == 1


@pytest.mark.parametrize('failure_kind,category,status', [
    ('output', 'output_validation', 'FAILED'),
    ('tool', 'tool_execution', 'UNKNOWN_OPERATION'),
])
async def test_stream_exception_group_preserves_nested_error_classification(fake_pydantic_ai,
    failure_kind, category, status):
    if failure_kind == 'output':
        failure = fake_pydantic_ai.UnexpectedModelBehavior(
            'Exceeded maximum retries (1) for output validation')
    else:
        failure = PydanticAIAdapterError('工具结果待核查', category='tool_execution',
            status='UNKNOWN_OPERATION', details={'operation_id': 'stream-group-op'})

    async def model(agent, prompt, history, handler):
        async def stream():
            yield SimpleNamespace(event_kind='part_delta', delta={'content_delta': 'partial'})
            raise ExceptionGroup('stream batch', [failure])

        await handler(tool_context(), stream())
        pytest.fail('异常组不能转换为成功模型结果')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {})

    assert raised.value.category == category
    assert raised.value.status == status
    if failure_kind == 'tool':
        assert raised.value.details['operation_id'] == 'stream-group-op'
        assert raised.value.details['requires_manual_review'] is True
    assert fake_pydantic_ai.instances[0].run_count == 1


async def test_historical_output_retry_does_not_reclassify_model_protocol_error(fake_pydantic_ai):
    retry_message = SimpleNamespace(parts=[
        SimpleNamespace(part_kind='retry-prompt', tool_name='final_result')])

    async def model(agent, prompt, history, handler):
        async def stream():
            raise fake_pydantic_ai.UnexpectedModelBehavior('Unknown content part')
            yield

        await handler(tool_context(messages=[retry_message]), stream())
        pytest.fail('历史校验重试不能掩盖新的模型协议错误')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {})

    assert raised.value.category == 'model'
    assert raised.value.status == 'FAILED'
    assert 'final_result' in repr(raised.value.details['message_history'])
    assert fake_pydantic_ai.instances[0].run_count == 1


@pytest.mark.parametrize('status', ['FAILED', 'UNKNOWN_OPERATION'])
async def test_error_tool_result_preserves_receipt_and_operation_id(fake_pydantic_ai, status):
    calls = []
    operation_id = 'receipt-' + status.lower()
    receipt = ToolResult(call_id='read-1', name='repo.read', isError=True,
        result={'partial_evidence_ref': 'evidence-before-error'}, executed=True,
        artifact_ref='error-artifact', error={'status': status, 'category': 'tool_execution',
            'message': '读取证据失败', 'operation_id': operation_id})

    def port(name, arguments, call_id):
        calls.append(call_id)
        return receipt

    async def model(agent, prompt, history, handler):
        await execute_tool(agent)
        pytest.fail('错误工具回执不得作为成功结果交给模型')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {}, tools=(readonly_tool(),),
                                               tool_executor=port)

    assert raised.value.category == 'tool_execution'
    assert raised.value.status == status
    assert raised.value.details['operation_id'] == operation_id
    recorded = raised.value.details['tool_calls'][0]['result']
    if isinstance(recorded, BaseModel):
        recorded = recorded.model_dump(mode='json', by_alias=True)
    assert recorded == receipt.model_dump(mode='json', by_alias=True)
    assert calls == ['read-1']
    assert fake_pydantic_ai.instances[0].run_count == 1


@pytest.mark.parametrize('approval', ['policy', 'always'])
async def test_approval_tool_is_rejected_before_model_or_port(fake_pydantic_ai, approval):
    calls = []

    async def model(agent, prompt, history, handler):
        pytest.fail('适配器不得执行需要审批的工具')

    with pytest.raises(PydanticAIAdapterError) as raised:
        await PydanticAIAdapter(model).generate(BrowserAction, {},
            tools=(readonly_tool(approval=approval),),
            tool_executor=lambda *arguments: calls.append(arguments))

    assert raised.value.category == 'configuration'
    assert calls == []
    assert all(agent.run_count == 0 for agent in fake_pydantic_ai.instances)
