import json
from pathlib import PurePosixPath

import pytest

from tests.test_pydantic_model_execution import call, response, transport
from tracefix.model.gateway import Gateway, ModelError
from tracefix.runtime.contracts import BrowserAction
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import ToolPipeline, ToolRegistry, ToolResult, ToolSpec
from evals.config import EvaluationConfig


def _pipeline(handler, *, feedback):
    spec = ToolSpec('context.expand', '展开 artifact 通道', {'type': 'object', 'properties': {
        'ref': {'type': 'string'}}, 'required': ['ref'], 'additionalProperties': False},
        phases=frozenset({'EXPLORE'}))
    return ToolPipeline(ToolRegistry([spec]), {'context.expand': handler}, 'EXPLORE',
                        feedback_rejections=feedback)



@pytest.mark.parametrize('feedback', [True, False])
async def test_run_pipeline_feeds_back_unexecuted_rejection_but_bare_pipeline_stays_terminal(
        monkeypatch, feedback):
    def expand(arguments, call_id):
        if arguments['ref'] == 'missing-channel':
            raise ValueError('原件没有请求的通道')
        return {'content': 'expanded'}

    requests = transport(monkeypatch, [
        response(None, calls=[call('ContextExpand', 'bad-expand', {'ref': 'missing-channel'})],
                 finish='tool_calls'),
        response(None, calls=[call('ContextExpand', 'good-expand', {'ref': 'ok'})],
                 finish='tool_calls'),
        response(json.dumps({'kind': 'finish'}))])
    pipeline = _pipeline(expand, feedback=feedback)
    gateway = Gateway(key='offline', stream=False)
    if feedback:
        result = await gateway.generate(BrowserAction, {'phase': 'EXPLORE'},
                                        tool_pipeline=pipeline, tool_registry=pipeline.registry)
        assert result.value.kind == 'finish'
        assert '原件没有请求的通道' in str(requests[1]['messages'])
    else:
        with pytest.raises(ModelError) as raised:
            await gateway.generate(BrowserAction, {'phase': 'EXPLORE'},
                                   tool_pipeline=pipeline, tool_registry=pipeline.registry)
        assert raised.value.category == 'tool_execution'
        assert len(requests) == 1



@pytest.mark.parametrize('status', ['UNKNOWN_OPERATION', 'executed'])
async def test_run_pipeline_never_feeds_back_uncertain_or_executed_failures(monkeypatch, status):
    def failing(arguments, call_id):
        if status == 'UNKNOWN_OPERATION':
            return ToolResult(call_id=call_id, name='context.expand', isError=True, executed=True,
                              error={'status': 'UNKNOWN_OPERATION', 'operation_id': 'op-1'})
        return ToolResult(call_id=call_id, name='context.expand', isError=True, executed=True,
                          error={'status': 'FAILED', 'message': '已执行但失败'})

    requests = transport(monkeypatch, [response(None, calls=[
        call('ContextExpand', 'uncertain', {'ref': 'x'})], finish='tool_calls')])
    pipeline = _pipeline(failing, feedback=True)
    with pytest.raises(ModelError) as raised:
        await Gateway(key='offline', stream=False).generate(BrowserAction, {'phase': 'EXPLORE'},
            tool_pipeline=pipeline, tool_registry=pipeline.registry)
    assert len(requests) == 1
    assert raised.value.status == ('UNKNOWN_OPERATION' if status == 'UNKNOWN_OPERATION' else 'FAILED')



def test_runtime_tools_pipeline_enables_rejection_feedback(tmp_path):
    from tracefix.tools.handlers import build_runtime_tools
    from tracefix.runtime.contracts import Phase
    engine, state = make_engine(tmp_path)
    state.phase = Phase.EXPLORE
    assert build_runtime_tools(engine, state, BrowserAction).pipeline().feedback_rejections is True



def test_container_config_paths_stay_posix_after_payload_round_trip(tmp_path):
    config = EvaluationConfig(case_id='public-task', family='public', source_root=tmp_path,
        source_revision='0' * 40, profile=tmp_path / 'profile.yaml', spec=tmp_path / 'spec.json',
        public_protocol=tmp_path / 'protocol.json', skills_root=tmp_path / 'skills',
        initial_experience=tmp_path / 'memory.json', goal='公开用户操作期望', model='fixture',
        seed=0, tools_hash='tools', recovery_rules_hash='recovery')
    import dataclasses
    staged = dataclasses.replace(config, source_root=PurePosixPath('/agent-run/source'),
                                 profile=PurePosixPath('/agent-run/inputs') / 'profile.yaml')
    payload = staged.payload()
    # 容器内的 Linux worker 按 POSIX 解析；反斜杠会被当作相对路径拼接到配置目录下。
    assert payload['source_root'] == '/agent-run/source'
    assert payload['profile'] == '/agent-run/inputs/profile.yaml'
