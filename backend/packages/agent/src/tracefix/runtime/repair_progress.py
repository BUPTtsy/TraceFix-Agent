"""混合修复的持久化进展观察，以及不调用模型的审计轨迹重放。

这里只返回软预警/信号；段边界是否纠偏或退出由 Engine 决定。阈值是
观察重复所需的样本数，不是工具、段数或执行时长预算。
"""
from __future__ import annotations

import copy
import io
import json
import re
import tokenize
from pathlib import Path

from tracefix.execution.workspace import git
from tracefix.runtime.contracts import digest


WINDOW = 12
STAGNATION_WARNING = 3
STAGNATION_SIGNAL = 5
DIFF_WARNING = 2
DIFF_SIGNAL = 3
SUBMISSION_WARNING = 2
SUBMISSION_SIGNAL = 3
_TOKEN = re.compile(r'[\w$]+|===|!==|=>|==|!=|<=|>=|\+\+|--|&&|\|\||\?\?|\?\.|\*\*|<<|>>>|>>|\+=|-=|\*=|/=|[^\s]', re.UNICODE)
_C_STYLE = {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.java', '.c', '.h', '.cpp', '.hpp', '.cs', '.go', '.rs', '.swift', '.kt'}


def _quoted_end(text, start):
    quote = text[start]
    marker = quote * 3 if text.startswith(quote * 3, start) else quote
    index = start + len(marker)
    interpolation = []
    while index < len(text):
        if text[index] == '\\':
            index += 2
        elif quote == '`' and text.startswith('${', index):
            interpolation.append(1)
            index += 2
        elif interpolation:
            character = text[index]
            if character in "'\"`":
                index = _quoted_end(text, index)
                continue
            if text.startswith('//', index):
                end = text.find('\n', index)
                index = len(text) if end < 0 else end + 1
                continue
            if text.startswith('/*', index):
                end = text.find('*/', index + 2)
                index = len(text) if end < 0 else end + 2
                continue
            if character == '{':
                interpolation[-1] += 1
            elif character == '}':
                interpolation[-1] -= 1
                if not interpolation[-1]:
                    interpolation.pop()
            index += 1
        elif text.startswith(marker, index):
            return index + len(marker)
        else:
            index += 1
    return len(text)


def normalize_source(source: str | bytes, path: str = '') -> tuple[str, ...]:
    """词法去格式/注释；字符串和 token 边界始终保留。

    Python 保留逻辑换行与缩进层级。JS 模板/正则整体保留，宁可漏掉其中
    一些格式噪声，也不删除字符串或正则里的 //、/* 和空格。
    """
    text = source.decode('utf-8', errors='replace') if isinstance(source, bytes) else source
    suffix = Path(path).suffix.lower()
    if suffix == '.py':
        try:
            return tuple('<indent>' if item.type == tokenize.INDENT else '<dedent>'
                         if item.type == tokenize.DEDENT else '<newline>'
                         if item.type == tokenize.NEWLINE else item.string
                         for item in tokenize.generate_tokens(io.StringIO(text).readline)
                         if item.type not in {tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER})
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # 半成品源码不能导致工具失败；保守保留内容，不猜测语法。
            return (text,)
    tokens, index = [], 0
    comments = suffix in _C_STYLE
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        start = index
        if text[index] in "'\"`":
            index = _quoted_end(text, index)
            tokens.append(text[start:index])
            continue
        if comments and text.startswith('//', index):
            end = text.find('\n', index)
            index = len(text) if end < 0 else end + 1
            continue
        if comments and text.startswith('/*', index):
            end = text.find('*/', index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        if suffix in {'.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx'} and text[index] == '/' and (
                not tokens or tokens[-1] in {'=', '(', '[', '{', ',', ':', ';', '!', '&&', '||', '??', '=>', 'return', 'throw', 'case', 'yield'}):
            index += 1
            in_class = False
            while index < len(text) and text[index] != '\n':
                character = text[index]
                if character == '\\':
                    index += 2
                    continue
                if character == '[':
                    in_class = True
                elif character == ']':
                    in_class = False
                elif character == '/' and not in_class:
                    index += 1
                    while index < len(text) and text[index].isalpha():
                        index += 1
                    break
                index += 1
            tokens.append(text[start:index])
            continue
        match = _TOKEN.match(text, index)
        tokens.append(match.group())
        index = match.end()
    return tuple(tokens)


def normalized_diff_fingerprint(workspace, state) -> str:
    """相对 patch_base_commit 的累积内容差异，兼容物化前后的同一候选。"""
    base = state.patch_base_commit or workspace.head()
    changed = git(workspace.root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', base, '--')
    paths = {item for item in changed.decode('utf-8').split('\0') if item}
    snapshot = workspace.require_repository_snapshot()
    paths.update(path for path in snapshot['files'] if workspace.staged_content(path) is not None)
    entries = []
    for path in sorted(paths):
        before = normalize_source(git(workspace.root, 'show', f'{base}:{path}'), path)
        staged = workspace.staged_content(path)
        after = normalize_source(staged if staged is not None else workspace.path(path).read_bytes(), path)
        if before != after:
            entries.append([path, before, after])
    return digest(entries)


def _repair(state):
    return (state.loop_evidence or {}).get('repair') or {}


def _counts(result, payload):
    explicit = payload.get('passed_counts', result.get('passed_counts'))
    if isinstance(explicit, dict):
        return {str(key): int(value) for key, value in explicit.items() if isinstance(value, int) and value >= 0}
    summary = result.get('summary') or {}
    if isinstance(summary, dict) and isinstance(summary.get('passed'), int):
        return {'passed': summary['passed']}
    checks = result.get('assertions') or result.get('checks')
    if isinstance(checks, list) and checks:
        key = 'regression' if result.get('kind') == 'regression' else 'target'
        return {key: sum(item is True or isinstance(item, dict) and (
            item.get('passed') is True or item.get('status') == 'pass') for item in checks)}
    return {'passed': int(result.get('passed') is True)}


def _assessment(repair):
    warnings, signals = [], []
    epoch = repair.get('epoch', 0)
    tail = [item for item in repair.get('history', []) if item.get('epoch') == epoch]

    def add(kind, count, warning, signal, **details):
        entry = {'kind': kind, 'count': count, 'window': WINDOW, **details}
        if count >= warning:
            warnings.append(entry)
        if count >= signal:
            signals.append(entry)

    tests = [item for item in tail if item['event'] == 'run_tests']
    if tests and tests[-1].get('parsed'):
        last = tests[-1]
        same = 0
        for item in reversed(tests):
            if not item.get('parsed') or any(item.get(key) != last.get(key)
                    for key in ('target', 'failure_signatures', 'passed_counts')):
                break
            same += 1
        add('repair_signature_stagnation', same, STAGNATION_WARNING, STAGNATION_SIGNAL,
            target=last['target'], failure_signatures=last['failure_signatures'], passed_counts=last['passed_counts'])
    # 一次自测紧接一次提交是同一个候选，折叠相邻相同 diff，避免假振荡。
    runs = []
    for item in tail:
        value = item.get('diff_fingerprint')
        if value and (not runs or runs[-1] != value):
            runs.append(value)
    if runs:
        add('repair_diff_oscillation', runs.count(runs[-1]), DIFF_WARNING, DIFF_SIGNAL,
            diff_fingerprint=runs[-1], diff_tail=runs)
    rejected = 0
    candidate = None
    for item in reversed(tail):
        if item['event'] != 'finish_repair':
            continue
        if item.get('accepted') or item.get('error_code') != 'FAILED_CANDIDATE_REPEATED':
            break
        current = item.get('candidate_fingerprint')
        if candidate is not None and current != candidate:
            break
        candidate = current
        rejected += 1
    add('repair_candidate_repeated', rejected, SUBMISSION_WARNING, SUBMISSION_SIGNAL,
        candidate_fingerprint=candidate)
    return {'warnings': warnings, 'signals': signals,
            'evidence': {'epoch': epoch, 'observations': repair.get('observations', 0),
                         'history': copy.deepcopy(tail), 'thresholds': _thresholds()}}


def _thresholds():
    return {'window': WINDOW, 'signature_stagnation': [STAGNATION_WARNING, STAGNATION_SIGNAL],
            'diff_oscillation': [DIFF_WARNING, DIFF_SIGNAL],
            'candidate_repeated': [SUBMISSION_WARNING, SUBMISSION_SIGNAL]}


def assess_repair(state) -> dict:
    if getattr(state, 'repair_mode', 'pipeline') != 'hybrid':
        return {'warnings': [], 'signals': [], 'evidence': {}}
    return _assessment(_repair(state))


def advance_repair(repair: dict, event: str, **payload) -> tuple[dict, bool]:
    """纯状态归约器；在线观察和离线真实审计记录共用同一判定。"""
    data = copy.deepcopy(repair)
    result = payload.get('result') or {}
    index = data.get('observations', 0) + 1
    epoch = data.get('epoch', 0)
    reasons = []
    entry = {'index': index, 'event': event, 'epoch': epoch}
    reference = payload.get('ref') or result.get('result_ref') or result.get('output_ref')
    if reference:
        entry['ref'] = reference
    if event == 'read':
        path = str(payload.get('path') or '').replace('\\', '/')
        seen = data.setdefault('read_files', [])
        if path and path not in seen:
            seen.append(path)
            reasons.append('new_file')
        entry['path'] = path
    if event in {'run_tests', 'verify'}:
        target = payload.get('target') or result.get('target') or result.get('kind') or 'unit'
        signatures = sorted(set(payload.get('failure_signatures', result.get('failure_signatures', []))))
        counts = _counts(result, payload)
        parsed = bool(payload.get('parsed', not (result.get('summary') or {}).get('unparsed', False)))
        key = event + ':' + target
        frontier = data.setdefault('frontier', {})
        previous = frontier.get(key)
        if event == 'run_tests' and target not in data.setdefault('targets', []):
            data['targets'].append(target)
            reasons.append('new_target')
        if parsed:
            if previous and set(signatures) < set(previous['failure_signatures']):
                reasons.append('failure_subset')
            if any(value > (previous or {}).get('passed_counts', {}).get(name, 0) for name, value in counts.items()):
                reasons.append('more_passed')
            # 仅保留已取得的最优前沿，来回退步/恢复不能反复伪造进展。
            frontier[key] = {'failure_signatures': (signatures if not previous or
                set(signatures) <= set(previous['failure_signatures']) else previous['failure_signatures']),
                'passed_counts': {name: max(value, (previous or {}).get('passed_counts', {}).get(name, 0))
                                  for name, value in {**(previous or {}).get('passed_counts', {}), **counts}.items()}}
        entry.update(target=target, failure_signatures=signatures, passed_counts=counts, parsed=parsed)
        data[event + '_count'] = data.get(event + '_count', 0) + 1
        data['last_' + event] = copy.deepcopy(result)
        if event == 'run_tests' and result.get('result_ref'):
            refs = data.setdefault('self_test_refs', [])
            if result['result_ref'] not in refs:
                refs.append(result['result_ref'])
    if event == 'finish_repair':
        entry.update(accepted=bool(payload.get('accepted')), error_code=payload.get('error_code'),
                     candidate_fingerprint=payload.get('candidate_fingerprint'))
        data['finish_repair_count'] = data.get('finish_repair_count', 0) + 1
    if event == 'correction':
        data['corrections'] = data.get('corrections', []) + [{'index': index, 'ref': reference}]
    if event == 'segment':
        entry['segment'] = payload.get('segment')
    if event in {'run_tests', 'finish_repair', 'verify', 'revert_overlay'}:
        entry['diff_fingerprint'] = payload.get('normalized_diff_fingerprint') or result.get('normalized_diff_fingerprint')
        entry['candidate_fingerprint'] = payload.get('candidate_fingerprint') or result.get('diff_fingerprint')
    if reasons:
        epoch += 1
        data['last_progress'] = {'index': index, 'reasons': reasons}
    entry.update(epoch=epoch, semantic_progress=bool(reasons), progress_reasons=reasons)
    data.update(version=1, epoch=epoch, observations=index, history=(data.get('history', []) + [entry])[-WINDOW:])
    return data, bool(reasons)


def observe_repair(engine, state, event: str, **payload) -> dict:
    """持久化观测并同步调用方状态；不会改变 phase/outcome 或终止工具循环。"""
    if getattr(state, 'repair_mode', 'pipeline') != 'hybrid':
        return assess_repair(state)
    result = payload.get('result') or {}
    if event == 'run_tests' and result.get('result_ref') and (
            result.get('kind') != 'repair_self_test' or result.get('run_id') != state.run_id
            or result.get('scope_id') != state.scope_id
            or result.get('source_manifest') != state.source_manifest):
        # 白名单只能登记属于当前 Run 的完整结构化结果，不能登记外部引用。
        result = {key: value for key, value in result.items() if key != 'result_ref'}
        payload['result'] = result
    if event in {'run_tests', 'finish_repair', 'verify', 'revert_overlay'} and not (
            payload.get('normalized_diff_fingerprint') or result.get('normalized_diff_fingerprint')):
        payload['normalized_diff_fingerprint'] = normalized_diff_fingerprint(engine.workspace, state)
    data, progressed = advance_repair(_repair(state), event, **payload)
    assessment = _assessment(data)
    warnings = state.loop_warnings + [item for item in assessment['warnings'] if item not in state.loop_warnings[-WINDOW:]]
    updated = engine.changed(state, loop_evidence={**(state.loop_evidence or {}), 'repair': data},
                             loop_warnings=warnings, _semantic_progress=progressed)
    for field in type(state).model_fields:
        setattr(state, field, getattr(updated, field))
    return assessment


def repair_summary(state) -> dict:
    data = _repair(state)
    history = data.get('history', [])
    return {'segments': getattr(state, 'repair_segment', 0),
            'run_tests_count': data.get('run_tests_count', 0), 'last_run_tests': data.get('last_run_tests'),
            'self_test_refs': data.get('self_test_refs', []), 'corrections': data.get('corrections', []),
            'last_progress': data.get('last_progress'), 'detection': assess_repair(state),
            'final_diff_fingerprint': next((item['diff_fingerprint'] for item in reversed(history)
                                            if item.get('diff_fingerprint')), None),
            'calibration': {'source': 'scripted', 'real_trajectory_calibrated': False, 'thresholds': _thresholds()}}


def replay_repair_events(records, *, initial=None, artifact_read=None) -> dict:
    """重放 JSONL 事件对象/行；支持 tool.started + tool.completed/tool.error 审计。

    artifact_read(ref) 可补回被裁剪的完整工具回执。只读取记录中已保存的
    normalized_diff_fingerprint，不把原始字节摘要冒充归一化 diff。缺失字段
    显式计入 incomplete；重放绝不调用模型、Docker 或修改工作区。
    """
    data, pending, seen, assessments, incomplete = copy.deepcopy(initial or {}), {}, set(), [], []
    for row, raw in enumerate(records, 1):
        record = json.loads(raw) if isinstance(raw, str) else raw
        kind = record.get('event') or record.get('kind') or record.get('type')
        payload = record.get('payload') or {}
        if kind in {'read', 'run_tests', 'finish_repair', 'verify', 'correction'}:
            event, value = kind, payload
        elif kind in {'tool.started', 'tool.requested'}:
            pending[payload.get('tool_call_id') or payload.get('operation_id')] = payload.get('intent') or {}
            continue
        elif kind in {'tool.completed', 'tool.error'}:
            receipt = payload.get('receipt') or {}
            if receipt.get('truncated') and artifact_read and receipt.get('artifact_ref'):
                receipt = artifact_read(receipt['artifact_ref'])
            call_id = payload.get('tool_call_id') or receipt.get('call_id') or payload.get('operation_id')
            if call_id is None:
                incomplete.append({'row': row, 'missing': 'receipt/call_id'})
                continue
            if call_id in seen:
                continue
            seen.add(call_id)
            intent = pending.get(call_id, pending.get(payload.get('operation_id'), {}))
            name = receipt.get('name') or payload.get('tool_name') or intent.get('tool_name')
            event = {'Read': 'read', 'RunTests': 'run_tests', 'FinishRepair': 'finish_repair', 'finish_repair': 'finish_repair'}.get(name)
            if event is None:
                continue
            result = receipt.get('result') or {}
            arguments = intent.get('arguments') or {}
            if event == 'read':
                if receipt.get('isError') or not receipt:
                    continue
                value = {'path': result.get('relative_path') or arguments.get('file_path')}
            elif event == 'run_tests':
                if receipt.get('isError') or not isinstance(result, dict):
                    continue
                value = {'target': result.get('target') or arguments.get('target', 'unit'), 'result': result}
            else:
                error = receipt.get('error') or payload.get('error') or {}
                value = {'accepted': not receipt.get('isError', True),
                         'error_code': error.get('error_code') or result.get('error_code'),
                         'candidate_fingerprint': result.get('candidate_fingerprint'), 'result': result}
        else:
            continue
        if event in {'run_tests', 'finish_repair', 'verify'} and not (
                value.get('normalized_diff_fingerprint') or (value.get('result') or {}).get('normalized_diff_fingerprint')):
            incomplete.append({'row': row, 'missing': 'normalized_diff_fingerprint'})
        data, progressed = advance_repair(data, event, **value)
        assessment = _assessment(data)
        assessments.append({'row': row, 'event': event, 'semantic_progress': progressed, **assessment})
    return {'repair': data, 'assessments': assessments, 'incomplete': incomplete}


def calibrate_repair_traces(traces, *, artifact_read=None) -> dict:
    """用人工标注 expected_loop 的脚本/真实 JSONL 轨迹统计误杀与漏检。

    每项为 {records, expected_loop, source: 'scripted'|'real'}；source 必须
    显式提供，不能把脚本输出当成真实模型轨迹的校准结果。
    """
    counts = {'true_positive': 0, 'false_positive': 0, 'true_negative': 0, 'false_negative': 0}
    sources = {'scripted': 0, 'real': 0}
    incomplete = 0
    for trace in traces:
        if trace.get('source') not in sources or type(trace.get('expected_loop')) is not bool:
            raise ValueError('校准轨迹必须显式标注 source 和 expected_loop')
        replay = replay_repair_events(trace['records'], artifact_read=artifact_read)
        detected = any(item['signals'] for item in replay['assessments'])
        key = ('true_' if detected == trace['expected_loop'] else 'false_') + ('positive' if detected else 'negative')
        counts[key] += 1
        sources[trace['source']] += 1
        incomplete += bool(replay['incomplete'])
    positives = counts['true_positive'] + counts['false_negative']
    negatives = counts['true_negative'] + counts['false_positive']
    return {**counts, 'sources': sources, 'incomplete_traces': incomplete, 'thresholds': _thresholds(),
            'false_positive_rate': counts['false_positive'] / negatives if negatives else None,
            'false_negative_rate': counts['false_negative'] / positives if positives else None}
