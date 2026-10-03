import copy
import json

import httpx
import pytest

from tracefix.model.gateway import BrowserPolicyRouter, Gateway, ModelError, ModelOutputError, ModelResult
from tracefix.runtime.contracts import (BrowserAction, Decision, FileEdit, Outcome,
    PatchProposal, RunStatus, digest)
from tracefix.runtime import smoke
from tracefix.runtime.smoke import make_engine


pytestmark = pytest.mark.usefixtures('json_completion_transport')


def completion(*, calls=None, content='{"kind":"finish"}', usage=1, status=200):
    message = {'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    return httpx.Response(status, json={
        'usage': {'total_tokens': usage},
        'choices': [{'finish_reason': 'tool_calls' if calls is not None else 'stop',
                     'message': message}],
    })


def tool_call(call_id='call-first', name='BrowserSnapshot', arguments=None):
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


@pytest.mark.parametrize('invalid_kind', ['unchanged', 'blank', 'stale_hash', 'duplicate', 'evidence'])
async def test_invalid_patch_is_corrected_before_workspace_operation(tmp_path, invalid_kind, monkeypatch):
    stable_content = b'export const unchanged = 1;\n'
    original_index = smoke.safe_index_files

    def initialize_stable_source(root, paths):
        (root / 'src/stable.ts').write_bytes(stable_content)
        return original_index(root, [*paths, 'src/stable.ts'])

    monkeypatch.setattr(smoke, 'safe_index_files', initialize_stable_source)
    engine, state = make_engine(tmp_path)
    original = engine.workspace.read('src/value.ts')
    stable_path = engine.workspace.root / 'src/stable.ts'
    assert engine.source['files']['src/stable.ts'] == digest(stable_content)
    assert engine.source['entries']['src/stable.ts']['tracked'] is True
    engine.workspace.check_frozen(engine.source)
    inner = engine.model
    contexts = []

    class InvalidPatchOnce:
        async def generate(self, schema, context, **kwargs):
            if schema is not PatchProposal:
                return await inner.generate(schema, context, **kwargs)
            contexts.append(context)
            assert engine.workspace.read('src/value.ts') == original
            assert stable_path.read_bytes() == stable_content
            if len(contexts) > 1:
                return await inner.generate(schema, context, **kwargs)
            proposal = PatchProposal(summary='invalid candidate',
                evidence_refs=context['evidence_refs'][:1], edits=[FileEdit(
                    path='src/value.ts', before_hash=digest(original.encode()),
                    content='export const persisted = true;\n')])
            if invalid_kind == 'unchanged':
                proposal.edits.append(FileEdit(path='src/stable.ts',
                    before_hash=digest(stable_content), content=stable_content.decode()))
            elif invalid_kind == 'blank':
                proposal.edits[0].content = ' \n'
            elif invalid_kind == 'stale_hash':
                proposal.edits[0].before_hash = '0' * 64
            elif invalid_kind == 'duplicate':
                proposal.edits.append(proposal.edits[0].model_copy())
            else:
                proposal.evidence_refs = ['invented.json']
            exchange = kwargs['on_attempt']('FAKE-CI', {'json': {}}, 1)
            usage = {'total_tokens': 1}
            kwargs['on_usage'](usage)
            kwargs['on_response'](exchange, {'http_status': 200, 'body': {
                'usage': usage, 'choices': [{'finish_reason': 'stop',
                    'message': {'content': proposal.model_dump_json()}}]}})
            return ModelResult(proposal, usage, 'FAKE-CI', 'stop')

    engine.model = InvalidPatchOnce()
    await engine.run(state)
    saved = engine.store.load(state.run_id, state.scope_id)
    events = engine.store.trace(state.run_id, state.scope_id)
    operations = [event for event in events if event['type'] == 'tool.started'
                  and ':workspace.patch:' in event['payload'].get('operation_id', '')]

    assert len(contexts) == 2
    assert contexts[1]['runtime_feedback'].startswith('ModelOutputError:')
    assert saved.run_status == RunStatus.WAITING_APPROVAL, saved.error
    assert saved.outcome == Outcome.FIX_VERIFIED
    assert saved.budget.patches == 1
    assert len(operations) == 1
    assert not any(event['type'] == 'run.error'
                   and event['payload'].get('status') == RunStatus.PAUSED for event in events)


