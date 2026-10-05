"""Bounded semantic views and scoped expansion of existing artifacts."""
from __future__ import annotations

import json
import re
import time
from copy import deepcopy

from tracefix.runtime.contracts import digest

VERSION_FIELDS = ('scope_id', 'source_manifest', 'patch_hash', 'page_generation',
                  'environment_digest', 'test_spec_hash')
SIGNALS = re.compile(r'error|exception|fail|alert|dialog|modal|unknown|反证|错误|失败|未知|警告|弹窗', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def public_record(value):
    """Reject explicit held-out/final-evaluation markers without inspecting hidden data."""
    if isinstance(value, dict):
        if (value.get('learnable') is False or value.get('final_scoring_only') is True
                or value.get('source') in {'held_out', 'held-out', 'oracle', 'final_scoring_only'}
                or value.get('provenance') in {'held_out', 'held-out', 'oracle', 'final_scoring_only'}
                or value.get('evidence_source') in {'held_out', 'held-out', 'oracle', 'final_scoring_only'}):
            return False
        return all(public_record(item) for item in value.values())
    if isinstance(value, list):
        return all(public_record(item) for item in value)
    return True


def _refs(record):
    refs = [record.get(key) for key in ('ref', 'artifact_ref', 'observation_ref', 'result_ref')]
    refs.extend(record.get('evidence_refs') or record.get('source_refs') or [])
    for field in ('first_refs', 'last_refs', 'variant_refs'):
        refs.extend(record.get(field) or [])
    return list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref))


def applicability(record, binding, phase=None):
    if not public_record(record):
        return 'non_public_evidence'
    metadata = record.get('metadata') or {}
    versions = {**metadata, **(record.get('binding') or {}), **record}
    if versions.get('status') in {'revoked', 'superseded', 'stale', 'expired'}:
        return 'inactive'
    for field in VERSION_FIELDS:
        expected = binding.get(field)
        actual = versions.get(field, versions.get('generation') if field == 'page_generation' else None)
        if expected not in (None, '') and actual not in (None, '', expected):
            return 'version_mismatch:' + field
    phases = versions.get('phases')
    phases = phases or ([versions['phase']] if versions.get('phase') else [])
    if phases and phase and str(phase).lower() not in {str(item).lower() for item in phases}:
        return 'phase_mismatch'
    if record.get('kind') == 'excluded' and binding.get('patch_hash') and (
            versions.get('patch_hash') != binding['patch_hash']):
        return 'excluded_requires_current_patch'
    expires_at = versions.get('expires_at') or versions.get('expiresAt')
    if expires_at is not None:
        try:
            if float(expires_at) <= time.time():
                return 'expired'
        except (TypeError, ValueError):
            return 'invalid_expiry'
    return None


def select_snapshot(snapshot, counter, token_limit, *, query='', ref=None, binding=None):
    lines = snapshot.splitlines()
    binding = binding or {}
    terms = {term.casefold() for term in re.findall(r'[\w]+', str(query)) if len(term) > 1}
    ancestors = {}
    stack = []
    scored = []
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        ancestors[index] = [parent for _, parent in stack]
        stack.append((indent, index))
        score = 8 * sum(term in line.casefold() for term in terms)
        score += 10 if SIGNALS.search(line) else 0
        score += 1 if re.search(r'heading|main|checkbox|button|textbox', line, re.I) else 0
        scored.append((score, index))
    marker = '... [省略原文可按 artifact/range 展开；上游完整性见 coverage] ...'
    selected = set()
    if counter.count(snapshot) <= token_limit:
        selected = set(range(len(lines)))
    else:
        ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
        for score, index in ranked:
            candidate = selected | set(ancestors[index]) | {index}
            if score > 0:
                for neighbor in (index - 1, index + 1):
                    if 0 <= neighbor < len(lines):
                        candidate.add(neighbor)
            text = '\n'.join(lines[position] for position in sorted(candidate))
            if counter.count(text + '\n' + marker) <= token_limit:
                selected = candidate
    omitted = [index + 1 for index in range(len(lines)) if index not in selected]
    text = '\n'.join(lines[index] for index in sorted(selected))
    if omitted and counter.count(text + '\n' + marker) <= token_limit:
        text += ('\n' if text else '') + marker
    ranges = []
    for index in sorted(selected):
        if ranges and ranges[-1]['end'] == index:
            ranges[-1]['end'] = index + 1
        else:
            ranges.append({'start': index + 1, 'end': index + 1})
    dropped_ranges = []
    for line_number in omitted:
        if dropped_ranges and dropped_ranges[-1]['end'] == line_number - 1:
            dropped_ranges[-1]['end'] = line_number
        else:
            dropped_ranges.append({'start': line_number, 'end': line_number})
    manifest = {'ref': ref, 'binding': binding, 'content_hash': digest(snapshot),
                'selected': ranges, 'dropped': dropped_ranges, 'line_count': len(lines),
                'coverage': 'full' if not omitted else 'semantic_view',
                'lookup_hint': 'snapshot line range'}
    return text, omitted, manifest


