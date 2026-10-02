import copy
import json
from os.path import commonprefix

import httpx
import pytest

from tracefix.knowledge.context import POLICY
from tracefix.model.gateway import Gateway
from tracefix.model.prompts import COMMON, STAGE_POLICIES, serialize_request, system_instructions
from tracefix.runtime.contracts import BrowserAction


@pytest.mark.parametrize('phase', ['PREPARE', 'EXPLORE', 'DIAGNOSE'])
def test_stage_policy_length_and_selection(phase):
    policy = STAGE_POLICIES[phase]
    assert 150 <= len(policy) <= 300
    assert 150 <= sum('\u4e00' <= character <= '\u9fff' for character in policy) <= 300
    system = system_instructions(BrowserAction, {'phase': phase})
    assert policy in system
    assert all(other not in system for name, other in STAGE_POLICIES.items() if name != phase)


def test_cross_stage_and_tool_mode_share_global_and_project_prefix():
    prefix = POLICY + COMMON
    systems = [system_instructions(BrowserAction, {'phase': phase},
               agent_instructions='project policy', native_tools=native)
               for phase in STAGE_POLICIES for native in (False, True)]
    shared = commonprefix(systems)
    assert shared.startswith(prefix)
    assert '</project_instructions>' in shared


def test_stable_task_fields_precede_dynamic_observation_without_mutating_context():
    before = {'observation': {'id': 'before'}, 'goal': 'verify',
              'test_spec': {'authorized_actions': ['observe', 'finish']}}
    original = copy.deepcopy(before)
    after = {**before, 'observation': {'id': 'after'}}
    shared = commonprefix([serialize_request(BrowserAction, context) for context in (before, after)])
    assert '"authorized_actions": ["observe", "finish"]' in shared
    assert before == original


async def test_provider_refresh_uses_current_phase_and_identical_serialization(monkeypatch):
    requests = capture_requests(monkeypatch)
    current = {'phase': 'DIAGNOSE', 'goal': 'inspect', 'observation': {'id': 'fresh'}}
    await Gateway(key='ci', tool_mode='json').generate(BrowserAction,
        {'phase': 'EXPLORE'}, context_provider=lambda: current)
    assert requests[0]['messages'][1]['content'] == serialize_request(BrowserAction, current)
    assert STAGE_POLICIES['DIAGNOSE'] in requests[0]['messages'][0]['content']
    assert STAGE_POLICIES['EXPLORE'] not in requests[0]['messages'][0]['content']


def skill_context(name='inspect', observation='before'):
    return {
        'observation': {'snapshot': observation, 'id': 'observation-' + observation},
        'goal': 'verify the page',
        'skill_index': [{'name': name, 'description': name + ' description',
                         'path': name + '/SKILL.md', 'content': 'index must not expose this body'}],
        'skills': [{'name': name, 'version': '1', 'content_hash': name + '-hash',
                    'content': name + ' detailed guidance ' * 80}],
    }


def completion(*, calls=None, usage=None):
    message = {'content': '{"kind":"finish"}'}
    if calls is not None:
        message = {'content': None, 'tool_calls': calls, 'reasoning_content': 'keep reasoning'}
    return httpx.Response(200, json={
        'model': 'test-model', 'usage': usage or {'total_tokens': 3},
        'choices': [{'finish_reason': 'tool_calls' if calls is not None else 'stop',
                     'message': message}],
    })


def snapshot_call():
    return {'id': 'snapshot-1', 'type': 'function',
            'function': {'name': 'BrowserSnapshot', 'arguments': '{}'}}


def capture_requests(monkeypatch, responses=None):
    monkeypatch.setenv('TRACEFIX_STREAM', 'false')
    requests = []

    async def post(client, url, **kwargs):
        requests.append(copy.deepcopy(kwargs['json']))
        return responses.pop(0) if responses is not None else completion()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    return requests


