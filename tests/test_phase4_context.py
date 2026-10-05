import copy
import json

import httpx
import pytest

from tracefix.knowledge.assembler import ContextAssembler, ContextWindowError, TokenCounter
from tracefix.knowledge.workset import expand_reference
from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import BrowserAction, Phase, digest
from tracefix.runtime.tools import ToolRegistry, ToolSpec
from tracefix.storage.artifacts import Artifacts


pytestmark = pytest.mark.usefixtures('json_completion_transport')


def completion(call_id=None):
    message = {'role': 'assistant', 'content': '{"kind":"finish"}'}
    if call_id:
        message = {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': call_id, 'type': 'function', 'function': {
                'name': 'BrowserSnapshot', 'arguments': '{}'}}]}
    return httpx.Response(200, json={'model': 'fixture', 'usage': {'total_tokens': 3},
        'choices': [{'finish_reason': 'tool_calls' if call_id else 'stop', 'message': message}]})


def registry():
    tools = ToolRegistry()
    tools.register(ToolSpec('context.expand', 'Expand a current Run artifact.',
        {'type': 'object', 'properties': {}, 'additionalProperties': False},
        phases=frozenset({Phase.EXPLORE}), side_effect='read'))
    return tools


def assembler(window=48000):
    counter = TokenCounter(name='utf8_upper_bound')
    counter.encoder = None
    counter.exact = False
    return ContextAssembler(context_window=window, output_tokens=1000,
                            overhead_tokens=1000, counter=counter)


def transport(monkeypatch, responses):
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        return responses.pop(0)

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    return requests


