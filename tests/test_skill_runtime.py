import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from tracefix.knowledge.context import SkillCatalog
from tracefix.model.gateway import Gateway, ModelError
from tracefix.rules import Rule, RuleResolver
from tracefix.runtime.contracts import BrowserAction, Decision, Phase, RunState, digest
from tracefix.runtime.smoke import PNG, make_engine


pytestmark = pytest.mark.usefixtures('json_completion_transport')


def response(value=None, *, tool_id=None, destination=None):
    if tool_id:
        message = {'content': None, 'tool_calls': [{
            'id': tool_id, 'type': 'function', 'function': {
                'name': 'BrowserNavigate',
                'arguments': json.dumps({'value': destination})}}]}
    else:
        message = {'content': json.dumps(value or {'action': {'kind': 'finish'}})}
    return httpx.Response(200, json={'usage': {'total_tokens': 1}, 'choices': [{
        'finish_reason': 'tool_calls' if tool_id else 'stop', 'message': message}]})


def request_context(request):
    content = next(message['content'] for message in request['messages']
                   if message['role'] == 'user')
    if isinstance(content, list):
        content = next(part['text'] for part in content if part['type'] == 'text')
    return json.loads(content)['context']


def skill_names(context):
    return {item['name'] for item in context['skills']}


def events(engine, state, kind):
    return [event['payload'] for event in engine.store.trace(state.run_id, state.scope_id)
            if event['type'] == kind]


def test_builtin_skills_have_a_model_consumer_phase():
    model_phases = {'PREPARE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE'}
    catalog = SkillCatalog(Path('backend/skills'))
    for entry in catalog.details():
        assert model_phases.intersection(entry['phases']), entry['name']


async def skill_engine(root, *, category='a11y', url_patterns=None):
    engine, state = make_engine(root)
    engine.subagent_enabled = False
    component = engine.workspace.root / 'src/Search.tsx'
    component.write_text('export const Search = () => null;\n', encoding='utf-8')
    engine.source['files']['src/Search.tsx'] = digest(component.read_bytes())
    state.source_manifest = digest(engine.source)
    state.repo_snapshot_ref = engine.put(state, engine.source)
    rule = Rule(id='skill-rule', name='页面状态规则', status='enabled', category=category,
        phases=['PREPARE', 'EXPLORE', 'DIAGNOSE'],
        scope={'url_patterns': url_patterns or []},
        detection={'type': 'guided', 'guided': {'prompt': '检查页面状态并引用证据'}})
    engine.rule_resolver = RuleResolver([rule])
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    engine.store.save(state)
    engine.model = Gateway(key='fixture', vision_model='', max_retry_delay=0)
    return engine, state


@pytest.mark.parametrize('category', ['a11y', 'visual'])
async def test_explore_rule_category_and_workspace_paths_reach_request(tmp_path, monkeypatch, category):
    engine, state = await skill_engine(tmp_path, category=category)
    requests = []

    async def post(client, url, **options):
        requests.append(copy.deepcopy(options['json']))
        return response()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await engine.decide(state, None)
    context = request_context(requests[0])
    assert context['detection_rules']['items'][0]['category'] == category
    assert 'empty-state-accessibility' in skill_names(context)
    assert context['cards'] == [] and 'files' not in context
    loaded = next(item for item in context['skills'] if item['name'] == 'empty-state-accessibility')
    assert hashlib.sha256(loaded['content'].encode('utf-8')).hexdigest() == loaded['content_hash']
    assert events(engine, state, 'skills.injected')[0]['skills'] == [
        {key: item[key] for key in ('name', 'version', 'content_hash')} for item in context['skills']]


