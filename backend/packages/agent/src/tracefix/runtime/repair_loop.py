"""混合修复的可恢复段：模型负责工具循环，外层继续负责补丁与验证门禁。"""
from __future__ import annotations

import asyncio
import copy
import difflib
import json

from tracefix.execution.workspace import PROTECTED_PATTERNS
from tracefix.knowledge.context import build_context
from tracefix.model.gateway import ModelError, ModelOutputError
from tracefix.runtime.contracts import FinishRepair, PatchProposal, Phase


def _session(state):
    return dict((state.loop_evidence or {}).get('repair_session') or {})


def _save_session(engine, state, **updates):
    state.loop_evidence = {**(state.loop_evidence or {}),
                          'repair_session': {**_session(state), **updates}}
    engine.store.save(state)


def _complete_history(messages):
    """仅恢复完整的工具调用/结果组；未知外部操作仍由 operation 回执门禁处理。"""
    result = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict):
            index += 1
            continue
        calls = message.get('tool_calls') if message.get('role') == 'assistant' else None
        if calls:
            identifiers = {call.get('id') for call in calls}
            end = index + 1
            while end < len(messages) and messages[end].get('role') == 'tool':
                end += 1
            returned = {item.get('tool_call_id') for item in messages[index + 1:end]}
            if identifiers <= returned:
                result.extend(copy.deepcopy(messages[index:end]))
            index = end
        else:
            if message.get('role') != 'tool':
                result.append(copy.deepcopy(message))
            index += 1
    return result


def exchange_history(engine, state, *, error=None):
    """从完整请求审计恢复原件，再补齐最后一轮响应与工具回执。"""
    details = getattr(error, 'details', {}) or {}
    if details.get('message_history'):
        return _complete_history(details['message_history'])
    messages = []
    exchange = None
    for ref in state.model_exchange_refs:
        audit = engine.get(state, ref)
        if audit.get('schema') != 'FinishRepair':
            continue
        request = audit.get('request')
        if request is not None:
            messages = copy.deepcopy(request.get('unprojected_messages')
                                     or request.get('json', {}).get('messages', []))
            exchange = audit.get('logical_exchange_id')
        elif audit.get('logical_exchange_id') == exchange:
            response = audit.get('response', {}).get('body', {})
            choices = response.get('choices', []) if isinstance(response, dict) else []
            if choices and isinstance(choices[0].get('message'), dict):
                messages.append(copy.deepcopy(choices[0]['message']))
            tool = audit.get('tool_result', {}).get('message')
            if tool:
                messages.append(copy.deepcopy(tool))
    return _complete_history(messages)


def restore_overlay(engine, state):
    session = _session(state)
    if (state.overlay_snapshot_ref and session.get('snapshot_patch_hash') == state.patch_hash
            and not engine.workspace.export_overlay()['files']):
        engine.workspace.restore_overlay(engine.get(state, state.overlay_snapshot_ref))


def current_diff(engine, state):
    workspace = engine.workspace
    overlay = []
    for path in workspace.export_overlay()['files']:
        before = workspace.baseline_bytes(path).decode('utf-8').splitlines(keepends=True)
        after = workspace.read(path).splitlines(keepends=True)
        overlay.extend(difflib.unified_diff(before, after, fromfile='a/' + path, tofile='b/' + path))
    return {'applied': workspace.diff(state.patch_base_commit or 'HEAD'), 'overlay': ''.join(overlay)}


def report_summary(engine, state):
    from tracefix.runtime.repair_progress import repair_summary
    summary = repair_summary(state)
    try:
        diff = current_diff(engine, state)
    except Exception as error:
        # 清理、取消或工作区故障后，辅助统计不可阻断原有最终报告。
        return {**summary, 'diff_size': None, 'diff_size_error': type(error).__name__}
    lines = (diff['applied'] + '\n' + diff['overlay']).splitlines()
    return {**summary, 'diff_size': {
        'files': len({line[6:] for line in lines if line.startswith('+++ b/')}),
        'added_lines': sum(line.startswith('+') and not line.startswith('+++') for line in lines),
        'removed_lines': sum(line.startswith('-') and not line.startswith('---') for line in lines)}}


