import json

import pytest

from tracefix.knowledge.context import build_context
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, FileEdit,
    Guidance, GuidanceAck, GuidanceConstraints, Locator, PatchProposal, Phase, RunState, RunStatus, digest)
from tracefix.runtime.guidance import (GuidanceLedger, GuidanceRejected, active_guidance,
    classify_guidance, forbidden_request, retarget_state)
from tracefix.runtime.smoke import FakeModel, make_engine


def test_guidance_ledger_classifies_and_rejects_unsafe_requests(tmp_path):
    ledger = GuidanceLedger(tmp_path / 'guidance.sqlite3')
    hint = ledger.submit('run_1', 'project', '先检查 FormField 组件')
    assert hint.level == 'hint' and hint.status == 'queued'
    constraint = ledger.submit('run_1', 'project', '不要改 src/legacy；补丁不超过 50 行')
    assert constraint.level == 'constraint' and constraint.constraints.max_lines_changed == 50
    rejected = ledger.submit('run_1', 'project', '跳过验证门禁')
    assert rejected.status == 'rejected'
    assert forbidden_request('bypass validation')
    assert classify_guidance('其实问题在结算页，不是购物车') == 'retarget'


def test_guidance_context_is_protected_and_ack_contract_is_strict():
    state = RunState(scope_id='project', goal='检查页面状态', url='http://app:3000')
    entry = Guidance(run_id=state.run_id, scope_id=state.scope_id, text='注意刷新后的状态')
    state.guidance = [entry]
    context = build_context(state, {'goal': state.goal}, observation={'id': 'obs'})
    assert context['user_guidance'][0]['text'] == '注意刷新后的状态'
    assert GuidanceAck(id=entry.id, how_applied='在回归场景中保留刷新断言')
    with pytest.raises(ValueError):
        GuidanceConstraints.model_validate({})


def test_guidance_constraint_payload_round_trips(tmp_path):
    ledger = GuidanceLedger(tmp_path / 'guidance.sqlite3')
    entry = ledger.submit('run_1', 'project', json.dumps({'exclude_paths': ['src/legacy/**'], 'max_files_changed': 2}), level='constraint')
    loaded = ledger.list('run_1', 'project')[0]
    assert loaded.id == entry.id
    assert loaded.constraints.exclude_paths == ['src/legacy/**']
    assert loaded.constraints.max_files_changed == 2


