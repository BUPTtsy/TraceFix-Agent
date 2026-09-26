import copy
import json

import httpx
import pytest

from tracefix.model.gateway import BrowserPolicyRouter, Gateway, ModelError, ModelOutputError, ModelResult
from tracefix.runtime.contracts import BrowserAction, Decision, RunStatus, digest
from tracefix.runtime.smoke import make_engine


def completion(*, calls=None, content='{"kind":"finish"}', usage=1, status=200):
    message = {'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    return httpx.Response(status, json={
        'usage': {'total_tokens': usage},
        'choices': [{'finish_reason': 'tool_calls' if calls is not None else 'stop',
                     'message': message}],
    })


def tool_call(call_id='call-first', name='browser_snapshot', arguments=None):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments or {})}}


def mock_responses(monkeypatch, responses):
    requests, sleeps = [], []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr('tracefix.model.gateway.asyncio.sleep', sleep)
    return requests, sleeps


@pytest.mark.parametrize('calls', [[{'id': 'missing-function'}], ['invalid-call'], {'bad': 'shape'}])
async def test_invalid_tool_envelopes_report_terminal_error(monkeypatch, calls):
    requests, sleeps = mock_responses(monkeypatch, [completion(calls=calls)])
    errors, usage = [], []
    with pytest.raises(ModelOutputError):
        await Gateway(key='ci', tool_mode='native').generate(
            BrowserAction, {}, on_error=lambda exchange, error: errors.append(error),
            on_usage=usage.append)
    assert len(requests) == 1
    assert sleeps == []
    assert usage == [{'total_tokens': 1}]
    assert len(errors) == 1
    assert errors[0]['category'] == 'tool_protocol'
    assert errors[0]['will_retry'] is False
    assert errors[0]['tool_round'] == 0
    assert errors[0]['attempt'] == 1


async def test_round_limit_records_error_and_usage_without_extra_execution(monkeypatch):
    mock_responses(monkeypatch, [completion(calls=[tool_call()]),
                                completion(calls=[tool_call('call-next')], usage=2)])
    executed, errors, usage = [], [], []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'ok': True}

    with pytest.raises(ModelOutputError):
        await Gateway(key='ci', tool_mode='native', max_tool_rounds=1).generate(
            BrowserAction, {}, tool_executor=execute,
            on_error=lambda exchange, error: errors.append(error), on_usage=usage.append)
    assert executed == ['call-first']
    assert usage == [{'total_tokens': 1}, {'total_tokens': 2}]
    assert len(errors) == 1
    assert errors[0]['category'] == 'tool_protocol'
    assert errors[0]['tool_round'] == 1


async def test_unknown_tool_execution_is_not_converted_to_recoverable_feedback(monkeypatch):
    requests, sleeps = mock_responses(monkeypatch, [completion(calls=[tool_call()]), completion()])
    failure = ModelOutputError('execution acknowledgement lost', status='UNKNOWN_OPERATION',
                               category='tool_execution', details={'requires_manual_review': True})
    errors = []

    async def execute(name, arguments, call_id):
        raise failure

    with pytest.raises(ModelOutputError) as raised:
        await Gateway(key='ci', tool_mode='native').generate(
            BrowserAction, {}, tool_executor=execute,
            on_error=lambda exchange, error: errors.append(error))
    assert raised.value is failure
    assert len(requests) == 1
    assert sleeps == []
    assert len(errors) == 1
    assert errors[0]['status'] == 'UNKNOWN_OPERATION'


async def test_output_correction_preserves_executed_tools_and_rejected_answer(monkeypatch):
    rejected = '{"kind":"navigate","value":"https://example.test"}'
    requests, sleeps = mock_responses(monkeypatch, [completion(calls=[tool_call()]),
                                                  completion(content=rejected), completion()])
    executed = []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'observation': {'id': 'observed'}}

    await Gateway(key='ci', tool_mode='native').generate(BrowserAction, {}, tool_executor=execute)
    assert executed == ['call-first']
    assert sleeps == [1]
    history = requests[-1]['messages']
    assert history[-2] == {'role': 'assistant', 'content': rejected}
    assert '工具结果' in history[-1]['content']
    assert '上一次输出未通过校验，未被执行' not in history[-1]['content']
    assert len([message for message in history if message['role'] == 'tool']) == 1


async def test_retries_are_bounded_per_request_and_do_not_reexecute_tools(monkeypatch):
    requests, sleeps = mock_responses(monkeypatch, [completion(status=503),
        completion(calls=[tool_call()]), completion(status=503), completion()])
    executed, records, usage = [], [], []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'ok': True}

    await Gateway(key='ci', tool_mode='native', max_attempts=2).generate(
        BrowserAction, {}, tool_executor=execute, on_usage=usage.append,
        on_attempt=lambda model, request, attempt: records.append(request))
    assert len(requests) == 4
    assert executed == ['call-first']
    assert sleeps == [1, 1]
    assert [(record['tool_round'], record['attempt']) for record in records] == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert len({record['logical_exchange_id'] for record in records}) == 1
    assert len(usage) == 4


