import asyncio
import json
import pytest
import httpx

from tracefix.execution.policy import Policy
from tracefix.model import protocol
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, Locator, Outcome,
    Phase, RunState, RunStatus)
from tracefix.runtime.contracts import TestSpec as Spec
from tracefix.runtime.smoke import FakeModel, make_engine


def _provider_response(content='{"kind":"finish"}', *, calls=None, usage=None,
                       finish='stop', reasoning=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    return httpx.Response(200, json={
        'id': 'provider-response', 'object': 'chat.completion', 'created': 0,
        'model': 'test-revision', 'usage': usage or {
            'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        'choices': [{'index': 0, 'finish_reason': finish, 'message': message}],
    })


@pytest.fixture(autouse=True)
def offline_gateway(monkeypatch):
    monkeypatch.setenv('TRACEFIX_STREAM', 'false')
    responses = []
    requests = []

    async def handle(request):
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        requests.append(json.loads(request.content))
        if not responses:
            pytest.fail('离线 Gateway 测试未注册该请求的响应')
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(
            httpx.MockTransport(handle), boundary), event_hooks={
                'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)

    def configure(values):
        responses.extend(values)
        return requests, responses

    return configure


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


async def test_delegate_tools_are_strict_and_parallel(offline_gateway):
    calls = [_delegate_tool_call('worker-1', _delegate_arguments('worker-1')),
             _delegate_tool_call('worker-2', _delegate_arguments('worker-2'))]
    requests, remaining = offline_gateway([
        _provider_response(None, calls=calls, finish='tool_calls'),
        _provider_response('{"role":"button","name":"Save"}'),
    ])
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
    assert len(requests) == 2 and remaining == []
    assert [tool['function']['name'] for tool in requests[0]['tools']] == [
        'AgentDelegate', 'AgentJoin']
    parameters = requests[0]['tools'][0]['function']['parameters']
    assert parameters['additionalProperties'] is False
    assert all(tool['function']['parameters']['additionalProperties'] is False
               for tool in requests[0]['tools'])
    assert {'task_id', 'role', 'objective', 'prompt', 'phase', 'allowed_tools',
            'expected_output', 'completion_criteria', 'constraints'} <= set(parameters['required'])
    assert '<supervisor_tool_protocol>' in requests[0]['messages'][0]['content']


async def test_browser_tools_remain_serial_when_extra_tools_are_offered(offline_gateway):
    calls = [_browser_tool_call('browser-1', 'BrowserSnapshot', '{}'),
             _browser_tool_call('browser-2', 'BrowserSnapshot', '{}'),
             _delegate_tool_call('worker-1', _delegate_arguments())]
    requests, remaining = offline_gateway([
        _provider_response(None, calls=calls, finish='tool_calls'), _provider_response(),
    ])
    executed = []
    active = 0
    maximum = 0

    async def execute(name, arguments, call_id):
        nonlocal active, maximum
        executed.append((name, call_id))
        if name == 'browser.snapshot':
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return {'observation': {'id': 'obs'}}
        return {'status': 'queued'}

    result = await Gateway(key='ci', additional_tools=Gateway.delegation_tools()).generate(
        BrowserAction, {}, tool_executor=execute)
    assert result.value.kind == 'finish'
    assert len(requests) == 2 and remaining == []
    assert maximum == 1
    assert [entry for entry in executed if entry[0] == 'browser.snapshot'] == [
        ('browser.snapshot', 'browser-1'), ('browser.snapshot', 'browser-2')]
    assert ('agent.delegate', 'worker-1') in executed


async def test_additional_tool_batch_is_validated_before_execution(offline_gateway):
    bad = _delegate_arguments()
    del bad['constraints']
    requests, remaining = offline_gateway([_provider_response(None, calls=[
        _delegate_tool_call('valid', _delegate_arguments('valid')),
        _delegate_tool_call('bad', bad)], finish='tool_calls')])
    executed = []

    async def execute(*args):
        executed.append(args)

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='ci', additional_tools=Gateway.delegation_tools()).generate(
            Locator, {}, tool_executor=execute)
    assert raised.value.category == 'tool_protocol'
    assert executed == []
    assert len(requests) == 1 and remaining == []
    assert raised.value.details['retryable'] is False


def test_gateway_default_output_and_tool_round_limits():
    gateway = Gateway(key='test-only-not-a-secret')

    assert gateway.max_output_tokens == 20480
    assert gateway.max_tool_rounds == 40


async def test_gateway_validates_structured_output_and_accounts_actual_usage(offline_gateway):
    calls=[];usage=[];responses=[]
    actual_usage = {'prompt_tokens': 11, 'completion_tokens': 6, 'total_tokens': 17}
    requests, remaining = offline_gateway([
        _provider_response(usage=actual_usage, reasoning='MUST_NOT_RETURN')])
    gateway=Gateway(key='test-only-not-a-secret')
    result=await gateway.generate(BrowserAction,{'goal':'verify'},agent_instructions='Always verify persistence.',
        on_attempt=lambda *x:calls.append(x[0]),on_response=lambda exchange,raw:responses.append(raw),on_usage=usage.append)
    assert result.value.kind=='finish'
    assert usage==[actual_usage] and calls==['deepseek-chat']
    assert result.usage == actual_usage
    assert result.model_revision == 'test-revision'
    assert len(requests) == 1 and remaining == []
    assert requests[0]['thinking'] == {'type': 'enabled'}
    assert any(tool['function']['name'] == 'BrowserSnapshot' for tool in requests[0]['tools'])
    assert 'MUST_NOT_RETURN' not in repr(result)
    assert responses[0]['body']['choices'][0]['message']['reasoning_content']=='MUST_NOT_RETURN'


async def test_gateway_missing_key_is_structured_configuration_error(offline_gateway):
    requests, remaining = offline_gateway([])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='').generate(BrowserAction, {})
    assert raised.value.status == 'FAILED'
    assert raised.value.category == 'configuration'
    assert raised.value.details['category'] == 'configuration'
    assert requests == [] and remaining == []


async def test_gateway_sends_project_instructions_and_redacted_audit_request(offline_gateway):
    requests=[]
    sent, remaining = offline_gateway([_provider_response()])
    await Gateway(key='must-not-persist').generate(BrowserAction,{},agent_instructions='Keep the patch minimal.',
        on_attempt=lambda model,request,attempt: requests.append(request))
    assert requests[0]['headers']['Authorization']=='[REDACTED]'
    assert 'must-not-persist' not in json.dumps(requests[0])
    assert '<project_instructions>\nKeep the patch minimal.\n</project_instructions>' in sent[0]['messages'][0]['content']
    assert len(sent) == 1 and remaining == []


async def test_gateway_corrects_typed_output_with_audited_usage_without_execution(offline_gateway):
    first_usage = {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}
    second_usage = {'prompt_tokens': 11, 'completion_tokens': 10, 'total_tokens': 21}
    requests, remaining = offline_gateway([
        _provider_response('{"kind":"observe"}', usage=first_usage),
        _provider_response(usage=second_usage),
    ])
    attempts, usages, audits = [], [], []
    result = await Gateway(key='ci', max_attempts=2).generate(BrowserAction, {},
        tool_executor=lambda *args: pytest.fail('无效的最终动作不能执行'),
        on_attempt=lambda model, request, number: attempts.append(request) or request,
        on_usage=usages.append, on_response=lambda exchange, raw: audits.append(raw))
    assert result.value.kind == 'finish'
    assert len(requests) == len(attempts) == len(audits) == 2 and remaining == []
    assert usages == [first_usage, second_usage]
    assert result.usage['total_tokens'] == 41
    assert [attempt['attempt'] for attempt in attempts] == [1, 2]
    assert attempts[0]['logical_exchange_id'] == attempts[1]['logical_exchange_id']
    assert [audit['logical_exchange_id'] for audit in audits] == [
        attempts[0]['logical_exchange_id']] * 2
    assert 'finish' in requests[1]['messages'][-1]['content']


async def test_gateway_rejects_truncated_json_without_retry_or_execution(offline_gateway):
    actual_usage = {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}
    requests, remaining = offline_gateway([
        _provider_response('{"kind":', usage=actual_usage, finish='length'),
        _provider_response(),
    ])
    errors, usages, audits = [], [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {},
            tool_executor=lambda *args: pytest.fail('截断输出不能执行工具'),
            on_error=lambda exchange, error: errors.append(error), on_usage=usages.append,
            on_response=lambda exchange, raw: audits.append(raw))
    assert raised.value.category == 'incomplete_output'
    assert len(requests) == len(errors) == len(audits) == 1
    assert len(remaining) == 1
    assert usages == [actual_usage]
    assert audits[0]['body']['choices'][0]['message']['content'] == '{"kind":'
    assert errors[0]['billing_status'] == 'known'
    assert errors[0]['retryable'] is False and errors[0]['will_retry'] is False


async def test_gateway_persists_connection_error_without_retry(offline_gateway):
    errors=[]
    requests, remaining = offline_gateway([httpx.ConnectError('offline'), _provider_response()])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction,{},
            on_attempt=lambda *args:{'exchange_id':'model_test'},
            on_error=lambda exchange,error:errors.append((exchange,error)))
    assert raised.value.category == 'network'
    assert len(requests) == len(errors) == 1 and len(remaining) == 1
    assert errors[0][0]['exchange_id']=='model_test'
    assert errors[0][1]['cause_type']=='ConnectError'
    assert errors[0][1]['status']=='WAITING_NETWORK'
    assert errors[0][1]['request_status']=='not_sent'
    assert errors[0][1]['retryable'] is False
    assert errors[0][1]['will_retry'] is False


