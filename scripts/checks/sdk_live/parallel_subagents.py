"""parallel_subagents（信息类场景，不计入 gate_passed）：主 Agent 在同一条消息中发出 3 个 readonly 委派时子 Agent 是否并发执行。

判定分三层：
- batched：3 个 Agent tool_use 属于同一条 API 消息。CLI 把一条响应的每个 content block 拆成独立记录，
  各记录 uuid 不同、message id 相同，因此按转发消息 payload.message_id 分组，原生 transcript 的 message.id 兜底。
- concurrent：子 Agent transcript（subagents/agent-*.jsonl，经 meta.json 的 toolUseId 归属）全部记录 timestamp 的首尾为执行区间，两两比较重叠。
- correct：summary 含 3 个标记、宿主副本无变化、子 Agent 无成功的写/命令工具调用、执行成功结束。
模型没把调用放在同一条消息里只说明模型行为，记 inconclusive，不代表 SDK 不支持。
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import datetime
from itertools import combinations

from sdk_live import common
from sdk_live.readonly_subagent import DELEGATE_NAMES, WRITE_NAMES, _clip, _execute, _guarded, _init_view

PARTS = ('src/part_a.txt', 'src/part_b.txt', 'src/part_c.txt')
INITIALIZER_TOOLS = ['Read', 'Glob', 'Grep', 'Agent']

PROMPT = (
    '这是一次并行委派能力探针。你（主 Agent）自己不得读取、搜索或修改任何文件，也不得执行命令。'
    '你必须在同一条回复消息里一次性发出 3 个 Agent（Task）工具调用，不得分多轮发出，参数 subagent_type 均为 "readonly"：'
    '第 1 个只读取 src/part_a.txt，第 2 个只读取 src/part_b.txt，第 3 个只读取 src/part_c.txt。'
    '每个子 Agent 只用 Read 读取分配给它的那一个文件，原样返回文件中的标记文本（形如 PART-X-...），不做其他事。'
    '必须等 3 个子 Agent 全部返回结果（若为后台运行，等全部完成通知到达）后才输出结构化结果；'
    'summary 按 a、b、c 顺序逐行列出 3 个标记原文，unresolved 列出未拿到标记的文件。')


def _markers():
    return {path: f'PART-{path[-5].upper()}-{secrets.token_hex(4).upper()}' for path in PARTS}


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def _records(path):
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def forwarded_messages(events):
    """转发消息中主 Agent 委派 tool_use 所属消息：{tool_use_id: {message_id, uuid}}。"""
    found = {}
    for payload in common.messages(events, 'AssistantMessage'):
        if payload.get('parent_tool_use_id') is not None:
            continue
        for block in payload.get('content') or []:
            if isinstance(block, dict) and block.get('name') in DELEGATE_NAMES and block.get('id'):
                found[block['id']] = {'message_id': payload.get('message_id'), 'uuid': payload.get('uuid')}
    return found


def native_messages(execution_dir):
    """主 transcript（非 subagents/）中委派 tool_use 所属消息：{tool_use_id: {message_id, uuid, timestamp}}。"""
    found = {}
    base = execution_dir / 'sdk-config' / 'projects'
    for path in sorted(base.rglob('*.jsonl')) if base.is_dir() else []:
        if 'subagents' in path.parts:
            continue
        for record in _records(path):
            message = record.get('message')
            if record.get('type') != 'assistant' or record.get('isSidechain') or not isinstance(message, dict):
                continue
            content = message.get('content')
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') in DELEGATE_NAMES:
                    found[block.get('id')] = {'message_id': message.get('id'), 'uuid': record.get('uuid'),
                                              'timestamp': record.get('timestamp')}
    return found


def main_transcript_end(execution_dir):
    """主 transcript 最后一条带 timestamp 的记录时间，用于发现主 Agent 先于子 Agent 结束。"""
    stamps = []
    base = execution_dir / 'sdk-config' / 'projects'
    for path in sorted(base.rglob('*.jsonl')) if base.is_dir() else []:
        if 'subagents' not in path.parts:
            stamps.extend(stamp for stamp in (_time(record.get('timestamp')) for record in _records(path)) if stamp)
    return max(stamps).isoformat() if stamps else None


def message_groups(delegation_ids, forwarded, native):
    """按 API 消息 id 分组委派调用；返回 (groups, missing, mismatched)。转发消息优先，原生 transcript 兜底。"""
    groups, missing, mismatched = {}, [], []
    for identity in delegation_ids:
        forwarded_id = (forwarded.get(identity) or {}).get('message_id')
        native_id = (native.get(identity) or {}).get('message_id')
        if forwarded_id and native_id and forwarded_id != native_id:
            mismatched.append(identity)
        message_id = forwarded_id or native_id
        if message_id:
            groups.setdefault(message_id, []).append(identity)
        else:
            missing.append(identity)
    return groups, missing, mismatched


def subagent_intervals(execution_dir, tool_use_ids):
    """经 meta.json 的 toolUseId 归属子 Agent transcript，取其全部记录 timestamp 的首尾作为执行区间。"""
    found = {}
    base = execution_dir / 'sdk-config' / 'projects'
    for meta_path in sorted(base.rglob('*.meta.json')) if base.is_dir() else []:
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get('toolUseId') not in tool_use_ids:
            continue
        transcript = meta_path.with_name(meta_path.name[:-len('.meta.json')] + '.jsonl')
        stamps = sorted(stamp for stamp in (_time(record.get('timestamp')) for record in _records(transcript))
                        if stamp) if transcript.is_file() else []
        found[meta['toolUseId']] = {'agent_type': meta.get('agentType'), 'request_shape': meta.get('requestShape'),
            'transcript': transcript.relative_to(execution_dir).as_posix(), 'timestamps': len(stamps),
            'start': stamps[0].isoformat() if stamps else None, 'end': stamps[-1].isoformat() if stamps else None}
    return found


def overlaps(intervals, tool_use_ids):
    """两两计算区间重叠秒数；返回 (mode, pairs)，mode 为 all/partial/none，任一区间缺失为 unknown。"""
    spans = {}
    for identity in tool_use_ids:
        entry = intervals.get(identity) or {}
        start, end = _time(entry.get('start')), _time(entry.get('end'))
        if start is None or end is None:
            return 'unknown', []
        spans[identity] = (start, end)
    pairs = [{'pair': [left, right], 'overlap_seconds': round(
        (min(spans[left][1], spans[right][1]) - max(spans[left][0], spans[right][0])).total_seconds(), 3)}
        for left, right in combinations(sorted(spans), 2)]
    hits = sum(pair['overlap_seconds'] > 0 for pair in pairs)
    return ('all' if pairs and hits == len(pairs) else 'partial' if hits else 'none'), pairs


def verdict(batched, overlap_mode, correct):
    """返回 (status, reason)；batched 为 None 表示无法确认消息分组。"""
    if batched is None:
        return 'inconclusive', '无法确认委派调用所属的消息分组'
    if not batched:
        return 'inconclusive', '模型未在同一条消息中发出 3 个 readonly 委派，属模型行为，不能说明 SDK 不支持'
    if overlap_mode == 'unknown':
        return 'inconclusive', '子 Agent transcript 时间戳缺失，无法判断是否并发'
    if overlap_mode == 'none':
        return 'failed', 'CLI 串行执行：同一条消息中的委派执行区间互不重叠'
    if overlap_mode == 'partial':
        return 'failed', '仅部分子 Agent 区间重叠，可能存在并发上限'
    if not correct:
        return 'failed', '子 Agent 并发执行，但结果标记或宿主/工具检查不通过'
    return 'passed', '同一条消息中的 3 个 readonly 委派并发执行且结果正确'


def _analyze(run, markers):
    execution_dir = run['workspace'].execution_dir
    delegations = run['delegations']
    readonly = [call['id'] for call in delegations if (call['input'] or {}).get('subagent_type') == 'readonly']
    others = [(call['input'] or {}).get('subagent_type') for call in delegations
              if (call['input'] or {}).get('subagent_type') != 'readonly']
    forwarded = forwarded_messages(run['events'])
    native = native_messages(execution_dir)
    groups, missing, mismatched = message_groups([call['id'] for call in delegations], forwarded, native)
    if missing:
        batched = None
    else:
        batched = (len(readonly) == len(PARTS) and not others
                   and any(set(readonly) <= set(members) for members in groups.values()))
    intervals = subagent_intervals(execution_dir, set(readonly))
    overlap_mode, pairs = overlaps(intervals, readonly) if readonly else ('unknown', [])
    summary = common.result_summary(run['result'])
    outcomes = run['outcomes']
    sub_writes = [call['name'] for call in run['sub_calls']
                  if call['name'] in WRITE_NAMES and common.succeeded(call, outcomes)]
    main_reads = [_clip(call['input'], 120) for call in common.tool_calls(run['events'])
                  if call['parent'] is None and call['name'] in {'Read', 'Glob', 'Grep', *WRITE_NAMES}]
    found = {path: marker in (summary['summary'] or '') for path, marker in markers.items()}
    correct = (summary['status'] == 'SUCCEEDED' and all(found.values()) and not run['host_changes']
               and not sub_writes)
    status, reason = verdict(batched, overlap_mode, correct)
    main_end = main_transcript_end(execution_dir)
    late = [identity for identity, entry in intervals.items()
            if main_end and entry.get('end') and _time(entry['end']) > _time(main_end)]
    started = {payload.get('data', {}).get('tool_use_id'): payload.get('data', {}).get('is_backgrounded')
               for payload in common.messages(run['events'], 'TaskStartedMessage')}
    notified = {payload.get('data', {}).get('tool_use_id'): payload.get('data', {}).get('status')
                for payload in common.messages(run['events'], 'TaskNotificationMessage')}
    return {'status': status, 'reason': reason, 'batched': batched, 'concurrent':
            'unknown' if overlap_mode == 'unknown' else overlap_mode == 'all', 'overlap_mode': overlap_mode,
            'correct': correct, 'execution_id': run['execution_id'], 'result': summary,
            'init': _init_view(run['init']),
            'delegations': [{'id': call['id'], 'subagent_type': (call['input'] or {}).get('subagent_type'),
                             'message_id': (forwarded.get(call['id']) or {}).get('message_id'),
                             'native_message_id': (native.get(call['id']) or {}).get('message_id'),
                             'uuid': (forwarded.get(call['id']) or native.get(call['id']) or {}).get('uuid'),
                             'is_backgrounded': started.get(call['id']), 'notification': notified.get(call['id']),
                             'outcome': _clip((run['delegation_outcomes'].get(call['id']) or {}).get('content'), 300)}
                            for call in delegations],
            'message_groups': groups, 'message_id_missing': missing, 'message_id_mismatched': mismatched,
            'subagent_count': len(intervals), 'agent_types': sorted({str(entry['agent_type']) for entry in intervals.values()}),
            'intervals': intervals, 'overlaps': pairs, 'main_transcript_end': main_end,
            'subagents_ended_after_main': late, 'markers_found': found, 'host_changes': run['host_changes'],
            'subagent_write_succeeded': sub_writes, 'main_direct_file_tools': main_reads,
            'non_readonly_delegations': others}


async def _probe(ctx, role, tools):
    markers = _markers()
    files = {**common.DEFAULT_FILES, **{path: marker + '\n' for path, marker in markers.items()}}
    run = await _execute(ctx, PROMPT, files, role=role, tools=tools)
    return _analyze(run, markers)


async def run(ctx) -> dict:
    # initializer 与 coder 各跑一次，互相独立（各自 execution 目录与容器）。
    coder, initializer = await asyncio.gather(
        _guarded(_probe(ctx.child('coder'), 'coder', None)),
        _guarded(_probe(ctx.child('initializer'), 'initializer', INITIALIZER_TOOLS)))
    roles = {'coder': coder, 'initializer': initializer}
    for value in roles.values():
        if 'error_type' in value:
            value['status'] = 'failed'
    statuses = {value['status'] for value in roles.values()}
    status = 'failed' if 'failed' in statuses else 'inconclusive' if 'inconclusive' in statuses else 'passed'
    report = {'status': status, 'informational': True, 'roles': roles, 'evidence_sources': {
        'batched': '转发 AssistantMessage.message_id（原生 transcript message.id 兜底）；同一响应的各 block uuid 不同',
        'concurrent': 'subagents/agent-*.jsonl 全部记录 timestamp 首尾，经 agent-*.meta.json 的 toolUseId 归属',
        'correct': 'ExecutionResult.summary 标记、execution/source 与冻结输入比对、子 Agent 写/命令工具回执'}}
    return common.redact(report, ctx.config)
