import asyncio
import copy
import json
import pytest
import httpx

from tracefix.execution.policy import Policy
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, Locator, Outcome,
    Phase, RunState, RunStatus)
from tracefix.runtime.contracts import TestSpec as Spec


def _delegate_tool_call(call_id, arguments):
    return {'id': call_id, 'type': 'function',
            'function': {'name': 'AgentDelegate', 'arguments': json.dumps(arguments)}}


def _browser_tool_call(call_id, name, arguments):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': arguments}}


def _delegate_arguments(task_id='worker-1'):
    return {
        'task_id': task_id,
        'role': '代码探索者',
        'objective': '定位刷新后状态丢失的完整调用链并确认最小修复位置',
        'prompt': ('请先读取授权文件并逐步追踪状态初始化、事件绑定、刷新后的数据流和调用者；'
                   '每一步记录文件路径与行号证据，禁止修改文件、禁止派生 Worker，'
                   '发现不足时明确说明缺口并在停止前返回结构化结论。'),
        'phase': 'DIAGNOSE',
        'allowed_tools': ['code.read', 'code.references'],
        'allowed_files': ['src/Filter.tsx'],
        'allowed_artifacts': [],
        'writable_files': [],
        'depends_on': [],
        'expected_output': '返回带有文件行号和证据引用的调查结论、未决问题与后续建议',
        'completion_criteria': ['所有结论均有授权文件或 artifact 引用'],
        'constraints': ['不得派生子 Worker', '不得执行写入命令', '不得扩大授权范围'],
    }


async def test_delegate_tools_are_strict_and_parallel(monkeypatch):
    calls = [_delegate_tool_call('worker-1', _delegate_arguments('worker-1')),
             _delegate_tool_call('worker-2', _delegate_arguments('worker-2'))]
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        if len(requests) == 1:
            message = {'content': None, 'tool_calls': calls}
            return httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
                'choices': [{'finish_reason': 'tool_calls', 'message': message}]})
        return httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
            'choices': [{'finish_reason': 'stop',
                        'message': {'content': '{"role":"button","name":"Save"}'}}]})

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    active = 0
    maximum = 0

    async def execute(name, arguments, call_id):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {'task_id': arguments['task_id'], 'status': 'queued'}

    result = await Gateway(key='ci', additional_tools=Gateway.delegation_tools()).generate(
        Locator, {}, tool_executor=execute)
    assert result.value == Locator(role='button', name='Save')
    assert maximum == 2
    assert requests[0]['parallel_tool_calls'] is True
    assert [tool['function']['name'] for tool in requests[0]['tools']] == [
        'AgentDelegate', 'AgentJoin']
    assert '<supervisor_tool_protocol>' in requests[0]['messages'][0]['content']


async def test_browser_tools_remain_serial_when_extra_tools_are_offered(monkeypatch):
    calls = [_browser_tool_call('browser-1', 'BrowserSnapshot', '{}'),
             _delegate_tool_call('worker-1', _delegate_arguments())]
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        if len(requests) == 1:
            message = {'content': None, 'tool_calls': calls}
            return httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
                'choices': [{'finish_reason': 'tool_calls', 'message': message}]})
        return httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]})

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    executed = []

    async def execute(name, arguments, call_id):
        executed.append(name)
        return {'observation': {'id': 'obs'}} if name == 'browser_snapshot' else {'status': 'queued'}

    result = await Gateway(key='ci', additional_tools=Gateway.delegation_tools()).generate(
        BrowserAction, {}, tool_executor=execute)
    assert result.value.kind == 'finish'
    assert requests[0]['parallel_tool_calls'] is False
    assert executed == ['browser_snapshot', 'agent.delegate']


async def test_additional_tool_batch_is_validated_before_execution(monkeypatch):
    bad = _delegate_arguments()
    del bad['constraints']
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        message = {'content': None, 'tool_calls': [_delegate_tool_call('bad', bad)]}
        return httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
            'choices': [{'finish_reason': 'tool_calls', 'message': message}]})

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    executed = []

    async def execute(*args):
        executed.append(args)

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='ci', additional_tools=Gateway.delegation_tools()).generate(
            Locator, {}, tool_executor=execute)
    assert raised.value.category == 'tool_protocol'
    assert executed == []
    assert requests[0]['parallel_tool_calls'] is True
from tracefix.runtime.smoke import FakeModel, make_engine


@pytest.fixture(autouse=True)
def buffered_gateway_protocol(monkeypatch):
    monkeypatch.setenv('TRACEFIX_STREAM', 'false')