async def test_student_requires_explicit_json_mode():
    class Student:
        async def generate(self, schema, context, **kwargs):
            pytest.fail('an unspecified student protocol must not run')

    class Teacher:
        async def generate(self, schema, context, **kwargs):
            return ModelResult(Decision(action=BrowserAction(kind='finish')), {'total_tokens': 1}, 'teacher', 'stop')

    result = await BrowserPolicyRouter(Teacher(), Student()).generate(Decision, {})
    assert result.model_revision == 'teacher'


async def test_engine_audit_identifies_every_tool_round(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)

    class MultiRoundModel:
        async def generate(self, schema, context, **kwargs):
            for tool_round in range(2):
                request = {'logical_exchange_id': 'round-audit', 'tool_round': tool_round, 'json': {}}
                exchange = kwargs['on_attempt']('fake', request, 1)
                kwargs['on_response'](exchange, {'http_status': 200, 'body': {'choices': []}})
            return ModelResult(Decision(action=BrowserAction(kind='finish')), {'total_tokens': 1}, 'fake', 'stop')

    engine.model = MultiRoundModel()
    await engine.model_call(state, Decision, {})
    records = [engine.get(state, ref) for ref in state.model_exchange_refs]
    assert len(set(state.model_exchange_refs)) == 4
    assert [(record['logical_exchange_id'], record.get('tool_round'), record['attempt'])
            for record in records] == [('round-audit', 0, 1), ('round-audit', 0, 1),
                                      ('round-audit', 1, 1), ('round-audit', 1, 1)]


