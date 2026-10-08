import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tracefix.execution.repository import safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.model import protocol
from tracefix.model.gateway import Gateway, ModelError
from tracefix.runtime.contracts import (BrowserAction, Decision, FileEdit, PatchProposal,
                                       Phase, RunState, TestSpec as Spec, digest)
from tracefix.runtime.smoke import make_engine
from tracefix.tools.handlers import build_runtime_tools
from tracefix.storage.store import MemoryStore


def call(call_id, name, arguments):
    return {'id': call_id, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


async def test_gateway_executes_registered_code_tool_and_returns_typed_submission(tmp_path, monkeypatch):
    source = tmp_path / 'src'
    source.mkdir()
    original = 'export const persisted = false;\n'
    (source / 'value.ts').write_text(original, encoding='utf-8')
    workspace = Workspace(tmp_path, ['src/**'])
    state = RunState(phase=Phase.DIAGNOSE, scope_id='scope', run_id='run',
        goal='验证原生工具执行和结构化补丁提交', url='http://app:3000',
        source_manifest='manifest', evidence_refs=['evidence'], observation_ref=None)
    store = MemoryStore()
    store.save(state)
    validated = []

    def validate_candidate(proposal, *, state=None):
        validated.append((proposal, state))

    engine = SimpleNamespace(workspace=workspace, store=store, rule_resolver=None,
        memory=None, validate_patch_candidate=validate_candidate)
    runtime = build_runtime_tools(engine, state, PatchProposal)
    pipeline = runtime.pipeline()
    edit = FileEdit(path='src/value.ts', before_hash=digest(original.encode()),
                    content='export const persisted = true;\n')
    responses = [
        httpx.Response(200, json={'id': 'read-response', 'object': 'chat.completion',
            'created': 0, 'model': 'tool-test', 'usage': {
                'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [call('read-1', 'Read', {'file_path': str(source / 'value.ts')})]}}]}),
        httpx.Response(200, json={'id': 'patch-response', 'object': 'chat.completion',
            'created': 0, 'model': 'tool-test', 'usage': {
                'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None,
                'tool_calls': [call('patch-1', 'ProposePatch', {
                    'summary': '修复状态持久化', 'evidence_refs': ['evidence'],
                    'edits': [edit.model_dump()]})]}}]}),
    ]
    requests = []

    async def handle(request):
        requests.append(copy.deepcopy(json.loads(request.content)))
        return responses.pop(0)

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(
            httpx.MockTransport(handle), boundary), event_hooks={
                'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    result = await Gateway(key='ci', stream=False,
                           max_attempts=1).generate(PatchProposal,
        {'phase': 'DIAGNOSE', 'available_evidence_refs': ['evidence']},
        tool_registry=runtime.registry, tool_pipeline=pipeline)
    assert isinstance(result.value, PatchProposal)
    assert result.value.edits[0].content.endswith('true;\n')
    assert pipeline.submission_value == result.value
    assert len(validated) == 1
    assert validated[0][0] == result.value and validated[0][1] is state
    assert requests[0]['tools'][0]['function']['name'] == 'Read'
    assert requests[1]['messages'][-1]['tool_call_id'] == 'read-1'
    assert requests[1]['messages'][-1]['role'] == 'tool'
    assert any(':patch-1:' in key for key in store.operations)
    assert all(record['status'] == 'DONE' for record in store.operations.values())


@pytest.mark.parametrize('phase,schema', [(Phase.PREPARE, Spec), (Phase.EXPLORE, Decision)])
async def test_source_tools_assist_element_discovery_without_replacing_page_binding(
        tmp_path, monkeypatch, phase, schema):
    engine, state = make_engine(tmp_path / 'run')
    project = engine.scopes.projects[state.scope_id]
    component = 'src/TaskControl.tsx'
    content = ('export const TaskControl = () => '
               '<input type="checkbox" aria-label="Complete task" '
               'onChange={() => persistTask()} />;\n')
    (project.root / component).write_text(content, encoding='utf-8', newline='')
    safe_index_files(project.root, [component])
    commit_workspace(project.root, '添加源码辅助识别测试组件')
    workspace, source = Workspace.export(engine.scopes, engine.context, 'HEAD', tmp_path / 'workspace')
    engine.workspace, engine.source = workspace, source
    engine.browser.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.put(state, source)
    state.phase = phase
    state.observation_ref = await engine.capture(state, await engine.browser.action(BrowserAction(kind='observe')))
    observation = engine.get(state, state.observation_ref)
    engine.browser.calls.clear()
    engine.store.save(state)
    engine.model = Gateway(key='ci', stream=False, vision_model='', max_attempts=1)
    requests = []
    tool_calls = [
        call('find-component', 'Glob', {'pattern': '**/*.tsx'}),
        call('search-handler', 'Grep', {'pattern': 'Complete task|onChange',
             'glob': '**/*.tsx', 'output_mode': 'content'}),
        call('read-component', 'Read', {'file_path': str(workspace.root / component)}),
    ]
    if phase == Phase.EXPLORE:
        arguments = {'observation_id': observation['id'], 'element_ref': 'e2',
                     'locator': {'role': 'checkbox', 'name': 'Complete task'}}
        tool_calls.extend([
            call('click-observed-ref', 'BrowserClick', arguments),
            call('finish-exploration', 'FinishExploration', {
                'action': {'kind': 'finish'}, 'summary': '核对源码并操作当前页面元素'}),
        ])
    else:
        tool_calls.append(call('submit-spec', 'SubmitTestSpec', engine.spec(state).model_dump(mode='json')))

    async def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        tool_call = tool_calls[len(requests) - 1]
        return httpx.Response(200, json={'id': 'source-assisted-response', 'object': 'chat.completion',
            'created': 0, 'model': 'tool-test', 'usage': {
                'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {
                'role': 'assistant', 'content': None, 'tool_calls': [tool_call]}}]})

    def client(boundary):
        return httpx.AsyncClient(transport=protocol.BoundaryTransport(
            httpx.MockTransport(handle), boundary), event_hooks={
                'request': [boundary.before], 'response': [boundary.received]})

    monkeypatch.setattr(protocol, 'create_http_client', client)
    result = await engine.model_call(state, schema, {'observation': observation, 'workspace_root': 'untrusted'})
    initial = json.loads(requests[0]['messages'][1]['content'])['context']
    assert initial['workspace_root'] == str(workspace.root)
    assert '事件绑定' in requests[0]['messages'][0]['content']
    assert all(name in requests[0]['messages'][0]['content'] for name in ('Glob', 'Grep', 'Read'))
    names = {tool['function']['name'] for tool in requests[0]['tools']}
    assert {'Read', 'Glob', 'Grep'} <= names
    assert not names & {'Write', 'Edit', 'NotebookEdit'}
    found = json.loads(requests[1]['messages'][-1]['content'])['result']
    assert found['files'] == [str(workspace.root / component)]
    searched = json.loads(requests[2]['messages'][-1]['content'])['result']
    assert searched['matches'][0]['content'] == content.strip()
    read = json.loads(requests[3]['messages'][-1]['content'])['result']
    assert read['raw_content'] == content
    assert read['source_revision'] == state.source_manifest
    assert (workspace.root / component).read_text(encoding='utf-8') == content
    if phase == Phase.EXPLORE:
        assert result.action.kind == 'finish'
        assert [action['kind'] for action in engine.browser.calls] == ['click']
        assert engine.browser.calls[0]['element_ref'] == 'e2'
        assert state.budget.browser_actions == 1
        current = engine.get(state, state.observation_ref)
        tool_calls.append(call('reject-source-ref', 'BrowserClick', {
            **arguments, 'observation_id': current['id'], 'element_ref': 'source-control-id'}))
        with pytest.raises(ModelError) as rejected:
            await engine.model_call(state, Decision, {'observation': current})
        assert rejected.value.category == 'tool_execution'
        assert rejected.value.details['executed'] is False
        assert len(engine.browser.calls) == 1 and state.budget.browser_actions == 1
    else:
        assert isinstance(result, Spec)
        assert not engine.browser.calls