async def test_gateway_records_retry_after_without_automatic_network_retry(offline_gateway):
    requests, remaining = offline_gateway([
        httpx.Response(429, headers={'Retry-After': '0.25'}, json={'error': {'type': 'rate_limit'}}),
        _provider_response(),
    ])
    attempts, errors, audits = [], [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_retry_delay=1).generate(BrowserAction, {},
            on_attempt=lambda model, request, number: attempts.append(request) or request,
            on_error=lambda exchange, error: errors.append(error),
            on_response=lambda exchange, raw: audits.append(raw))
    assert raised.value.category == 'rate_limit'
    assert len(requests) == len(attempts) == len(errors) == len(audits) == 1
    assert len(remaining) == 1
    assert audits[0]['headers']['retry-after'] == '0.25'
    assert audits[0]['body'] == {'error': {'type': 'rate_limit'}}
    assert errors[0]['status'] == 'FAILED'
    assert errors[0]['retryable'] is False and errors[0]['will_retry'] is False
    assert errors[0]['logical_exchange_id']
    assert errors[0]['logical_exchange_id'] == attempts[0]['logical_exchange_id']
    assert audits[0]['logical_exchange_id'] == attempts[0]['logical_exchange_id']


async def test_gateway_service_unavailable_never_retries_for_retry_delay_setting(offline_gateway):
    requests, remaining = offline_gateway([
        httpx.Response(503, headers={'Retry-After': '10'}, json={'error': 'busy'}),
        _provider_response(),
    ])
    errors, audits = [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci', max_retry_delay=0.5).generate(BrowserAction, {},
            on_error=lambda exchange, error: errors.append(error),
            on_response=lambda exchange, raw: audits.append(raw))
    assert raised.value.category == 'service_unavailable'
    assert len(requests) == len(errors) == len(audits) == 1 and len(remaining) == 1
    assert audits[0]['headers']['retry-after'] == '10'
    assert errors[0]['http_status'] == 503
    assert errors[0]['retryable'] is False and errors[0]['will_retry'] is False