async def test_observation_changes_keep_schema_and_skill_body_in_shared_prefix(monkeypatch):
    requests = capture_requests(monkeypatch)
    before = skill_context()
    after = skill_context(observation='after')
    gateway = Gateway(key='ci', tool_mode='json')

    await gateway.generate(BrowserAction, before)
    await gateway.generate(BrowserAction, after)

    first_text = requests[0]['messages'][1]['content']
    second_text = requests[1]['messages'][1]['content']
    shared = commonprefix([first_text, second_text])
    legacy_shared = commonprefix([
        json.dumps({'context': context, 'response_json_schema': BrowserAction.model_json_schema()},
                   ensure_ascii=False) for context in (before, after)])
    decoded = json.loads(first_text)

    assert list(decoded) == ['response_json_schema', 'context']
    assert list(decoded['context'])[0] == 'skills'
    assert decoded['context'] == before
    assert decoded['response_json_schema'] == BrowserAction.model_json_schema()
    assert before['skills'][0]['content'] in shared
    assert '"response_json_schema"' in shared
    assert len(shared) > len(legacy_shared)
    assert requests[0]['messages'][0] == requests[1]['messages'][0]


async def test_prompt_serialization_is_stable_across_dictionary_insertion_order(monkeypatch):
    requests = capture_requests(monkeypatch)
    original = skill_context()
    reordered = dict(reversed(list(original.items())))
    reordered['observation'] = dict(reversed(list(original['observation'].items())))
    reordered['skills'] = [dict(reversed(list(original['skills'][0].items())))]
    reordered['skill_index'] = [dict(reversed(list(original['skill_index'][0].items())))]
    gateway = Gateway(key='ci', tool_mode='json')

    await gateway.generate(BrowserAction, original)
    await gateway.generate(BrowserAction, reordered)

    assert requests[0]['messages'] == requests[1]['messages']
    assert list(original)[0] == 'observation'


async def test_stage_changes_preserve_fixed_system_prefix_and_limit_index_to_summaries(monkeypatch):
    requests = capture_requests(monkeypatch)
    gateway = Gateway(key='ci', tool_mode='json')
    before = skill_context('inspect')
    after = skill_context('repair')
    instructions = 'Keep changes within the approved project scope.'

    await gateway.generate(BrowserAction, before, agent_instructions=instructions)
    await gateway.generate(BrowserAction, after, agent_instructions=instructions)

    systems = [request['messages'][0]['content'] for request in requests]
    shared = commonprefix(systems)
    project_end = shared.index('</project_instructions>')
    assert shared.startswith(POLICY + COMMON)
    assert instructions in shared
    assert project_end < shared.index('<skill_index>')
    for system, context in zip(systems, (before, after)):
        index_text = system.split('<skill_index>\n', 1)[1].split('\n</skill_index>', 1)[0]
        assert json.loads(index_text) == [{
            'name': context['skill_index'][0]['name'],
            'description': context['skill_index'][0]['description'],
        }]
        assert context['skills'][0]['content'] not in system
        assert 'index must not expose this body' not in system


async def test_selected_skill_changes_retain_schema_prefix_when_index_is_unchanged(monkeypatch):
    requests = capture_requests(monkeypatch)
    before = skill_context('inspect')
    after = skill_context('repair')
    index = before['skill_index'] + after['skill_index']
    before['skill_index'] = after['skill_index'] = index
    gateway = Gateway(key='ci', tool_mode='json')

    await gateway.generate(BrowserAction, before)
    await gateway.generate(BrowserAction, after)

    assert requests[0]['messages'][0] == requests[1]['messages'][0]
    shared = commonprefix([request['messages'][1]['content'] for request in requests])
    schema_prefix = '{"response_json_schema": ' + json.dumps(
        BrowserAction.model_json_schema(), ensure_ascii=False, sort_keys=True)
    assert shared.startswith(schema_prefix + ', "context": {"skills": ')
    assert json.loads(requests[1]['messages'][1]['content'])['context']['skills'] == after['skills']