def _status(record):
    if isinstance(record.get('fact'), dict):
        return record['fact'].get('status', 'observed')
    result = record.get('result')
    result = result if isinstance(result, dict) else {}
    status = record.get('status', result.get('status'))
    if status in {'UNKNOWN', 'unknown', 'pending', 'WAITING', 'WAITING_NETWORK'}:
        return 'unknown' if str(status).lower() == 'unknown' else 'pending'
    if record.get('error') or record.get('isError') or record.get('passed') is False:
        return 'failed'
    if result.get('error') or result.get('isError') or result.get('passed') is False:
        return 'failed'
    return str(status or 'observed')


def semantic_signature(record):
    result = record.get('result')
    result = result if isinstance(result, dict) else {}
    payload = {**result, **record}
    fields = ('action', 'kind', 'locator', 'target', 'assertion', 'assertions',
              'error_code', 'failure_class', 'error', 'reason', 'text',
              'counterevidence', 'support', 'unknown', 'exit_code', 'status_code', 'output', 'diagnostics',
              'source_manifest', 'patch_hash', 'page_generation')
    semantic = {key: payload[key] for key in fields if key in payload}
    semantic['status'] = _status(record)
    if not semantic.keys() - {'status'}:
        semantic['result'] = result or record.get('summary')
    return digest(semantic)


def summarize_record(record):
    result = record.get('result')
    result = result if isinstance(result, dict) else {}
    fields = ('action', 'summary', 'text', 'support', 'counterevidence', 'unknown',
              'invariants', 'next_action', 'error', 'error_code', 'assertions')
    summary = {'status': _status(record), 'refs': _refs(record)}
    for key in fields:
        value = record.get(key, result.get(key))
        if value is not None:
            encoded = _json(value)
            summary[key] = deepcopy(value) if len(encoded) <= 800 else {
                'excerpt': encoded[:800], 'content_hash': digest(value), 'expand_refs': _refs(record)}
    return summary


def cluster_steps(steps, *, keep_recent=4, max_clusters=12):
    recent = deepcopy(steps[-keep_recent:]) if keep_recent else []
    older = steps[:-keep_recent] if keep_recent else steps
    clusters = {}
    for index, record in enumerate(older):
        if not isinstance(record, dict):
            continue
        signature = record.get('semantic_signature') or semantic_signature(record)
        refs = _refs(record)
        semantic = deepcopy(record.get('fact')) if record.get('semantic_signature') else summarize_record(record)
        cluster = clusters.setdefault(signature, {'semantic_signature': signature, 'count': 0,
            'first_step': record.get('first_step', record.get('step', index + 1)),
            'last_step': record.get('last_step', record.get('step', index + 1)),
            'first_refs': record.get('first_refs', refs), 'last_refs': refs,
            'variant_refs': record.get('variant_refs', [])[-3:], 'fact': semantic})
        if semantic.get('status') == 'failed':
            for key in ('error', 'error_code', 'failure_class'):
                if key in semantic and key not in cluster:
                    cluster[key] = deepcopy(semantic[key])
        cluster['count'] += record.get('count', 1) if record.get('semantic_signature') else 1
        cluster['last_step'] = record.get('last_step', record.get('step', index + 1))
        cluster['last_refs'] = record.get('last_refs', refs)
        if semantic != cluster['fact']:
            cluster['variant_refs'] = list(dict.fromkeys(cluster['variant_refs'] + refs))[-3:]
    ordered = sorted(clusters.values(), key=lambda value: (
        value['fact']['status'] not in {'unknown', 'pending', 'failed'}, -value['last_step']))
    selected, dropped = ordered[:max_clusters], ordered[max_clusters:]
    return {'progress': [item for item in selected if item['fact']['status'] == 'observed'],
            'failures': [item for item in selected if item['fact']['status'] == 'failed'],
            'unresolved': [item for item in selected if item['fact']['status'] in {'unknown', 'pending'}],
            'clusters': selected, 'recent_steps': recent, 'merged_steps': len(older),
            'dropped_clusters': [{'semantic_signature': item['semantic_signature'],
                'first_refs': item['first_refs'], 'last_refs': item['last_refs'], 'count': item['count']}
                for item in dropped]}