def checkpoint_segment(engine, state, *, error=None, submitted=False, advance=True):
    history = exchange_history(engine, state, error=error)
    previous_ref = _session(state).get('history_ref')
    if not history and previous_ref:
        history = engine.get(state, previous_ref)['messages']
    history_ref = engine.put(state, {'type': 'repair_message_history',
        'segment': state.repair_segment, 'messages': history}, name='混合修复完整会话')
    snapshot_ref = engine.put(state, engine.workspace.export_overlay(), name='混合修复暂存快照')
    session = {**_session(state), 'history_ref': history_ref,
               'snapshot_patch_hash': state.patch_hash, 'submitted': submitted,
               'feedback': str(error) if error else None,
               'error_details': getattr(error, 'details', {}) if error else None}
    state = engine.changed(state, repair_segment=state.repair_segment + int(advance),
        overlay_snapshot_ref=snapshot_ref,
        loop_evidence={**(state.loop_evidence or {}), 'repair_session': session})
    engine.event(state, 'repair.segment.completed', {'segment': state.repair_segment,
        'history_ref': history_ref, 'overlay_snapshot_ref': snapshot_ref,
        'submitted': submitted, 'boundary': 'interruption' if not advance else
        'model_output' if error else 'submission'})
    return state


def segment_history(engine, state, context):
    ref = _session(state).get('history_ref')
    if not ref:
        return []
    history = _complete_history(engine.get(state, ref)['messages'])
    if not any(message.get('role') == 'user' for message in history):
        history.insert(0, {'role': 'user', 'content': '继续同一修复会话，按当前冻结上下文执行。'})
    counter = engine.assembler.counter
    # 旧轮工具结果可缩为摘要和原件引用，调用 ID 和结果配对保持完整。
    limit = max(512, (engine.assembler.available - counter.count(context)) // 2)
    if counter.count(history) <= limit:
        return history
    compacted = copy.deepcopy(history)
    tools = [index for index, message in enumerate(compacted) if message.get('role') == 'tool']
    for index in tools[:-2]:
        message = compacted[index]
        raw = message.get('content', '')
        message['content'] = json.dumps({'summary': str(raw)[:300], 'history_ref': ref,
            'tool_call_id': message.get('tool_call_id'), 'compacted': True}, ensure_ascii=False)
    # 原始目标等冻结信息每段从 context 完整注入；删除重复的历史系统/用户上下文。
    if counter.count(compacted) > limit:
        compacted = [message for message in compacted if message.get('role') not in {'system', 'user'}]
    if counter.count(compacted) > limit:
        # 从完整组边界裁剪；完整原件可通过 history_ref 回放。
        while len(compacted) > 1 and counter.count(compacted) > limit:
            end = 1
            while end < len(compacted) and compacted[end].get('role') == 'tool':
                end += 1
            compacted = compacted[end:]
        compacted.insert(0, {'role': 'user', 'content': json.dumps({
            'repair_history_summary': '较早工具轮次已归档；继续根据当前 diff、验证反馈和冻结约束修复。',
            'history_ref': ref}, ensure_ascii=False)})
    if not any(message.get('role') == 'user' for message in compacted):
        compacted.insert(0, {'role': 'user', 'content': json.dumps({
            'repair_history_summary': '较早工具正文已压缩，完整历史保留在原件。',
            'history_ref': ref}, ensure_ascii=False)})
    engine.event(state, 'context.compacted', {'reason': 'repair_segment_boundary',
        'history_ref': ref, 'tokens_before': counter.count(history),
        'tokens_after': counter.count(compacted)})
    return compacted


async def repair_context(engine, state):
    spec = engine.spec(state)
    observation = engine.get(state, state.observation_ref) if state.observation_ref else None
    cards = []
    recipes = []
    # 首段只带少量定位线索，后续由模型按需检索和读取。
    if state.repair_segment == 0:
        engine.warn_retrieval_degraded(state)
        overlay = state.source_manifest + (':' + state.patch_hash if state.patch_hash else '')
        await engine.retriever.index(engine.workspace, overlay)
        code = await engine.retriever.retrieve(state.goal, overlay, 'M1', 4, state=state, phase=state.phase)
        readers = {'artifact_exists': lambda ref: engine.artifacts.exists(state.scope_id, state.run_id, ref),
                   'artifact_read': lambda ref: engine.get(state, ref)}
        recipes = await engine.retriever.retrieve(state.goal, state.source_manifest, 'M3', 3,
            state=state, phase=state.phase, allow_compatible=False, **readers)
        preferred = []
        for card in code:
            try:
                preferred.append(json.loads(card.get('content', '{}')).get('path'))
            except (ValueError, TypeError):
                pass
        cards = engine.workspace.fragments(state.goal, preferred_paths=preferred, limit_chars=12000)
        for card in cards:
            ref = engine.put(state, card, name='混合修复起点源码线索')
            card['artifact_ref'] = ref
            state.evidence_refs.append(ref)
        _save_session(engine, state, repair_memory=recipes)
    else:
        recipes = _session(state).get('repair_memory', [])
    context = build_context(state, spec.model_dump(mode='json'), observation=observation, cards=cards,
                            rules=engine.active_rules(state, str(Phase.DIAGNOSE)))
    failure = _session(state).get('failure_baseline')
    if failure is None:
        failure = {'reproduction_refs': list(state.evidence_refs),
                   'failure_signatures': list(state.failure_signatures),
                   'assertions': [item.model_dump(mode='json') for item in spec.assertions]}
        _save_session(engine, state, failure_baseline=failure)
    context.update(repair_mode='hybrid', repair_segment=state.repair_segment,
        failure_baseline=failure, baseline_validation_refs=list(state.baseline_validation_refs),
        allowed_files=list(engine.workspace.allowed_files),
        protected_paths=list(PROTECTED_PATTERNS) + list(engine.workspace.protected_paths),
        current_diff=current_diff(engine, state),
        latest_verification=engine.current_diagnosis_feedback(state),
        repair_memory=recipes, segment_feedback=_session(state).get('feedback'),
        correction=(state.error_details or {}).get('recovery') or _session(state).get('correction'),
        instruction='在授权 overlay 中自主查阅、编辑和自测；按需委派独立尝试。'
                    '浏览器操作仅为诊断，不修改冻结复现计划；GitHistory 仅访问冻结提交及其祖先。'
                    'Worker 候选不会自动合并，采纳时先核对候选并在父 overlay 重新编辑和自测。'
                    '核对真实 diff 后调用 FinishRepair；它仅请求外层补丁与验证，不代表修复通过。'
                    '若仍缺证据或有验证失败，读取反馈并继续同一修复会话，不放宽冻结断言或权限。')
    engine.store.save(state)
    return context


async def run_segment(engine, state):
    from tracefix.runtime.repair_progress import observe_repair

    engine.scopes.assert_current(engine.context)
    engine.sync_guidance(state)
    engine.require_repair_rule_coverage(state)
    if state.repair_source and not state.reproduced:
        raise ValueError('Finding Repair 必须在独立 Run 内重新稳定复现后修复')
    engine.workspace.check_frozen(engine.source)
    restore_overlay(engine, state)
    if (state.error_details or {}).get('recovery'):
        _save_session(engine, state, correction=state.error_details['recovery'])
    context = await repair_context(engine, state)
    history = segment_history(engine, state, context)
    try:
        proposal = await engine.model_call(state, FinishRepair, context, message_history=history)
        if not isinstance(proposal, PatchProposal):
            raise ModelOutputError('FinishRepair 必须通过运行时提交工具形成已校验补丁', category='output_validation')
    except ModelOutputError as error:
        if getattr(error, 'status', None) in {'WAITING_NETWORK', 'UNKNOWN_OPERATION'}:
            checkpoint_segment(engine, state, error=error, advance=False)
            raise
        state = checkpoint_segment(engine, state, error=error)
        observe_repair(engine, state, 'segment', segment=state.repair_segment)
        # 段上限/输出纠错由混合循环进展判定，不累计旧诊断错误重试。
        state = engine.changed(state, error=None, error_details=None,
                               last_error_signature=None, loop_error_signatures=[])
        return engine.output(state, 'prelude')
    except (ModelError, asyncio.CancelledError) as error:
        checkpoint_segment(engine, state, error=error, advance=False)
        raise
    state = checkpoint_segment(engine, state, submitted=True)
    ref = engine.put(state, proposal.model_dump(mode='json'), name='混合修复补丁方案')
    state = engine.changed(state, phase=Phase.PATCH, patch_ref=ref,
                           hypothesis_refs=state.hypothesis_refs + [ref],
                           error=None, error_details=None, last_error_signature=None)
    return engine.output(state, 'prelude')