@pytest.mark.parametrize('skill_change', ['keep', 'replace', 'remove'])
async def test_native_refresh_updates_index_and_body_and_keeps_image_and_tool_pair(monkeypatch, skill_change):
    requests = capture_requests(monkeypatch, [completion(calls=[snapshot_call()]), completion()])
    initial = skill_context('outdated')
    current = skill_context('inspect')
    after = skill_context('inspect' if skill_change == 'keep' else 'repair', observation='fresh')
    if skill_change == 'remove':
        after['skill_index'] = []
        after['skills'] = []

    async def execute(name, arguments, call_id):
        nonlocal current
        current = after
        return {'observation': after['observation']}

    await Gateway(key='ci', vision_model='vision').generate(
        BrowserAction, initial, image=b'image-content',
        context_provider=lambda: current, tool_executor=execute)

    first_messages = requests[0]['messages']
    final_messages = requests[1]['messages']
    assert json.loads(first_messages[1]['content'][0]['text'])['context'] == skill_context('inspect')
    assert json.loads(final_messages[1]['content'][0]['text'])['context'] == after
    assert first_messages[1]['content'][1] == final_messages[1]['content'][1]
    assert 'outdated' not in first_messages[0]['content']
    if skill_change != 'keep':
        assert 'inspect description' not in final_messages[0]['content']
    assert ('<skill_index>' in final_messages[0]['content']) == (skill_change != 'remove')
    if skill_change == 'replace':
        assert 'repair description' in final_messages[0]['content']
    if skill_change == 'keep':
        assert first_messages[0] == final_messages[0]
        shared = commonprefix([messages[1]['content'][0]['text']
                               for messages in (first_messages, final_messages)])
        assert after['skills'][0]['content'] in shared
    assert final_messages[2]['tool_calls'] == [snapshot_call()]
    assert final_messages[2]['reasoning_content'] == 'keep reasoning'
    assert final_messages[3]['tool_call_id'] == 'snapshot-1'
    assert json.loads(final_messages[3]['content'])['observation'] == after['observation']
    assert requests[0]['tools'] == requests[1]['tools']


@pytest.mark.parametrize('use_provider', [False, True])
async def test_resumed_prompt_refreshes_only_gateway_messages_and_reuses_tool_result(monkeypatch, use_provider):
    requests = capture_requests(monkeypatch, [completion(), completion(calls=[snapshot_call()]), completion()])
    gateway = Gateway(key='ci')
    before = skill_context('inspect')
    after = skill_context('repair', observation='fresh')
    await gateway.generate(BrowserAction, before)
    history = copy.deepcopy(requests[0]['messages']) + [
        {'role': 'assistant', 'content': None, 'tool_calls': [snapshot_call()],
         'reasoning_content': 'restored reasoning'},
        {'role': 'tool', 'name': 'BrowserSnapshot', 'tool_call_id': 'snapshot-1',
         'content': '{"observation":{"id":"saved"}}'},
        {'role': 'user', 'content': 'Preserve this final-output correction.'},
    ]
    original_history = copy.deepcopy(history)

    async def execute(*args):
        pytest.fail('a completed tool result must be reused')

    await gateway.generate(BrowserAction, before if use_provider else after,
        messages=history, tool_executor=execute,
        context_provider=(lambda: after) if use_provider else None)

    resumed = requests[1]['messages']
    assert json.loads(resumed[1]['content'])['context'] == after
    assert 'repair description' in resumed[0]['content']
    assert 'inspect description' not in resumed[0]['content']
    assert resumed[2:] == history[2:]
    assert history == original_history
    assert requests[2]['messages'][-1]['content'] == history[3]['content']


@pytest.mark.parametrize('cache_usage', [
    {'prompt_cache_hit_tokens': 120, 'prompt_cache_miss_tokens': 30},
    {'prompt_tokens_details': {'cached_tokens': 120}},
])
async def test_provider_cache_usage_is_preserved_in_result_callbacks_and_audit(monkeypatch, cache_usage):
    usage = {'prompt_tokens': 150, 'completion_tokens': 10, 'total_tokens': 160, **cache_usage}
    capture_requests(monkeypatch, [completion(usage=usage)])
    usage_records = []
    response_records = []

    result = await Gateway(key='ci').generate(BrowserAction, skill_context(),
        on_usage=usage_records.append,
        on_response=lambda exchange, response: response_records.append(response))

    assert result.usage == usage
    assert usage_records == [usage]
    assert response_records[0]['body']['usage'] == usage
