"""按 token 预算组装上下文，不改动冻结证据与权限。

上下文组装器负责在每次模型调用前计算可用 token 预算；压缩只作用于可选
观测和历史信息，冻结的权限、目标、TestSpec、规则及用户引导始终保持完整。
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from tracefix.runtime.contracts import digest
from tracefix.knowledge.workset import cluster_steps, prepare_workset, select_snapshot


class ContextWindowError(RuntimeError):
    """受保护区块本身超出窗口时暂停 Run，并返回可审计的容量信息。"""

    status = 'PAUSED'
    category = 'context_window'

    def __init__(self, required_tokens, available_tokens, protected_blocks):
        super().__init__('受保护的上下文超过模型窗口，请精简引导或使用更大的上下文窗口后恢复。')
        self.details = {'source': 'context', 'required_tokens': required_tokens,
                        'available_tokens': available_tokens,
                        'protected_blocks': list(protected_blocks),
                        'requires_manual_review': False}


def serialized(value: Any) -> str:
    """用稳定的 JSON 表示计算 token，避免字典顺序造成预算抖动。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


class TokenCounter:
    """使用供应商 tokenizer；不可用时退化为保守的 UTF-8 字节上界。

    计数器名称和是否精确会写入 manifest，便于回放时解释同一上下文的预算
    差异，而不会把本地估算误当成供应商的精确 token 数。
    """

    def __init__(self, encoder: Callable[[str], Any] | None = None, *, name=None):
        if encoder is None:
            try:
                import tiktoken
                tokenizer = tiktoken.get_encoding('cl100k_base')
                encoder = lambda text: tokenizer.encode(text, disallowed_special=())
                name = name or 'tiktoken_cl100k_base_approximation'
            except (ImportError, OSError):
                pass
        self.encoder = encoder
        self.name = name or ('provider_tokenizer' if encoder else 'utf8_upper_bound')
        self.exact = encoder is not None and self.name != 'tiktoken_cl100k_base_approximation'

    def count(self, value: Any) -> int:
        text = value if isinstance(value, str) else serialized(value)
        if self.encoder is None:
            return len(text.encode('utf-8'))
        encoded = self.encoder(text)
        return int(encoded) if isinstance(encoded, int) else len(encoded)


@dataclass(frozen=True)
class ContextAssembly:
    """一次组装的上下文、清单和是否发生压缩的标记。"""

    context: dict
    manifest: dict
    compacted: bool


def failed_fact(value) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(value.get('error') or value.get('isError') or value.get('is_error')
                or value.get('passed') is False or failed_fact(value.get('result')))


def compact_steps(steps: list[dict], *, keep_recent=4) -> dict:
    """汇总旧步骤的语义簇与原件引用，保留最近完整步骤和未知操作状态。"""
    return cluster_steps(steps, keep_recent=keep_recent)


def compress_snapshot(snapshot: str, counter: TokenCounter, token_limit: int, *,
                      query='', ref=None, binding=None) -> tuple[str, list[int]]:
    """按任务与错误语义保留完整行及祖先，省略内容可按原件引用展开。"""
    text, omitted, _ = select_snapshot(snapshot, counter, token_limit,
                                       query=query, ref=ref, binding=binding)
    return text, omitted