def persistence(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    records = []

    def save(exchange, record):
        content = record['message']['content']
        reference = artifacts.put('scope', 'run', {'scope_id': 'scope', 'run_id': 'run',
            'tool_result': record, 'tool_content': content}, label='工具结果')
        records.append(record)
        return {'result_ref': reference, 'content_hash': digest(content),
                'expandable': len(content) <= 16000 and len(content.splitlines()) <= 200,
                'binding': {'scope_id': 'scope', 'source_manifest': None,
                            'patch_hash': None, 'environment_digest': None,
                            'test_spec_hash': None}}

    return artifacts, records, save


async def test_full_payload_projects_old_results_and_resumes_without_execution(tmp_path, monkeypatch):
    requests = transport(monkeypatch, [completion(f'call-{index}') for index in range(6)]
                         + [completion('call-0'), completion()])
    artifacts, records, save = persistence(tmp_path)
    audits, manifests, executed = [], [], []
    budget = assembler()

    async def execute(name, arguments, call_id):
        executed.append(call_id)
        return {'executed': True, 'observation_ref': 'page.json',
            'observation': {'id': call_id, 'snapshot': 'prefix\n' + 'x' * 6500
                + '\nMIDDLE_EVIDENCE\n' + 'y' * 1000}}

    await Gateway(key='fixture').generate(BrowserAction, {}, tool_registry=registry(),
        tool_executor=execute, on_tool_result=save, context_assembler=budget,
        on_attempt=lambda model, request, attempt: audits.append(copy.deepcopy(request)),
        on_context=lambda manifest, compacted: manifests.append((manifest, compacted)))

    assert executed == [f'call-{index}' for index in range(6)]
    assert records[-1]['reused'] is True
    assert audits[1]['tool_result_refs']['call-0']['expandable'] is True
    assert all(Gateway._payload_tokens(budget.counter, request) <= budget.available
               for request in requests)
    projected_audits = [audit for audit in audits if audit.get('unprojected_messages')]
    assert projected_audits
    assert any(compacted for manifest, compacted in manifests)
    latest = projected_audits[-1]
    for message in latest['unprojected_messages']:
        if message.get('role') != 'tool':
            continue
        metadata = latest['tool_result_refs'][message['tool_call_id']]
        expanded = expand_reference(artifacts, 'scope', 'run', metadata['result_ref'],
                                    channel='tool_content', expected_hash=metadata['content_hash'])
        assert expanded['text'] == message['content']
        assert 'MIDDLE_EVIDENCE' in expanded['text']
        assert expanded['coverage'] == 'range'
    assert Gateway._completed_history(latest['unprojected_messages'], requests[0]['tools'])

    resumed_requests = transport(monkeypatch, [completion('call-0'), completion()])

    async def no_execution(*arguments):
        pytest.fail('completed tool must not execute during resume')

    await Gateway(key='fixture').generate(BrowserAction, {}, tool_registry=registry(),
        tool_executor=no_execution, on_tool_result=save, context_assembler=budget,
        messages=latest['unprojected_messages'], tool_result_refs=latest['tool_result_refs'],
        preserve_resumed_request=True)
    assert all(Gateway._payload_tokens(budget.counter, request) <= budget.available
               for request in resumed_requests)


@pytest.mark.parametrize('case', ['no_ref', 'no_expand', 'long_line', 'wrong_hash'])
async def test_history_without_expandable_evidence_stops_before_sending(tmp_path, monkeypatch, case):
    requests = transport(monkeypatch, [completion(f'call-{index}') for index in range(8)]
                         + [completion()])
    artifacts, records, save = persistence(tmp_path)
    budget = assembler(48000)

    async def execute(name, arguments, call_id):
        return {'executed': True, 'observation': {
            'snapshot': 'x' * (80000 if case == 'long_line' else 7500)}}

    def persist(exchange, record):
        metadata = save(exchange, record)
        if case == 'no_ref':
            return None
        if case == 'wrong_hash':
            metadata.update(content_hash='wrong', expandable=True)
        return metadata

    with pytest.raises(ContextWindowError):
        await Gateway(key='fixture').generate(BrowserAction, {}, tool_executor=execute,
            tool_registry=None if case == 'no_expand' else registry(),
            on_tool_result=persist, context_assembler=budget)
    assert (len(requests) == 1 if case == 'long_line' else 1 < len(requests) < 8)
    assert len(records) == len(requests)
    assert all(Gateway._payload_tokens(budget.counter, request) <= budget.available
               for request in requests)


async def test_resume_checks_budget_and_preserves_unchanged_request(monkeypatch):
    original = [{'role': 'system', 'content': 'fixed'}, {'role': 'user', 'content': 'fixed'}]
    requests = transport(monkeypatch, [completion()])
    await Gateway(key='fixture').generate(BrowserAction, {}, messages=original,
        preserve_resumed_request=True, context_assembler=assembler())
    assert requests[0]['messages'] == original
    original[0]['content'] = 'x' * 100000
    with pytest.raises(ContextWindowError):
        await Gateway(key='fixture').generate(BrowserAction, {}, messages=original,
            preserve_resumed_request=True, context_assembler=assembler())
    assert len(requests) == 1


@pytest.mark.parametrize('failure', [
    {'isError': True, 'error': {'message': 'business failure'}},
    {'operation_status': 'UNKNOWN_OPERATION', 'operation_id': 'original-operation'},
    {'business_outcome': {'passed': False, 'assertions': ['reverse action failed']}},
    {'executed': False},
    {'unrecognized_result': 'keep original structure'},
])
def test_failed_unknown_or_unrecognized_results_are_never_projected(tmp_path, failure):
    artifacts, records, save = persistence(tmp_path)
    original = {'executed': True, 'observation': {'snapshot': 'x' * 7000}, **failure}
    message = {'role': 'tool', 'name': 'BrowserSnapshot', 'tool_call_id': 'original-call',
               'content': json.dumps(original)}
    metadata = save(None, {'message': message})
    saved = copy.deepcopy(message)
    assert Gateway._project_tool_message(message, metadata) is False
    assert message == saved


def test_projection_keeps_receipt_references_and_latest_complete_batch(tmp_path):
    artifacts, records, save = persistence(tmp_path)
    original = {'executed': True, 'operation_id': 'original-operation',
        'receipt': {'status': 'completed', 'operation_id': 'original-operation'},
        'artifact_ref': 'original-artifact.json', 'observation_ref': 'original-page.json',
        'observation': {'id': 'original-page', 'page_generation': 3, 'snapshot': 'x' * 7000}}
    message = {'role': 'tool', 'name': 'BrowserSnapshot', 'tool_call_id': 'original-call',
               'content': json.dumps(original)}
    metadata = save(None, {'message': message})
    assert Gateway._project_tool_message(message, metadata)
    projected = json.loads(message['content'])
    for field in ('receipt', 'operation_id', 'artifact_ref', 'observation_ref', 'executed'):
        assert projected[field] == original[field]
    assert projected['observation']['page_generation'] == 3
    assert projected['tool_history_ref']['result_ref'] == metadata['result_ref']
    messages = [completion('original-call').json()['choices'][0]['message'], message,
        {'role': 'assistant', 'tool_calls': [{'id': 'new-one'}, {'id': 'new-two'}]},
        {'role': 'tool', 'tool_call_id': 'new-one'}, {'role': 'tool', 'tool_call_id': 'new-two'}]
    assert Gateway._tool_history_candidates(messages) == [1]


async def test_context_wrapping_is_measured_after_optional_projection(monkeypatch):
    requests = transport(monkeypatch, [completion()])
    budget = assembler(26000)
    context = {'goal': '保持取消完成', 'recent_action_results': [
        {'output': '\\"' * 18000}], 'test_spec': {'reverse': 'must remain available'}}
    await Gateway(key='fixture').generate(BrowserAction, context, context_assembler=budget)
    assert len(requests) == 1
    assert Gateway._payload_tokens(budget.counter, requests[0]) <= budget.available
    sent = json.loads(requests[0]['messages'][1]['content'])['context']
    assert sent['goal'] == context['goal']
    assert sent['test_spec'] == context['test_spec']
