"""混合修复引擎的段切换、真实工具提交与外层门禁回归。"""
import json
import asyncio

import pytest

from tracefix.execution.workspace import Workspace
from tracefix.model.contracts import ModelError, ModelOutputError, ModelResult
from tracefix.runtime.contracts import FinishRepair, PatchProposal, Phase, RunState
from tracefix.runtime.repair_loop import checkpoint_segment, segment_history
from tracefix.runtime.smoke import make_engine


def configured(tmp_path):
    engine, state = make_engine(tmp_path)
    state.phase, state.repair_mode = Phase.DIAGNOSE, 'hybrid'
    state.reproduced = state.source_aligned = True
    state.environment_digest = engine.runner.environment_digest
    state.evidence_refs = [engine.put(state, {'passed': False, 'assertions': []})]
    engine.store.save(state)
    return engine, state


class SegmentModel:
    supports_tool_executor = True

    def __init__(self, engine, *, boundary=False, forged=False, infrastructure=False):
        self.engine, self.boundary, self.forged = engine, boundary, forged
        self.infrastructure = infrastructure
        self.contexts, self.histories = [], []

    async def generate(self, schema, context, **options):
        assert schema is FinishRepair
        self.contexts.append(context)
        self.histories.append(options.get('message_history', []))
        messages = self.histories[-1] or [{'role': 'user', 'content': '原始故障上下文'}]
        exchange = options['on_attempt']('scripted', {'logical_exchange_id': 'h2',
            'unprojected_messages': messages, 'json': {'messages': messages}}, 1)
        if self.forged:
            return ModelResult(FinishRepair(summary='伪造提交'), {}, 'scripted', 'stop')
        pipeline = options['tool_pipeline']
        if not self.engine.workspace.export_overlay()['files']:
            result = await pipeline.execute('Write', {'file_path': str(self.engine.workspace.root / 'src/value.ts'),
                'content': 'export const persisted = true;\n'}, 'write-' + str(len(self.contexts)))
            assert not result.is_error, result.error
        if self.infrastructure:
            if self.infrastructure == 'cancel':
                raise asyncio.CancelledError()
            raise ModelError('连接在派发前断开', category='network', status='WAITING_NETWORK',
                             details={'request_status': 'not_sent'})
        if self.boundary and len(self.contexts) == 1:
            history = messages + [{'role': 'assistant', 'tool_calls': [
                {'id': 'read-one', 'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'read-one', 'name': 'Read', 'content': '定位到状态写回遗漏'}]
            raise ModelOutputError('单段工具轮次已达上限', category='output_validation',
                                   details={'message_history': history})
        result = await pipeline.execute('finish_repair', {'summary': '修复持久化'},
                                        'finish-' + str(len(self.contexts)))
        assert not result.is_error, result.error
        return ModelResult(FinishRepair(summary='修复持久化'), {}, 'scripted', 'stop')


@pytest.mark.asyncio
async def test_hybrid_routes_and_real_submission_requires_patch_then_verify(tmp_path):
    engine, state = configured(tmp_path)
    engine.model = SegmentModel(engine)
    assert engine.route(state) == 'repair_loop'
    result = await engine.repair_loop(state, None)
    state = RunState(**result['data'])
    assert state.phase == Phase.PATCH and state.repair_segment == 1
    assert state.outcome is None and state.diagnosis_retry_count == 0
    assert 'false' in engine.workspace.path('src/value.ts').read_text()
    assert 'true' in engine.workspace.read('src/value.ts')
    patch = engine.get(state, state.patch_ref)
    assert patch['edits'][0]['path'] == 'src/value.ts'
    result = await engine.patch(state, None)
    state = RunState(**result['data'])
    assert state.phase == Phase.VERIFY and state.outcome is None


@pytest.mark.asyncio
async def test_verification_failure_returns_to_same_repair_session(tmp_path):
    from tracefix.runtime.repair_loop import repair_context, restore_overlay
    engine, state = configured(tmp_path)
    model = engine.model = SegmentModel(engine)
    state = RunState(**(await engine.repair_loop(state, None))['data'])
    history_ref = state.loop_evidence['repair_session']['history_ref']
    state = RunState(**(await engine.patch(state, None))['data'])
    engine.runner.fail = 'unit'
    state.validation_index = 1
    engine.store.save(state)
    state = RunState(**(await engine.verify(state, None))['data'])
    assert state.phase == Phase.DIAGNOSE and state.diagnosis_feedback_refs
    assert state.loop_evidence['repair_session']['history_ref'] == history_ref
    restore_overlay(engine, state)
    assert not engine.workspace.export_overlay()['files']
    context = await repair_context(engine, state)
    assert context['latest_verification'][-1]['status'] == 'failed'
    assert context['current_diff']['applied'] and not context['current_diff']['overlay']


@pytest.mark.asyncio
async def test_full_hybrid_lifecycle_keeps_external_success_gate_and_report(tmp_path):
    from tracefix.runtime.contracts import Outcome
    engine, state = make_engine(tmp_path)
    legacy = engine.model

    class LifecycleModel(SegmentModel):
        async def generate(self, schema, context, **options):
            if schema is FinishRepair:
                return await super().generate(schema, context, **options)
            return await legacy.generate(schema, context, **options)

    engine.model = LifecycleModel(engine)
    state.repair_mode, state.execution_mode = 'hybrid', 'batch'
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert finished.outcome == Outcome.FIX_VERIFIED, finished.error
    assert engine.verification_passed(finished)
    report = engine.get(finished, finished.report_ref)
    assert report['repair_loop']['segments'] == 1
    assert report['repair_loop']['diff_size']['files'] == 1
    assert report['patch_verification'] == 'verified'


@pytest.mark.asyncio
async def test_finalize_preserves_failure_report_when_overlay_snapshot_is_unavailable(tmp_path, monkeypatch):
    from tracefix.runtime.contracts import Outcome
    engine, state = configured(tmp_path)
    state.phase, state.outcome, state.error = Phase.FINALIZE, Outcome.INFRA_FAILURE, '工作区快照不可用'
    engine.store.save(state)

    def unavailable():
        raise ValueError('工作区快照故障')

    monkeypatch.setattr(engine.workspace, 'export_overlay', unavailable)
    state = RunState(**(await engine.finalize(state, None))['data'])
    report = engine.get(state, state.report_ref)
    assert state.outcome == Outcome.INFRA_FAILURE and state.report_ref
    assert report['repair_loop']['diff_size'] is None
    assert report['repair_loop']['diff_size_error'] == 'ValueError'


@pytest.mark.asyncio
async def test_output_boundary_preserves_history_overlay_without_diagnosis_retry(tmp_path):
    engine, state = configured(tmp_path)
    state.error_details = {'recovery': {'action': 'correct_strategy', 'reason': '改查状态持久化链路'}}
    engine.store.save(state)
    model = engine.model = SegmentModel(engine, boundary=True)
    first = await engine.repair_loop(state, None)
    state = RunState(**first['data'])
    assert state.phase == Phase.DIAGNOSE and state.repair_segment == 1
    assert state.diagnosis_retry_count == 0 and state.outcome is None
    assert state.overlay_snapshot_ref and 'true' in engine.workspace.read('src/value.ts')
    second = await engine.repair_loop(state, None)
    state = RunState(**second['data'])
    assert state.phase == Phase.PATCH and state.repair_segment == 2
    assert any(item.get('tool_call_id') == 'read-one' for item in model.histories[1])
    assert '单段工具轮次' in model.contexts[1]['segment_feedback']
    assert 'failure_baseline' in model.contexts[1]
    assert model.contexts[1]['correction']['action'] == 'correct_strategy'


@pytest.mark.asyncio
async def test_restart_restores_segment_overlay_and_does_not_restore_applied_candidate(tmp_path):
    engine, state = configured(tmp_path)
    engine.model = SegmentModel(engine, boundary=True)
    state = RunState(**(await engine.repair_loop(state, None))['data'])
    old = engine.workspace
    engine.workspace = Workspace(old.root, old.allowed_files, protected_paths=old.protected_paths)
    engine.workspace.repository_snapshot = old.repository_snapshot
    model = engine.model = SegmentModel(engine)
    state = RunState(**(await engine.repair_loop(state, None))['data'])
    assert state.phase == Phase.PATCH and model.histories[0]
    state = RunState(**(await engine.patch(state, None))['data'])
    assert not engine.workspace.export_overlay()['files']
    from tracefix.runtime.repair_loop import restore_overlay
    restore_overlay(engine, state)
    assert not engine.workspace.export_overlay()['files']


@pytest.mark.asyncio
async def test_model_cannot_bypass_finish_repair_tool(tmp_path):
    engine, state = configured(tmp_path)
    engine.model = SegmentModel(engine, forged=True)
    state = RunState(**(await engine.repair_loop(state, None))['data'])
    assert state.phase == Phase.DIAGNOSE and state.patch_ref is None
    assert state.diagnosis_retry_count == 0


@pytest.mark.asyncio
async def test_network_error_keeps_original_handling_and_snapshot(tmp_path):
    engine, state = configured(tmp_path)
    engine.model = SegmentModel(engine, infrastructure=True)
    with pytest.raises(ModelError) as failure:
        await engine.repair_loop(state, None)
    assert failure.value.status == 'WAITING_NETWORK'
    persisted = engine.store.load(state.run_id, state.scope_id)
    assert persisted.overlay_snapshot_ref and persisted.repair_segment == 0
    assert persisted.loop_evidence['repair_session']['history_ref']


@pytest.mark.asyncio
async def test_cancellation_keeps_overlay_history_without_advancing_segment(tmp_path):
    engine, state = configured(tmp_path)
    engine.model = SegmentModel(engine, infrastructure='cancel')
    with pytest.raises(asyncio.CancelledError):
        await engine.repair_loop(state, None)
    persisted = engine.store.load(state.run_id, state.scope_id)
    assert persisted.repair_segment == 0 and persisted.overlay_snapshot_ref
    assert persisted.loop_evidence['repair_session']['history_ref']
    assert 'true' in engine.workspace.read('src/value.ts')


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'cancel'])
async def test_segment_boundary_honors_user_controls(tmp_path, control):
    engine, state = configured(tmp_path)
    engine.control = control
    result = await engine.prelude(state, None)
    state = RunState(**result['data'])
    assert result['next_node'] == ('paused' if control == 'pause' else 'finalize')
    assert state.phase == (Phase.DIAGNOSE if control == 'pause' else Phase.FINALIZE)


def test_pipeline_route_and_repair_progress_avoid_old_state_repeat(tmp_path):
    engine, state = configured(tmp_path)
    state.repair_mode = 'pipeline'
    assert engine.route(state) == 'diagnose'
    state.repair_mode = 'hybrid'
    fingerprint = engine._loop_state_fingerprint(state)
    state.loop_state_fingerprints = [fingerprint] * 20
    state.loop_no_progress_steps = 100
    _, _, signals = engine._loop_assessment(state)
    assert not any(item['kind'] == 'state_repeated' for item in signals)
    assert any(item['kind'] == 'no_progress' for item in signals)


@pytest.mark.asyncio
async def test_repeated_empty_output_segments_eventually_stop_without_diagnosis_retry(tmp_path):
    from tracefix.runtime.contracts import Outcome
    engine, state = configured(tmp_path)

    class EmptyModel:
        supports_tool_executor = True

        async def generate(self, schema, context, **options):
            raise ModelOutputError('模型重复未输出可用修复内容', category='output_validation')

    engine.model = EmptyModel()
    for _ in range(40):
        state = RunState(**(await engine.repair_loop(state, None))['data'])
        state, stopped = await engine._record_loop_boundary(state)
        if stopped:
            break
    assert state.outcome == Outcome.LOOP_DETECTED
    assert state.diagnosis_retry_count == 0
    assert any(item['kind'] == 'no_progress' for item in state.loop_evidence['signals'])
    assert state.loop_evidence['repair_session']['history_ref']


@pytest.mark.asyncio
async def test_semantically_improving_results_reset_generic_no_progress_guard(tmp_path):
    from tracefix.runtime.repair_progress import observe_repair
    engine, state = configured(tmp_path)
    for passed in range(1, 9):
        state.loop_no_progress_steps = engine.LOOP_NO_PROGRESS_LIMIT - 1
        engine.store.save(state)
        observe_repair(engine, state, 'run_tests', target='unit',
            result={'failure_signatures': ['remaining-assertion'], 'passed_counts': {'unit': passed}})
        assert state.loop_no_progress_steps == 0
        state, stopped = await engine._record_loop_boundary(state)
        assert not stopped and state.outcome is None
        assert not engine._loop_assessment(state)[2]


def test_context_compaction_keeps_complete_tool_groups_and_original_artifact(tmp_path):
    engine, state = configured(tmp_path)
    history = [{'role': 'user', 'content': '冻结约束'}]
    for index in range(8):
        history.extend([{'role': 'assistant', 'tool_calls': [{'id': str(index),
            'type': 'function', 'function': {'name': 'Read', 'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'Read', 'tool_call_id': str(index), 'content': '详细结果' * 500}])
    state = checkpoint_segment(engine, state,
        error=ModelOutputError('段边界', details={'message_history': history}))
    engine.assembler.available = 1400
    compacted = segment_history(engine, state, {'goal': state.goal})
    assert len(json.dumps(compacted)) < len(json.dumps(history))
    calls = {call['id'] for item in compacted for call in item.get('tool_calls', [])}
    results = {item['tool_call_id'] for item in compacted if item.get('role') == 'tool'}
    assert calls == results
    assert any(item['role'] == 'user' for item in compacted)
    ref = state.loop_evidence['repair_session']['history_ref']
    assert engine.get(state, ref)['messages'] == history


@pytest.mark.asyncio
async def test_resumed_segment_replaces_old_prompt_with_feedback_and_guidance():
    import httpx
    from tracefix.model.gateway import Gateway
    from tracefix.model.protocol import RequestBoundary
    from tracefix.tools.core import ToolRegistry

    context = {'phase': 'DIAGNOSE', 'goal': '冻结目标', 'test_spec': {'assertions': []},
        'latest_verification': [{'status': 'failed', 'message': '刷新后未持久化'}],
        'correction': {'action': 'correct_strategy'},
        'user_guidance': [{'id': 'guidance-new', 'text': '先核对store写入'}]}
    history = [{'role': 'user', 'content': '{"context":{"goal":"旧上下文"}}'},
        {'role': 'assistant', 'tool_calls': [{'id': 'read-old', 'type': 'function',
            'function': {'name': 'Read', 'arguments': '{}'}}]},
        {'role': 'tool', 'name': 'Read', 'tool_call_id': 'read-old', 'content': '旧读取结果'}]
    gateway = Gateway(key='test', stream=False)
    boundary = RequestBoundary(gateway, FinishRepair, context, ToolRegistry(), Phase.DIAGNOSE,
                               {'initial_history': history, 'context_provider': lambda: context})
    request = httpx.Request('POST', 'https://model.invalid/chat/completions',
                           json={'model': 'test', 'messages': history})
    await boundary.before(request)
    messages = json.loads(request.content)['messages']
    injected = json.loads(next(item['content'] for item in messages if item['role'] == 'user'))['context']
    assert injected['latest_verification'][0]['status'] == 'failed'
    assert injected['correction']['action'] == 'correct_strategy'
    assert injected['user_guidance'][0]['id'] == 'guidance-new'
    assert boundary.tool_round == 0
    assert next(item for item in messages if item.get('tool_call_id') == 'read-old')['content'] == '旧读取结果'