async def test_gateway_timeout_never_retries_unknown_billing(offline_gateway):
    requests, remaining = offline_gateway([
        httpx.ReadTimeout('upstream did not answer'), _provider_response()])
    errors = []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {},
            on_error=lambda exchange, error: errors.append(error))

    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert len(requests) == len(errors) == 1 and len(remaining) == 1
    assert errors[0]['status'] == 'UNKNOWN_OPERATION'
    assert errors[0]['billing_status'] == 'unknown'
    assert errors[0]['retryable'] is False
    assert errors[0]['requires_manual_review'] is True


async def test_gateway_rejects_malformed_provider_response_without_retry(offline_gateway):
    errors = []
    audits, usages = [], []
    requests, remaining = offline_gateway([
        httpx.Response(200, json={'usage': {'total_tokens': 1}, 'choices': []}),
        _provider_response(),
    ])
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {},
            tool_executor=lambda *args: pytest.fail('畸形响应不能执行工具'),
            on_error=lambda exchange, error: errors.append(error), on_usage=usages.append,
            on_response=lambda exchange, raw: audits.append(raw))
    assert raised.value.category == 'malformed_response'
    assert len(requests) == len(errors) == len(audits) == 1 and len(remaining) == 1
    assert audits[0]['body']['choices'] == []
    assert usages == [{'total_tokens': 1}]
    assert errors[0]['message'] == '模型响应必须包含一个完整 choice'
    assert errors[0]['status'] == 'FAILED'
    assert errors[0]['retryable'] is False and errors[0]['will_retry'] is False