def test_gateway_default_output_and_tool_round_limits():
    gateway = Gateway(key='test-only-not-a-secret')

    assert gateway.max_output_tokens == 20480
    assert gateway.max_tool_rounds == 40


async def test_gateway_validates_structured_output_and_accounts_actual_usage(monkeypatch):
    calls=[];usage=[];responses=[]
    async def post(client,url,**kwargs):
        assert url=='https://api.deepseek.com/chat/completions'
        assert kwargs['json']['thinking']=={'type':'enabled'}
        assert any(tool['function']['name'] == 'BrowserSnapshot'
                   for tool in kwargs['json']['tools'])
        return httpx.Response(200,json={'model':'test-revision','usage':{'total_tokens':17},'choices':[{'finish_reason':'stop','message':{'content':'{"kind":"finish"}','reasoning_content':'MUST_NOT_RETURN'}}]})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    gateway=Gateway(key='test-only-not-a-secret')
    result=await gateway.generate(BrowserAction,{'goal':'verify'},agent_instructions='Always verify persistence.',
        on_attempt=lambda *x:calls.append(x[0]),on_response=lambda exchange,raw:responses.append(raw),on_usage=usage.append)
    assert result.value.kind=='finish'
    assert usage==[{'total_tokens':17}] and calls==['deepseek-chat']
    assert 'MUST_NOT_RETURN' not in repr(result)
    assert responses[0]['body']['choices'][0]['message']['reasoning_content']=='MUST_NOT_RETURN'


async def test_gateway_missing_key_is_structured_configuration_error():
    with pytest.raises(ModelError) as raised:
        await Gateway(key='').generate(BrowserAction, {})
    assert raised.value.status == 'FAILED'
    assert raised.value.category == 'configuration'
    assert raised.value.details['category'] == 'configuration'


async def test_gateway_sends_project_instructions_and_redacted_audit_request(monkeypatch):
    requests=[]
    async def post(client,url,**kwargs):
        system=kwargs['json']['messages'][0]['content']
        assert '<project_instructions>\nKeep the patch minimal.\n</project_instructions>' in system
        return httpx.Response(200,json={'model':'test','usage':{'total_tokens':1},
            'choices':[{'finish_reason':'stop','message':{'content':'{"kind":"finish"}'}}]})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    await Gateway(key='must-not-persist').generate(BrowserAction,{},agent_instructions='Keep the patch minimal.',
        on_attempt=lambda model,request,attempt: requests.append(request))
    assert requests[0]['headers']['Authorization']=='[REDACTED]'
    assert 'must-not-persist' not in json.dumps(requests[0])


async def test_gateway_retries_truncated_json_without_execution(monkeypatch):
    responses = [
        httpx.Response(200,json={'usage':{'total_tokens':20},'choices':[{'finish_reason':'length','message':{'content':'{"kind":'}}]}),
        httpx.Response(200,json={'usage':{'total_tokens':21},'choices':[{'finish_reason':'stop','message':{'content':'{"kind":"finish"}'}}]}),
    ]
    async def post(*args,**kwargs):
        return responses.pop(0)
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci').generate(BrowserAction,{})
    assert result.value.kind == 'finish'
    assert sleeps == [1]


async def test_gateway_persists_connection_error(monkeypatch):
    errors=[]
    responses = [httpx.Response(200,json={'usage':{'total_tokens':1},
        'choices':[{'finish_reason':'stop','message':{'content':'{"kind":"finish"}'}}]})]
    async def post(*args,**kwargs):
        if errors:
            return responses.pop(0)
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci').generate(BrowserAction,{},
        on_attempt=lambda *args:{'exchange_id':'model_test'},
        on_error=lambda exchange,error:errors.append((exchange,error)))
    assert result.value.kind == 'finish'
    assert errors[0][0]['exchange_id']=='model_test'
    assert errors[0][1]['type']=='ConnectError'
    assert errors[0][1]['status']=='RETRYING'
    assert errors[0][1]['request_status']=='not_sent'
    assert errors[0][1]['retryable'] is True
    assert sleeps == [1]


async def test_gateway_honors_retry_after_and_keeps_logical_exchange_auditable(monkeypatch):
    responses = [
        httpx.Response(429, headers={'Retry-After': '0.25'}, json={'error': {'type': 'rate_limit'}}),
        httpx.Response(200, json={'model': 'test', 'usage': {'total_tokens': 2},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]}),
    ]
    attempts = []
    attempt_records = []
    sleeps = []

    async def post(*args, **kwargs):
        attempts.append(kwargs)
        return responses.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    errors = []
    result = await Gateway(key='ci', max_retry_delay=1).generate(
        BrowserAction, {},
        on_attempt=lambda model, request, number: (attempt_records.append(request) or request),
        on_error=lambda exchange, error: errors.append(error))

    assert result.value.kind == 'finish'
    assert len(attempts) == 2
    assert sleeps == [0.25]
    assert errors[0]['retry_after_seconds'] == 0.25
    assert errors[0]['status'] == 'RETRYING'
    assert errors[0]['logical_exchange_id']
    assert attempt_records[0]['logical_exchange_id'] == attempt_records[1]['logical_exchange_id']
    assert attempts[0]['json'] == attempts[1]['json']


