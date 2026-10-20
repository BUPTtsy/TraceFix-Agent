import pytest

from tracefix.model.gateway import Gateway, ModelOutputError
from tracefix.model.protocol import RequestBoundary
from tracefix.runtime.contracts import FinishRepair, PatchProposal
from tracefix.tools.core import ToolRegistry
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.effects import EffectBoundaryError
from tests.test_h2_repair_tools import repair
from tests.test_pydantic_model_execution import call, response, transport


async def generate(engine, state, gateway):
    runtime = build_runtime_tools(engine, state, FinishRepair)
    pipeline = runtime.pipeline(store_artifact=lambda value: engine.put(state, value))
    result = await gateway.generate(FinishRepair, {'phase': 'DIAGNOSE', 'goal': state.goal},
        tool_pipeline=pipeline, tool_registry=runtime.registry)
    return result, pipeline


async def test_finish_rejection_and_edit_rejection_continue_same_model_session(repair, monkeypatch):
    engine, state = repair
    file_path = str(engine.workspace.root / 'src/value0.py')
    requests = transport(monkeypatch, [
        response(None, calls=[call('FinishRepair', 'empty', {'summary': '尚无修改'})], finish='tool_calls'),
        response(None, calls=[call('Edit', 'bad-edit', {
            'file_path': file_path, 'old_string': 'missing', 'new_string': 'value = 2'})], finish='tool_calls'),
        response(None, calls=[call('Edit', 'good-edit', {
            'file_path': file_path, 'old_string': 'value = 1', 'new_string': 'value = 2'})], finish='tool_calls'),
        response(None, calls=[call('FinishRepair', 'finish', {'summary': '完成修复'})], finish='tool_calls')])
    result, pipeline = await generate(engine, state, Gateway(key='offline', stream=False))
    assert isinstance(result.value, PatchProposal)
    assert result.value == pipeline.submission_value
    assert result.value.edits[0].content == 'value = 2\n'
    assert state.repair_segment == 0 and state.diagnosis_retry_count == 0
    assert len(requests) == 4
    assert 'NO_CHANGE' in str(requests[1]['messages'])
    names = {item['function']['name'] for item in requests[0]['tools']}
    assert 'FinishRepair' in names and 'final_result' not in names
    assert not any('ProposePatch' == name for name in names)


async def test_rejected_finish_calls_are_bounded_by_segment_tool_limit(repair, monkeypatch):
    engine, state = repair
    requests = transport(monkeypatch, [response(None,
        calls=[call('FinishRepair', f'empty-{index}', {'summary': '无效提交'})], finish='tool_calls')
        for index in range(4)])
    with pytest.raises(ModelOutputError) as raised:
        await generate(engine, state, Gateway(key='offline', stream=False, max_tool_rounds=2))
    assert raised.value.category == 'tool_protocol'
    assert raised.value.details['reason'] == 'max_tool_rounds'
    assert len(requests) == 3


async def test_large_overlay_is_not_lost_when_finish_receipt_is_shaped(repair, monkeypatch):
    engine, state = repair
    content = 'value = 2\n' + '# 大候选补丁\n' * 4000
    engine.workspace.stage('src/value0.py', content)
    transport(monkeypatch, [response(None,
        calls=[call('FinishRepair', 'large', {'summary': '保留完整候选'})], finish='tool_calls')])
    result, pipeline = await generate(engine, state, Gateway(key='offline', stream=False))
    assert result.value.edits[0].content == content
    assert pipeline.submission_value.edits[0].content == content


def test_segment_rounds_reset_while_network_resume_keeps_same_segment_count():
    history = [{'role': 'assistant', 'tool_calls': [{'id': str(index)}]} for index in range(45)]
    gateway, registry = Gateway(key='offline', stream=False), ToolRegistry([])
    new = RequestBoundary(gateway, FinishRepair, {}, registry, 'DIAGNOSE', {'initial_history': history})
    assert new.tool_round == 0
    resumed = RequestBoundary(gateway, FinishRepair, {}, registry, 'DIAGNOSE', {
        'initial_history': history, 'preserve_resumed_request': True, 'initial_tool_round': 7})
    assert resumed.tool_round == 7


async def test_write_persistence_failure_keeps_unknown_operation_fence(repair, monkeypatch):
    engine, state = repair
    pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()

    def cannot_save(*args, **kwargs):
        raise ValueError('模拟写后状态保存失败')

    monkeypatch.setattr(engine, 'changed', cannot_save)
    with pytest.raises(EffectBoundaryError) as raised:
        await pipeline.execute('Write', {'file_path': str(engine.workspace.root / 'src/value0.py'),
            'content': 'value = 2\n'}, 'write-save-failed')
    assert raised.value.status == 'UNKNOWN_OPERATION'
    assert engine.workspace.staged_content('src/value0.py') == b'value = 2\n'