def prepare_workset(context):
    context = deepcopy(context)
    observation = context.get('observation')
    observation = observation if isinstance(observation, dict) else {}
    binding = {field: context.get(field, observation.get(field)) for field in VERSION_FIELDS}
    binding['scope_id'] = context.get('scope_id') or context.get('scope') or observation.get('scope_id')
    phase = context.get('phase')
    query = str(context.get('query') or context.get('goal') or context.get('task') or '')
    manifest = {'version': 'tracefix/workset/1', 'binding': binding, 'phase': phase,
                'query_hash': digest(query), 'selected': [], 'dropped': [], 'limitations': []}
    for field in ('working_memory', 'job_memory', 'retrieved_memory', 'repair_memory'):
        value = context.get(field)
        groups = value if isinstance(value, dict) else {'items': value}
        retained = {}
        for group, items in groups.items():
            if not isinstance(items, list):
                retained[group] = items
                continue
            selected = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = {**item, 'kind': item.get('kind', group)}
                reason = applicability(entry, binding, phase)
                index = {'field': field, 'id': item.get('id'), 'refs': _refs(item),
                         'binding': {**(item.get('metadata') or {}), **(item.get('binding') or {})}}
                manifest['dropped' if reason else 'selected'].append({**index, 'reason': reason})
                if not reason:
                    selected.append(item)
            retained[group] = selected
            if query:
                words = {word.casefold() for word in re.findall(r'[\w]+', query) if len(word) > 1}
                retained[group] = sorted(selected, key=lambda item: (
                    not bool(SIGNALS.search(_json(item))),
                    -sum(word in _json(item).casefold() for word in words),
                    -items.index(item)))
        if field in context:
            context[field] = retained if isinstance(value, dict) else retained.get('items', [])
    reason = applicability(observation, binding, phase)
    if reason:
        context['observation'] = {'unavailable': reason, 'observation_ref': context.get('observation_ref')}
        manifest['dropped'].append({'field': 'observation', 'reason': reason})
    if observation.get('truncated') or observation.get('provider_truncated') or observation.get('collector_truncated'):
        manifest['limitations'].append('upstream_truncated: omitted source cannot be reconstructed')
    pending = []
    for field in ('recent_steps', 'recent_action_results'):
        steps = context.get(field)
        if isinstance(steps, list):
            compacted = cluster_steps(steps)
            context[field] = compacted['clusters'] + compacted['recent_steps']
            manifest[field] = {key: value for key, value in compacted.items() if key != 'recent_steps'}
            manifest[field]['recent_index'] = [summarize_record(item)
                for item in compacted['recent_steps'] if isinstance(item, dict)]
            pending.extend({'status': item['fact']['status'], 'refs': item['last_refs']}
                           for item in compacted['unresolved'])
            pending.extend({'status': _status(item), 'refs': _refs(item)}
                           for item in compacted['recent_steps'] if isinstance(item, dict)
                           and _status(item) in {'unknown', 'pending'})
    action = context.get('pending_action')
    if isinstance(action, dict):
        pending.append({'status': 'pending', 'refs': _refs(action)})
    invariants = context.get('invariants') or []
    anchors = {'goal': str(context.get('goal') or '')[:500],
               'invariants': [{'ref': item.get('ref'), 'text': str(item.get('text') or '')[:180]}
                              if isinstance(item, dict) else str(item)[:180] for item in invariants[:8]],
               'pending': pending[:8]}
    context['pruning_facts'] = anchors
    return context, manifest, query, binding


def expand_reference(artifacts, scope_id, run_id, ref, *, start=1, end=None, channel='snapshot',
                     expected_hash=None, binding=None, max_lines=200, max_chars=16000):
    raw = artifacts.read(scope_id, run_id, ref)
    value = json.loads(raw) if ref.endswith('.json') else raw.decode('utf-8')
    original = value
    if isinstance(value, dict):
        if (value.get('learnable') is False or value.get('final_scoring_only') is True
                or value.get('source') in {'held_out', 'oracle', 'final_scoring_only'}):
            raise PermissionError('最终评价原件不可进入开发工作集')
        reason = applicability(value, binding or {})
        if reason:
            raise ValueError(reason)
        value = value.get(channel)
        if value is None:
            raise ValueError('原件没有请求的通道')
    if expected_hash and expected_hash not in {digest(original), digest(value)}:
        raise ValueError('原件版本已变化')
    text = value if isinstance(value, str) else _json(value)
    lines = text.splitlines()
    if type(max_lines) is not int or max_lines < 1 or max_lines > 200:
        raise ValueError('展开上限必须在1到200行内')
    if type(max_chars) is not int or max_chars < 1 or max_chars > 16000:
        raise ValueError('展开字符上限必须在1到16000之间')
    if type(start) is not int or start < 1 or end is not None and (type(end) is not int or end < start):
        raise ValueError('展开行范围无效')
    last = min(len(lines), start + max_lines - 1, end or len(lines))
    selected = '\n'.join(lines[start - 1:last])
    truncated = len(selected) > max_chars
    return {'ref': ref, 'scope_id': scope_id, 'run_id': run_id, 'channel': channel,
            'content_hash': digest(raw), 'span': {'start': start, 'end': last},
            'text': selected[:max_chars], 'coverage': 'range_truncated' if truncated else 'range',
            'character_span': {'start': 0, 'end': min(len(selected), max_chars)},
            'upstream_truncated': bool(isinstance(original, dict) and (
                original.get('truncated') or original.get('provider_truncated')
                or original.get('collector_truncated'))),
            'remaining': max(0, (end or len(lines)) - last)}