async def test_planning_and_patch_proposal_receive_workflow_skills(tmp_path, monkeypatch):
    engine, state = await skill_engine(tmp_path)
    original_spec = engine.spec(state).model_dump()
    state.phase = Phase.PREPARE
    state.step = 3
    state.test_spec_ref = None
    state.test_spec_hash = ''
    engine.store.save(state)
    contexts = []

    async def post(client, url, **options):
        context = request_context(options['json'])
        contexts.append(copy.deepcopy(context))
        if len(contexts) == 1:
            return response(original_spec)
        card = next(item for item in context['cards'] if item['path'] == 'src/value.ts')
        return response({'summary': '根据复现证据修复状态',
            'evidence_refs': context['evidence_refs'][:1], 'edits': [{
                'path': card['path'], 'before_hash': card['before_hash'],
                'content': 'export const persisted = true;\n'}]})

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    planned = await engine.prepare(state, None)
    state = RunState(**planned['data'])
    assert {'workflow-test-plan', 'local-verify', 'report-and-review'} <= skill_names(contexts[0])
    state.phase = Phase.DIAGNOSE
    state.reproduced = True
    state.source_aligned = True
    state.evidence_refs = [state.observation_ref]
    engine.store.save(state)
    diagnosed = await engine.diagnose(state, None)
    state = RunState(**diagnosed['data'])
    assert state.phase == Phase.PATCH
    assert {'minimal-patch', 'local-verify', 'report-and-review', 'empty-search-state-fix'} <= skill_names(contexts[1])
    assert 'false' in engine.workspace.read('src/value.ts')
    patched = await engine.patch(state, None)
    state = RunState(**patched['data'])
    await engine.verify(state, None)
    assert 'true' in engine.workspace.read('src/value.ts')
    assert len(contexts) == 2 and state.budget.model_calls == 2
    assert [item['phase'] for item in events(engine, state, 'skills.injected')] == ['PREPARE', 'DIAGNOSE']


@pytest.mark.parametrize('explicit', [False, True])
async def test_native_navigation_refreshes_and_removes_automatic_skills(tmp_path, monkeypatch, explicit):
    engine, state = await skill_engine(tmp_path, url_patterns=['/search*'])
    action = engine.browser.action
    requests = []

    async def navigate(proposed):
        observation = await action(proposed)
        observation['url'] = proposed.value
        return observation

    async def post(client, url, **options):
        requests.append(copy.deepcopy(options['json']))
        if len(requests) == 1:
            return response(tool_id='search', destination='http://app:3000/search')
        if len(requests) == 2:
            return response(tool_id='home', destination='http://app:3000/home')
        return response()

    monkeypatch.setattr(engine.browser, 'action', navigate)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    context = {'load_skills': ['empty-state-accessibility']} if explicit else {}
    await engine.model_call(state, Decision, context)
    loaded = ['empty-state-accessibility' in skill_names(request_context(request)) for request in requests]
    assert loaded == ([True, True, True] if explicit else [False, True, False])
    injections = events(engine, state, 'skills.injected')
    assert [item['tool_round'] for item in injections] == [0, 1, 2]
    assert len({item['logical_exchange_id'] for item in injections}) == 1
    assert [any(skill['name'] == 'empty-state-accessibility' for skill in item['skills'])
            for item in injections] == loaded
    assert len([item for item in events(engine, state, 'skill.loaded')
                if item['name'] == 'empty-state-accessibility']) == 1


async def test_native_skill_refresh_without_rule_resolver(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    skill = tmp_path / 'skills/react-page/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: react-page\ndescription: React 页面指导\nphases: [EXPLORE]\n'
        'triggers:\n  frameworks: [react]\n---\n只引用当前页面证据。', encoding='utf-8')
    engine.skills = SkillCatalog(skill.parent.parent)
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    engine.store.save(state)
    engine.model = Gateway(key='fixture', vision_model='')
    action = engine.browser.action
    requests = []

    async def navigate(proposed):
        observation = await action(proposed)
        observation['url'] = proposed.value
        if proposed.value.endswith('/react'):
            observation['snapshot'] += '\nReact account page'
        return observation

    async def post(client, url, **options):
        requests.append(copy.deepcopy(options['json']))
        if len(requests) == 1:
            return response(tool_id='react', destination='http://app:3000/react')
        if len(requests) == 2:
            return response(tool_id='home', destination='http://app:3000/home')
        return response()

    monkeypatch.setattr(engine.browser, 'action', navigate)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    await engine.model_call(state, Decision, {'observation': engine.get(state, state.observation_ref)})
    assert [skill_names(request_context(request)) for request in requests] == [set(), {'react-page'}, set()]
    assert request_context(requests[-1])['observation']['url'] == 'http://app:3000/home'
    injections = events(engine, state, 'skills.injected')
    assert injections[0]['skills'] == injections[-1]['skills'] == []