class ContextAssembler:
    """按保护级别和稳定顺序组装上下文，并生成可审计的 block manifest。

    ``OPTIONAL`` 字段按照优先级逐步裁剪，其他字段全部按受保护区块处理。
    manifest 记录每个区块的 hash、token 数和省略项，供事件、回放和人工
    排查确认模型实际看到的内容。
    """

    ORDER = ('policy', 'project_instructions', 'detection_rules', 'skill_index', 'skills',
             'goal', 'test_spec', 'task', 'user_guidance', 'review_guidance', 'working_memory',
             'job_memory', 'retrieved_memory', 'code_insights', 'cards', 'observation',
             'recent_steps', 'recent_action_results', 'instruction')
    PROTECTED = {'policy', 'project_instructions', 'goal', 'test_spec', 'task',
                 'scope', 'phase', 'patch_hash', 'evidence_refs', 'available_evidence_refs',
                 'allowed_files', 'allowed_urls', 'authorized_actions', 'rule_snapshot_hash',
                 'detection_rules', 'user_guidance', 'review_guidance', 'guidance_policy',
                 'effective_constraints', 'ack_required', 'guidance_ack_required', 'instruction'}
    OPTIONAL = ('recent_steps', 'recent_action_results', 'observation', 'code_insights',
                'cards', 'retrieved_memory', 'repair_memory', 'job_memory',
                'reference_documents', 'working_memory')
    SHARES = {'working_memory': .10, 'job_memory': .08,
              'retrieved_memory': .08, 'repair_memory': .08, 'reference_documents': .08,
              'code_insights': .25, 'cards': .25, 'observation': .20}

    def __init__(self, context_window=131072, *, output_tokens=20480, overhead_tokens=1024,
                 counter: TokenCounter | None = None):
        if context_window <= output_tokens + overhead_tokens:
            raise ValueError('模型窗口必须大于输出和协议预留空间')
        self.context_window = context_window
        self.available = context_window - output_tokens - overhead_tokens
        self.counter = counter or TokenCounter()

    def _fit(self, field, value, limit, *, query='', binding=None, workset=None):
        """在字段自己的预算内裁剪，返回保留值及可追踪的省略标识。"""
        if self.counter.count(value) <= limit:
            if field == 'observation' and isinstance(value, dict) and workset is not None:
                _, _, selection = select_snapshot(value.get('snapshot') or '', self.counter, limit,
                    query=query, ref=value.get('artifact_ref') or value.get('observation_ref')
                    or value.get('ref'), binding=binding)
                workset['snapshot'] = selection
                workset['snapshot']['source_coverage'] = (
                    'upstream_truncated' if workset.get('limitations') else value.get('coverage', 'unknown'))
            return value, []
        if field == 'observation' and isinstance(value, dict):
            observation = deepcopy(value)
            snapshot = observation.pop('snapshot', '')
            for channel in ('console', 'network'):
                if channel in observation:
                    logs = observation[channel]
                    logs = logs if isinstance(logs, list) else [logs]
                    ranked = sorted(enumerate(logs), key=lambda item: (
                        not bool(re.search(
                            r'error|fail|exception|500|反证|失败|错误', serialized(item[1]), re.I)),
                        -item[0]))
                    retained = []
                    for _, item in ranked:
                        if self.counter.count(retained + [item]) <= max(0, limit // 5):
                            retained.append(item)
                    observation[channel] = retained
            remaining = max(0, limit - self.counter.count(observation) - 20)
            ref = value.get('artifact_ref') or value.get('observation_ref') or value.get('ref')
            compacted, omitted, selection = select_snapshot(snapshot, self.counter, remaining,
                                                query=query, ref=ref, binding=binding)
            observation['snapshot'] = compacted
            if self.counter.count(observation) > limit:
                for channel in ('console', 'network'):
                    if channel in observation:
                        observation[channel] = '[完整内容见 observation artifact]'
                remaining = max(0, limit - self.counter.count({key: item for key, item in observation.items()
                                                            if key != 'snapshot'}) - 20)
                observation['snapshot'], omitted, selection = select_snapshot(
                    snapshot, self.counter, remaining, query=query, ref=ref, binding=binding)
            if workset is not None:
                workset['snapshot'] = selection
                workset['snapshot']['source_coverage'] = (
                    'upstream_truncated' if workset.get('limitations') else value.get('coverage', 'unknown'))
            return observation, ['snapshot_line:' + str(index) for index in omitted]
        if isinstance(value, list):
            # 列表按原顺序尝试保留，超出预算的项用 id/path 或索引记录。
            retained = []
            omitted = []
            ranked = sorted(enumerate(value), key=lambda entry: (
                not (isinstance(entry[1], dict) and (
                    entry[1].get('status') in {'unknown', 'pending'}
                    or entry[1].get('fact', {}).get('status') in {'unknown', 'pending', 'failed'}
                    or failed_fact(entry[1]))), -entry[0]))
            for index, item in ranked:
                if self.counter.count(retained + [item]) <= limit:
                    retained.append(item)
                else:
                    omitted.append(str(item.get('id') or item.get('path') or index)
                                   if isinstance(item, dict) else str(index))
            return retained, omitted
        if isinstance(value, dict) and field == 'working_memory':
            # 工作记忆只移除最早的列表项，保留最新假设、发现和待办。
            retained = deepcopy(value)
            omitted = []
            for key in ('progress', 'hypothesis', 'finding', 'excluded', 'todo'):
                items = retained.get(key)
                if not isinstance(items, list):
                    continue
                while items and self.counter.count(retained) > limit:
                    removed = items.pop(0)
                    omitted.append(str(removed.get('id', digest(removed)[:12]))
                                   if isinstance(removed, dict) else digest(removed)[:12])
            return retained, omitted
        return None, [digest(value)[:12]]

    def assemble(self, context: dict, *, extra_tokens=0) -> ContextAssembly:
        """在输出和协议预留后组装上下文，必要时只压缩可选区块。

        受保护区块先整体计数；若它们已经超过输入预算，抛出
        ``ContextWindowError`` 让运行时暂停，而不是静默删除权限或测试约束。
        """
        available = self.available - extra_tokens
        original = deepcopy(context)
        original.pop('context_manifest', None)
        original, workset, query, binding = prepare_workset(original)
        ordered = {key: original[key] for key in self.ORDER if key in original}
        ordered.update({key: original[key] for key in sorted(original) if key not in ordered})
        omitted = {}
        protected = {key: value for key, value in ordered.items() if key not in self.OPTIONAL}
        protected_tokens = self.counter.count(protected)
        if protected_tokens > available:
            raise ContextWindowError(protected_tokens, available, protected)
        before = self.counter.count(ordered)
        # 先给大字段分配上限，再按优先级处理整体超限，保证压缩结果确定。
        for field, share in self.SHARES.items():
            if field in ordered:
                ordered[field], omitted[field] = self._fit(field, ordered[field], int(available * share),
                                                          query=query, binding=binding, workset=workset)
        for field in self.OPTIONAL:
            if self.counter.count(ordered) <= available:
                break
            if field not in ordered:
                continue
            excess = self.counter.count(ordered) - available
            limit = max(0, self.counter.count(ordered[field]) - excess - 16)
            source_value = original[field] if field == 'observation' else ordered[field]
            ordered[field], additional = self._fit(field, source_value, limit,
                                                   query=query, binding=binding, workset=workset)
            omitted[field] = list(dict.fromkeys(omitted.get(field, []) + additional))
        after = self.counter.count(ordered)
        if after > available:
            raise ContextWindowError(after, available, protected)
        blocks = [{'name': key, 'tokens': self.counter.count(value), 'hash': digest(value),
                   'protected': key not in self.OPTIONAL,
                   'trimmed_ids': omitted.get(key, [])} for key, value in ordered.items()]
        # manifest 是事实记录，不参与下一轮模型上下文，避免审计字段反过来挤占预算。
        manifest = {'version': 'tracefix/context/1', 'counter': self.counter.name,
                    'exact_tokenizer': self.counter.exact, 'context_window': self.context_window,
                    'input_limit': available, 'tokens_before': before, 'tokens_after': after,
                    'context_hash': digest(ordered), 'blocks': blocks, 'workset': workset}
        return ContextAssembly(ordered, manifest, before != after)