async def test_batch_noop_patch_followups_enrich_context_and_eventually_verify(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    original = engine.workspace.read('src/value.ts')
    inner = engine.model
    contexts, card_limits = [], []
    original_cards = engine.workspace.cards

    def record_cards(limit_chars=60_000, preferred_paths=()):
        card_limits.append(limit_chars)
        return original_cards(limit_chars=limit_chars, preferred_paths=preferred_paths)

    class NoopTwice:
        async def generate(self, schema, context, **kwargs):
            if schema is not PatchProposal:
                return await inner.generate(schema, context, **kwargs)
            contexts.append(copy.deepcopy(context))
            result = await inner.generate(schema, context, **kwargs)
            if len(contexts) <= 2:
                result.value.edits[0].content = original
                result.value.summary = 'No code change is needed'
            return result

    engine.workspace.cards = record_cards
    engine.model = NoopTwice()
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.run_status == RunStatus.COMPLETED, finished.error
    assert finished.outcome == Outcome.FIX_VERIFIED
    assert len(contexts) == 3
    assert card_limits == [60_000, 120_000, 180_000]
    assert [context['diagnosis_retry_count'] for context in contexts] == [0, 1, 2]
    assert len(contexts[1]['diagnosis_feedback']) == 1
    assert len(contexts[2]['diagnosis_feedback']) == 2
    assert 'No code change is needed' in json.dumps(contexts[2]['diagnosis_feedback'])
    assert contexts[1]['observation']['snapshot']
    assert 'replay_plan' in contexts[1]
    assert 'current_workspace_diff' in contexts[1]
    assert contexts[1]['runtime_feedback'].startswith('ModelOutputError:')
    assert len(finished.diagnosis_feedback_refs) == 2
    assert finished.diagnosis_retry_count == 0
    report = engine.get(finished, finished.report_ref)
    assert report['patch_available'] is True
    assert report['patch_verification'] == 'verified'


async def test_batch_repeated_noop_patch_emits_exhausted_report_and_empty_diff(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    original = engine.workspace.read('src/value.ts')
    inner = engine.model
    contexts = []

    class NoopForever:
        async def generate(self, schema, context, **kwargs):
            result = await inner.generate(schema, context, **kwargs)
            if schema is PatchProposal:
                contexts.append(copy.deepcopy(context))
                result.value.edits[0].content = original
            return result

    engine.model = NoopForever()
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert len(contexts) == engine.DIAGNOSIS_RETRY_LIMIT
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.REPAIR_EXHAUSTED
    assert finished.diagnosis_retry_count == engine.DIAGNOSIS_RETRY_LIMIT
    assert len(finished.diagnosis_feedback_refs) == engine.DIAGNOSIS_RETRY_LIMIT
    assert finished.budget.patches == 0
    assert engine.workspace.read('src/value.ts') == original
    report = engine.get(finished, finished.report_ref)
    assert report['patch_available'] is False
    assert report['patch_verification'] == 'none'
    assert engine.artifacts.read(state.scope_id, state.run_id, report['patch_diff_ref']) == b''
    assert report['error_details']['terminal_reason'] == 'diagnosis_retry_limit'
    assert report['result_summary']
    assert not any(event['type'] == 'state.changed'
                   and event['payload']['status'] == RunStatus.PAUSED for event in
                   engine.store.trace(state.run_id, state.scope_id))


async def test_batch_patch_failure_with_unknown_receipt_is_never_replayed(tmp_path):
    engine, state = make_engine(tmp_path)
    state.execution_mode = 'batch'
    original_apply = engine.workspace.apply
    attempts = []

    def write_then_fail(proposal, base='HEAD'):
        attempts.append(proposal)
        original_apply(proposal, base=base)
        raise ValueError('Patch was written but the receipt was lost')

    engine.workspace.apply = write_then_fail
    await engine.run(state)

    finished = engine.store.load(state.run_id, state.scope_id)
    assert len(attempts) == 1
    assert finished.run_status == RunStatus.FAILED
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['status'] == 'UNKNOWN_OPERATION'
    assert finished.error_details['operation_id']
    report = engine.get(finished, finished.report_ref)
    assert report['patch_available'] is True
    assert report['patch_verification'] == 'unverified'
    assert 'persisted = true' in engine.artifacts.read(
        state.scope_id, state.run_id, report['patch_diff_ref']).decode()