@pytest.mark.parametrize('with_image', [False, True])
async def test_each_attempt_audits_actual_skills_but_run_loads_once(tmp_path, monkeypatch, with_image):
    engine, state = await skill_engine(tmp_path)
    engine.model = Gateway(key='fixture', vision_model='fixture-vision' if with_image else '',
                           max_retry_delay=0)
    requests = []

    async def post(client, url, **options):
        requests.append(copy.deepcopy(options['json']))
        if len(requests) == 1:
            return httpx.Response(503, json={'error': {'message': 'retry fixture'}})
        return response()

    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    for call_index in range(2):
        await engine.model_call(state, Decision, {}, image=PNG if with_image else None)
    injections = events(engine, state, 'skills.injected')
    assert [item['attempt'] for item in injections] == [1, 2, 1]
    assert [item['tool_round'] for item in injections] == [0, 0, 0]
    assert injections[0]['logical_exchange_id'] == injections[1]['logical_exchange_id']
    assert injections[2]['logical_exchange_id'] != injections[0]['logical_exchange_id']
    for injection, request in zip(injections, requests, strict=True):
        assert injection['phase'] == 'EXPLORE'
        assert injection['skills'] == [{key: item[key] for key in ('name', 'version', 'content_hash')}
                                       for item in request_context(request)['skills']]
        persisted = engine.get(state, injection['request_ref'])
        assert persisted['request']['json'] == request
    first_skills = request_context(requests[0])['skills']
    assert len(events(engine, state, 'skill.loaded')) == len(first_skills)
    assert len(state.skills_loaded) == len(first_skills)


async def test_configuration_failure_does_not_claim_request_injection(tmp_path):
    engine, state = await skill_engine(tmp_path)
    engine.model.key = ''
    with pytest.raises(ModelError, match='TRACEFIX_API_KEY'):
        await engine.model_call(state, Decision, {})
    assert events(engine, state, 'skill.loaded')
    assert events(engine, state, 'skills.injected') == []
    assert events(engine, state, 'model.request.persisted') == []


@pytest.mark.parametrize('with_image', [False, True])
async def test_resume_preserves_latest_skill_request_then_refreshes_after_tool(tmp_path, monkeypatch, with_image):
    engine, state = await skill_engine(tmp_path, url_patterns=['/search*'])
    engine.model = Gateway(key='fixture', max_attempts=1,
                           vision_model='fixture-vision' if with_image else '')
    action = engine.browser.action
    requests = []

    async def navigate(proposed):
        observation = await action(proposed)
        observation['url'] = proposed.value
        return observation

    async def post(client, url, **options):
        requests.append(copy.deepcopy(options['json']))
        if len(requests) == 1:
            return response(tool_id='search', destination='http://app:3000/search')
        if len(requests) == 2:
            raise httpx.ConnectError('fixture connection refused')
        if len(requests) == 3:
            return response(tool_id='home', destination='http://app:3000/home')
        return response()

    monkeypatch.setattr(engine.browser, 'action', navigate)
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    with pytest.raises(ModelError) as interrupted:
        await engine.model_call(state, Decision, {'original_context': 'preserve this'},
                                image=PNG if with_image else None)
    state.error_details = interrupted.value.details
    engine.store.save(state)
    assert state.error_details['status'] == 'WAITING_NETWORK'
    await engine.model_call(state, Decision, {'recent_action_results': ['rebuilt context']},
                            image=PNG if with_image else None)
    assert requests[2]['messages'] == requests[1]['messages']
    assert 'recent_action_results' not in request_context(requests[2])
    loaded = ['empty-state-accessibility' in skill_names(request_context(request)) for request in requests]
    assert loaded == [False, True, True, False], [(request_context(request).get('observation', {}).get('url'), request_context(request).get('detection_rules', {}).get('rule_ids')) for request in requests]
    assert request_context(requests[-1])['observation']['url'] == 'http://app:3000/home'
    assert state.error_details is None
    injections = events(engine, state, 'skills.injected')
    assert [item['tool_round'] for item in injections] == [0, 1, 1, 2]
    assert injections[1]['skills'] == injections[2]['skills']
    assert injections[1]['logical_exchange_id'] != injections[2]['logical_exchange_id']
    for injection, request in zip(injections, requests, strict=True):
        assert injection['skills'] == [{key: item[key] for key in ('name', 'version', 'content_hash')}
                                       for item in request_context(request)['skills']]


async def test_failed_document_read_does_not_mark_skill_loaded(tmp_path, monkeypatch):
    engine, state = await skill_engine(tmp_path)

    def unavailable(name, phase):
        raise OSError('fixture unreadable document')

    monkeypatch.setattr(engine.skills, 'load_document', unavailable)
    with pytest.raises(OSError, match='unreadable document'):
        engine.load_skill(state, 'empty-state-accessibility')
    assert state.skills_loaded == []
    assert events(engine, state, 'skill.loaded') == []
