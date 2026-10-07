import copy
import json
from types import SimpleNamespace

import httpx

from tracefix.execution.workspace import Workspace
from tracefix.model import protocol
from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import FileEdit, PatchProposal, Phase, RunState, digest
from tracefix.runtime.tool_handlers import build_runtime_tools
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