async def test_engine_resume_does_not_reuse_unrelated_exchange(tmp_path):
    engine, state = make_engine(tmp_path)
    prior_messages = [{'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
                      {'role': 'tool', 'tool_call_id': 'call-first', 'content': '{"old":true}'}]
    prior_ref = engine.put(state, {'schema': 'Decision', 'logical_exchange_id': 'unrelated',
        'request': {'logical_exchange_id': 'unrelated', 'json': {'messages': prior_messages}}})
    state.model_exchange_refs.append(prior_ref)
    state.error_details = {'status': 'WAITING_NETWORK', 'logical_exchange_id': 'failed-current',
                           'request_status': 'not_sent', 'requires_manual_review': False}
    engine.store.save(state)

    class ResumeModel:
        supports_tool_executor = True

        async def generate(self, schema, context, **kwargs):
            assert 'messages' not in kwargs
            return ModelResult(Decision(action=BrowserAction(kind='finish')), {'total_tokens': 1}, 'fake', 'stop')

    engine.model = ResumeModel()
    await engine.model_call(state, Decision, {})


async def test_engine_records_usage_and_recovers_from_non_list_choices(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    malformed = httpx.Response(200, json={'usage': {'total_tokens': 3}, 'choices': None})
    mock_responses(monkeypatch, [malformed, completion(content='{"action":{"kind":"finish"}}', usage=4)])
    engine.model = Gateway(key='ci', tool_mode='native', max_attempts=2)
    engine.store.save(state)
    await engine.model_call(state, Decision, {})
    assert state.budget.tokens == 7
    assert state.budget.model_calls == 2


async def test_pause_resume_preserves_completed_tool_transcript(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path, bugfree=True)
    requests, sleeps = mock_responses(monkeypatch, [completion(calls=[tool_call()]),
        httpx.ConnectError('connection refused'), completion(content='{"action":{"kind":"finish"}}')])
    engine.model = Gateway(key='ci', tool_mode='native', max_attempts=1)
    await engine.run(state)
    paused = engine.store.load(state.run_id, state.scope_id)
    assert paused.run_status == RunStatus.PAUSED
    assert paused.error_details['status'] == 'WAITING_NETWORK'
    assert paused.error_details['request_status'] == 'not_sent'
    history = requests[1]['messages']
    assert any(message['role'] == 'tool' for message in history)
    await engine.run(resume='resume')
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED
    assert requests[2]['messages'] == history
    assert finished.error_details is None
    assert sleeps == []


async def test_engine_resume_selects_matching_failed_request(tmp_path):
    engine, state = make_engine(tmp_path)
    matching_messages = [{'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
                         {'role': 'tool', 'tool_call_id': 'call-first', 'content': '{"matched":true}'}]
    other_messages = copy.deepcopy(matching_messages)
    other_messages[-1]['content'] = '{"unrelated":true}'
    for exchange_id, messages in [('failed-current', matching_messages), ('unrelated', other_messages)]:
        ref = engine.put(state, {'schema': 'Decision', 'logical_exchange_id': exchange_id,
            'request': {'logical_exchange_id': exchange_id, 'json': {'messages': messages}}})
        state.model_exchange_refs.append(ref)
    state.error_details = {'status': 'WAITING_NETWORK', 'logical_exchange_id': 'failed-current',
                           'request_status': 'not_sent', 'requires_manual_review': False}
    engine.store.save(state)

    class ResumeModel:
        supports_tool_executor = True

        async def generate(self, schema, context, **kwargs):
            assert kwargs['messages'] == matching_messages
            return ModelResult(Decision(action=BrowserAction(kind='finish')), {'total_tokens': 1}, 'fake', 'stop')

    engine.model = ResumeModel()
    await engine.model_call(state, Decision, {})


@pytest.mark.parametrize('message', [None, [], {}, {'content': {'kind': 'finish'}}])
async def test_invalid_message_envelope_retries_without_emitting_invalid_history(monkeypatch, message):
    invalid = httpx.Response(200, json={'usage': {'total_tokens': 2},
        'choices': [{'finish_reason': 'stop', 'message': message}]})
    requests, sleeps = mock_responses(monkeypatch, [invalid, completion(usage=3)])
    usage, errors = [], []
    result = await Gateway(key='ci', tool_mode='native', max_attempts=2).generate(
        BrowserAction, {}, on_usage=usage.append,
        on_error=lambda exchange, error: errors.append(error))
    assert result.value.kind == 'finish'
    assert usage == [{'total_tokens': 2}, {'total_tokens': 3}]
    assert sleeps == [1]
    assert len(errors) == 1
    assert errors[0]['will_retry'] is True
    for historical in requests[-1]['messages']:
        if historical['role'] == 'assistant':
            assert historical['content'] is None or isinstance(historical['content'], str)


async def test_failed_attempts_include_full_exchange_coordinates(monkeypatch):
    mock_responses(monkeypatch, [completion(status=503), completion(calls=[tool_call()]),
                                completion(content='invalid JSON'), completion()])
    errors, responses = [], []

    async def execute(name, arguments, call_id):
        return {'ok': True}

    await Gateway(key='ci', tool_mode='native', max_attempts=2).generate(
        BrowserAction, {}, tool_executor=execute,
        on_error=lambda exchange, error: errors.append(error),
        on_response=lambda exchange, response: responses.append(response))
    assert [(error.get('tool_round'), error.get('attempt')) for error in errors] == [(0, 1), (1, 1)]
    assert [(response.get('tool_round'), response.get('attempt')) for response in responses] == [
        (0, 1), (0, 2), (1, 1), (1, 2)]
    assert len({error['logical_exchange_id'] for error in errors}) == 1


async def test_completed_history_counts_toward_tool_round_limit(monkeypatch):
    history = [{'role': 'assistant', 'content': None, 'tool_calls': [tool_call()]},
               {'role': 'tool', 'tool_call_id': 'call-first', 'content': '{"ok":true}'}]
    requests, sleeps = mock_responses(monkeypatch, [completion(calls=[tool_call('call-next')]), completion()])
    executed, errors, records = [], [], []

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'ok': True}

    with pytest.raises(ModelOutputError):
        await Gateway(key='ci', tool_mode='native', max_tool_rounds=1).generate(
            BrowserAction, {}, messages=history, tool_executor=execute,
            on_attempt=lambda model, request, attempt: records.append(request),
            on_error=lambda exchange, error: errors.append(error))
    assert executed == []
    assert len(requests) == 1
    assert records[0]['tool_round'] == 1
    assert errors[0]['tool_round'] == 1
    assert sleeps == []


async def test_loop_error_callback_preserves_unknown_operation(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    mock_responses(monkeypatch, [httpx.ReadTimeout('lost response'), httpx.ReadTimeout('lost response')])
    recorded = []
    gateway = Gateway(key='ci', tool_mode='native', max_attempts=1)
    with pytest.raises(ModelError):
        await gateway.generate(BrowserAction, {},
            on_error=lambda exchange, error: recorded.append(error))
    signature = digest([recorded[0]['type'], recorded[0]['message']])
    state.loop_error_signatures = [signature] * (engine.LOOP_ERROR_LIMIT - 1)
    engine.store.save(state)
    engine.model = gateway
    with pytest.raises(ModelError) as raised:
        await engine.model_call(state, Decision, {})
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert raised.value.details['requires_manual_review'] is True
    assert raised.value.details['request_status'] == 'unknown'


@pytest.mark.parametrize('status', ['UNKNOWN_OPERATION', 'WAITING_NETWORK'])
async def test_unknown_result_pauses_before_loop_termination(tmp_path, status):
    engine, state = make_engine(tmp_path)

    class UnknownModel:
        async def generate(self, schema, context, **kwargs):
            current = engine.store.load(state.run_id, state.scope_id)
            current.loop_no_progress_steps = engine.LOOP_NO_PROGRESS_LIMIT
            engine.store.save(current)
            raise ModelError('response lost', status=status, category='network',
                             details={'requires_manual_review': True, 'request_status': 'unknown'})

    engine.model = UnknownModel()
    await engine.run(state)
    paused = engine.store.load(state.run_id, state.scope_id)
    assert paused.run_status == RunStatus.PAUSED
    assert paused.outcome is None
    assert paused.error_details['status'] == status
    assert paused.error_details['requires_manual_review'] is True