@pytest.mark.parametrize('payload, message', [
    ([], '模型响应必须为对象'),
    ('invalid provider response', '模型响应必须为对象'),
    (None, '模型响应必须为对象'),
    ({'usage': {'total_tokens': 1}, 'choices': [1]}, '模型 choice 必须为对象'),
])
async def test_gateway_rejects_non_object_provider_payloads_without_retry(offline_gateway, payload, message):
    requests, remaining = offline_gateway([httpx.Response(200,
        content=json.dumps(payload), headers={'Content-Type': 'application/json'}),
        _provider_response()])
    errors, audits = [], []
    with pytest.raises(ModelError) as raised:
        await Gateway(key='ci').generate(BrowserAction, {},
            tool_executor=lambda *args: pytest.fail('非对象 choice 不能执行工具'),
            on_error=lambda exchange, error: errors.append(error),
            on_response=lambda exchange, raw: audits.append(raw))
    assert raised.value.category == 'malformed_response'
    assert len(requests) == len(errors) == len(audits) == 1 and len(remaining) == 1
    assert audits[0]['body'] == payload
    assert errors[0]['message'] == message
    assert errors[0]['retryable'] is False and errors[0]['will_retry'] is False


async def test_gateway_output_correction_keeps_logical_exchange_and_usage(offline_gateway):
    requests, remaining = offline_gateway([
        _provider_response('{"kind":"invalid"}', usage={
            'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}),
        _provider_response(usage={'prompt_tokens': 5, 'completion_tokens': 2,
                                  'total_tokens': 7}),
    ])
    attempts, usages = [], []
    result = await Gateway(key='ci', max_attempts=2).generate(BrowserAction, {},
        on_attempt=lambda model, request, number: attempts.append(request),
        on_usage=usages.append)
    assert result.value.kind == 'finish'
    assert len(requests) == len(attempts) == 2 and remaining == []
    assert attempts[0]['logical_exchange_id'] == attempts[1]['logical_exchange_id']
    assert [request['attempt'] for request in attempts] == [1, 2]
    assert [usage['total_tokens'] for usage in usages] == [4, 7]
    assert result.usage['total_tokens'] == 11
    assert requests[1]['messages'] != requests[0]['messages']


async def test_gateway_output_correction_stops_at_configured_attempt_bound(offline_gateway):
    requests, remaining = offline_gateway([
        _provider_response('{"kind":"invalid"}', usage={
            'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}),
        _provider_response('{"kind":"invalid"}', usage={
            'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}),
        _provider_response(),
    ])
    executed = []
    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='ci', max_attempts=2).generate(BrowserAction, {},
            tool_executor=lambda *arguments: executed.append(arguments))
    assert len(requests) == 2 and len(remaining) == 1 and executed == []
    assert raised.value.category == 'output_validation'
    assert raised.value.details['usage']['total_tokens'] == 11
    assert raised.value.details['will_retry'] is False


async def test_teacher_locator_that_repeatedly_cannot_bind_ends_loop_detected(tmp_path):
    engine,state=make_engine(tmp_path)
    state.mode = 'test'
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