async def test_gateway_caps_retry_after_without_disabling_bounded_retry(monkeypatch):
    responses = [
        httpx.Response(503, headers={'Retry-After': '10'}, json={'error': 'busy'}),
        httpx.Response(200, json={'usage': {'total_tokens': 1},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]}),
    ]
    sleeps = []

    async def post(*args, **kwargs):
        return responses.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci', max_retry_delay=0.5).generate(BrowserAction, {})

    assert result.value.kind == 'finish'
    assert sleeps == [0.5]


async def test_gateway_timeout_never_retries_unknown_billing(monkeypatch):
    calls = []
    errors = []

    async def post(*args, **kwargs):
        calls.append(1)
        raise httpx.ReadTimeout('upstream did not answer')

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {},
            on_error=lambda exchange, error: errors.append(error))

    assert len(calls) == 1
    assert errors[0]['status'] == 'UNKNOWN_OPERATION'
    assert errors[0]['billing_status'] == 'unknown'
    assert errors[0]['retryable'] is False
    assert errors[0]['requires_manual_review'] is True


async def test_gateway_retries_malformed_provider_response(monkeypatch):
    errors = []
    responses = [
        httpx.Response(200, json={'usage': {'total_tokens': 1}, 'choices': []}),
        httpx.Response(200, json={'usage': {'total_tokens': 1},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]}),
    ]

    async def post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    async def sleep(delay):
        pass
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci').generate(BrowserAction, {},
        on_error=lambda exchange, error: errors.append(error))

    assert result.value.kind == 'finish'
    assert errors[0]['message'] == '缺少 choices[0]'
    assert errors[0]['status'] == 'RETRYING'


@pytest.mark.parametrize('payload', [{'usage': {'total_tokens': 1}, 'choices': [1]}])
async def test_gateway_retries_non_object_provider_payloads(monkeypatch, payload):
    responses = [httpx.Response(200, json=payload),
                 httpx.Response(200, json={'usage': {'total_tokens': 1},
                     'choices': [{'finish_reason': 'stop', 'message': {'content': '{"kind":"finish"}'}}]})]
    async def post(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    async def sleep(delay):
        pass
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    result = await Gateway(key='ci').generate(BrowserAction, {})
    assert result.value.kind == 'finish'


async def test_teacher_locator_that_repeatedly_cannot_bind_ends_loop_detected(tmp_path):
    engine,state=make_engine(tmp_path)
    class UnbindableLocatorModel(FakeModel):
        async def generate(self,schema,context,**kwargs):
            result=await super().generate(schema,context,**kwargs)
            if schema is Decision:
                result.value=Decision(action=BrowserAction(kind='click',
                    observation_id=context['observation']['id'],element_ref='e2',
                    locator=Locator(role='checkbox',name='页面上不存在的复选框')),summary='teacher bad locator')
            return result
    engine.model=UnbindableLocatorModel(engine.workspace)
    await engine.run(state)
    final=engine.store.load(state.run_id,'b')
    assert final.outcome == Outcome.LOOP_DETECTED
    assert final.run_status == RunStatus.ABNORMAL
    assert final.loop_evidence['signals']
    assert any('无法唯一匹配' in event['payload'].get('error', '')
               for event in engine.store.trace(state.run_id, 'b'))
    assert not any(call['kind']=='click' for call in engine.browser.calls)


def test_policy_reports_unbindable_model_locator_as_model_output_error():
    spec=Spec(goal='policy locator gate',assertions=[Assertion(locator=Locator(role='button',name='Save'))],
            regression_assertions=[Assertion(locator=Locator(role='button',name='Save'))])
    state=RunState(scope_id='b',goal='policy locator gate',url='http://app:3000',mode='repair',phase=Phase.EXPLORE)
    observation={'id':'obs1','snapshot':'- button "Save" [ref=e1]\n- button "Save" [ref=e2]'}
    action=BrowserAction(kind='click',observation_id='obs1',element_ref='e1',locator=Locator(role='button',name='Save'))
    with pytest.raises(ModelOutputError,match='无法唯一匹配'):
        Policy(['http://app:3000']).browser(state,action,spec,observation)