async def test_guidance_is_in_next_model_artifact_and_acknowledged(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    state.mode = 'test'
    engine.store.save(state)
    entry = engine.submit_guidance(state, '注意刷新之后的状态')

    class GuidedModel(FakeModel):
        async def generate(self, schema, context, **kwargs):
            def on_attempt(model, request, attempt):
                request['json']['messages'] = [{'role': 'user', 'content': json.dumps(context, ensure_ascii=False)}]
                return kwargs['on_attempt'](model, request, attempt)
            options = {key: value for key, value in kwargs.items() if key in {
                'image', 'agent_instructions', 'on_response', 'on_error', 'on_usage'}}
            result = await super().generate(schema, context, on_attempt=on_attempt, **options)
            result.value.guidance_ack = [GuidanceAck(id=item['id'], how_applied='保留刷新后的检查')
                                        for item in context['user_guidance']]
            return result

    engine.model = GuidedModel(engine.workspace)
    await engine.run(state)
    latest = engine.store.load(state.run_id, state.scope_id)
    assert latest.run_status == RunStatus.COMPLETED
    assert latest.guidance[0].status == 'acknowledged'
    assert engine.model.decision_calls > 1
    assert latest.guidance[0].how_applied == '保留刷新后的检查'
    assert engine.guidance_status(latest)[0] == latest.guidance[0]
    requests = [engine.get(latest, ref) for ref in latest.model_exchange_refs]
    request = next(item['request'] for item in requests if 'request' in item)
    assert entry.id in json.dumps(request)
    assert '注意刷新之后的状态' in json.dumps(request, ensure_ascii=False)
    events = engine.store.trace(state.run_id, state.scope_id)
    assert [event['type'] for event in events if event['type'].startswith('guidance.')] == [
        'guidance.queued', 'guidance.applied', 'guidance.acknowledged']


def test_record_guidance_ack_is_idempotent_after_state_reload(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    entry = Guidance(run_id=state.run_id, scope_id=state.scope_id, text='注意刷新后的状态',
                     status='applied', applied_step=3, applied_call=2)
    state.guidance = [entry]
    engine.guidance_ledger.save(entry)
    engine.store.save(state)
    candidate = Decision(action=BrowserAction(kind='finish'),
        guidance_ack=[GuidanceAck(id=entry.id, how_applied='保留刷新后的检查')])
    engine.record_guidance_ack(state, candidate)
    events = engine.store.trace(state.run_id, state.scope_id)
    assert [event['type'] for event in events] == ['guidance.acknowledged']
    snapshot = state.model_dump(mode='json')

    state = engine.store.load(state.run_id, state.scope_id)
    engine.guidance_ledger = GuidanceLedger(engine.guidance_ledger.path)

    def unexpected_save(value):
        pytest.fail('重复确认不应再次写入 ledger 或 RunState')

    monkeypatch.setattr(engine.guidance_ledger, 'save', unexpected_save)
    monkeypatch.setattr(engine.store, 'save', unexpected_save)
    for repeat in range(3):
        engine.record_guidance_ack(state, candidate)
    assert state.model_dump(mode='json') == snapshot
    assert engine.store.trace(state.run_id, state.scope_id) == events
    assert engine.guidance_status(state)[0] == state.guidance[0]
    assert state.guidance[0].applied_step == 3
    assert state.guidance[0].applied_call == 2


def test_record_guidance_ack_updates_preserve_previous_evidence(tmp_path):
    engine, state = make_engine(tmp_path)
    entry = Guidance(run_id=state.run_id, scope_id=state.scope_id, text='注意刷新后的状态',
                     status='applied')
    state.guidance = [entry]
    engine.guidance_ledger.save(entry)
    engine.store.save(state)
    explanations = ['保留刷新后的检查', '补充刷新后的持久化断言', '保留刷新后的检查']
    for how_applied in explanations:
        state = engine.store.load(state.run_id, state.scope_id)
        candidate = Decision(action=BrowserAction(kind='finish'),
            guidance_ack=[GuidanceAck(id=entry.id, how_applied=how_applied)])
        engine.record_guidance_ack(state, candidate)
        engine.record_guidance_ack(state, candidate)
        assert state.guidance[0].status == 'acknowledged'
        assert state.guidance[0].how_applied == how_applied
        assert engine.store.load(state.run_id, state.scope_id).guidance == state.guidance
        assert engine.guidance_status(state) == state.guidance

    events = engine.store.trace(state.run_id, state.scope_id)
    assert [event['type'] for event in events] == [
        'guidance.acknowledged', 'guidance.acknowledgement.updated', 'guidance.acknowledgement.updated']
    assert [event['payload']['how_applied'] for event in events] == explanations
    assert [event['payload']['previous_how_applied'] for event in events[1:]] == explanations[:-1]
    assert all(event['payload']['id'] == entry.id for event in events)


@pytest.mark.parametrize('status', ['queued', 'rejected', 'superseded'])
def test_record_guidance_ack_rejects_invalid_state_transitions(tmp_path, status):
    from tracefix.model.gateway import ModelOutputError
    engine, state = make_engine(tmp_path)
    entry = Guidance(run_id=state.run_id, scope_id=state.scope_id, text='注意刷新后的状态', status=status)
    state.guidance = [entry]
    engine.guidance_ledger.save(entry)
    engine.store.save(state)
    snapshot = state.model_dump(mode='json')
    candidate = Decision(action=BrowserAction(kind='finish'),
        guidance_ack=[GuidanceAck(id=entry.id, how_applied='保留刷新后的检查')])
    with pytest.raises(ModelOutputError, match='guidance_ack'):
        engine.record_guidance_ack(state, candidate)
    assert state.model_dump(mode='json') == snapshot
    assert engine.store.load(state.run_id, state.scope_id).model_dump(mode='json') == snapshot
    assert engine.guidance_status(state) == state.guidance
    assert not engine.store.trace(state.run_id, state.scope_id)


async def test_guidance_ack_rejects_omitted_or_unknown_ids(tmp_path):
    from tracefix.model.gateway import ModelOutputError
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    entry = engine.submit_guidance(state, '先检查源码')
    context = engine.guidance_context(state, {}, 1)
    with pytest.raises(ModelOutputError, match='guidance_ack'):
        engine.acknowledge_guidance(state, Decision(action=BrowserAction(kind='finish')), context)
    with pytest.raises(ModelOutputError, match='guidance_ack'):
        engine.acknowledge_guidance(state, Decision(action=BrowserAction(kind='finish'),
            guidance_ack=[GuidanceAck(id='invented', how_applied='已采纳')]), context)
    engine.acknowledge_guidance(state, Decision(action=BrowserAction(kind='finish'),
        guidance_ack=[GuidanceAck(id=entry.id, how_applied='检查源码')]), context)


async def test_constraints_apply_before_patch_and_never_partially_write(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    engine.submit_guidance(state, '禁止修改 src/value.ts', level='constraint')
    engine.sync_guidance(state)
    before = engine.workspace.read('src/value.ts')
    patch = PatchProposal(summary='修改状态', evidence_refs=['evidence'], edits=[FileEdit(
        path='src/value.ts', before_hash=digest(before.encode()), content='export const persisted = true;\n')])
    with pytest.raises(GuidanceRejected) as error:
        engine.workspace.apply(patch)
    assert error.value.details['path'] == 'src/value.ts'
    assert engine.workspace.read('src/value.ts') == before
    assert engine.store.load(state.run_id, state.scope_id).guidance_constraints


def test_constraint_line_limit_counts_whole_candidate_diff(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.workspace.guidance_constraints = [GuidanceConstraints(max_lines_changed=1)]
    before = engine.workspace.read('src/value.ts')
    patch = PatchProposal(summary='修改状态', evidence_refs=['evidence'], edits=[FileEdit(
        path='src/value.ts', before_hash=digest(before.encode()), content='export const persisted = true;\n')])
    with pytest.raises(GuidanceRejected) as error:
        engine.workspace.apply(patch)
    assert error.value.details['actual_lines'] == 2
    assert engine.workspace.read('src/value.ts') == before


def test_retarget_requires_confirmation_and_resets_frozen_contract(tmp_path):
    engine, state = make_engine(tmp_path)
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    entry = engine.submit_guidance(state, '改为检查结算页面的提交状态', level='retarget')
    with pytest.raises(ValueError, match='二次确认'):
        retarget_state(state, entry)
    confirmed = engine.guidance_ledger.confirm(entry.id, state.run_id, state.scope_id)
    child = retarget_state(state, confirmed)
    assert child.parent_run_id == state.run_id and child.run_id != state.run_id
    assert child.goal == entry.text and child.test_spec_ref is None and child.test_spec_hash == ''
    assert not child.reproduced and not child.validation_refs and child.phase == Phase.PREPARE
    with pytest.raises(ValueError):
        engine.guidance_ledger.confirm(entry.id, state.run_id, state.scope_id)


async def test_confirmed_retarget_supersedes_parent_and_preserves_checked_evidence(tmp_path):
    engine, state = make_engine(tmp_path)
    evidence = engine.artifacts.put(state.scope_id, state.run_id, {'facts': ['真实父任务观测']})
    state.evidence_refs = [evidence]
    engine.store.save(state)
    entry = engine.submit_guidance(state, '改为检查结算页面的提交状态', level='retarget')
    engine.guidance_ledger.confirm(entry.id, state.run_id, state.scope_id)
    await engine.run(state)
    latest = engine.store.load(state.run_id, state.scope_id)
    assert latest.run_status == RunStatus.SUPERSEDED
    child = engine.copy_retarget_evidence(latest, engine.retarget_child)
    assert latest.superseded_by_run_id == child.run_id
    assert engine.artifacts.json(child.scope_id, child.run_id, child.evidence_refs[0]) == {'facts': ['真实父任务观测']}
    assert child.inherited_evidence_refs == child.evidence_refs


def test_guidance_expiration_does_not_remove_run_or_future_queue():
    state = RunState(scope_id='project', goal='检查页面状态', url='http://app:3000', phase=Phase.EXPLORE)
    state.guidance = [Guidance(run_id=state.run_id, scope_id=state.scope_id, text='本次只读检查',
                              expires='once', applied_call=1, status='applied'),
                      Guidance(run_id=state.run_id, scope_id=state.scope_id, text='全程保留约束'),
                      Guidance(run_id=state.run_id, scope_id=state.scope_id, text='只在诊断阶段',
                               expires='phase', created_phase=Phase.DIAGNOSE)]
    assert [entry.text for entry in active_guidance(state, logical_call=2)] == ['全程保留约束']
