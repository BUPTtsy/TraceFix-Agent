"""按 token 预算组装上下文，不改动冻结证据与权限。

上下文组装器负责在每次模型调用前计算可用 token 预算；压缩只作用于可选
观测和历史信息，冻结的权限、目标、TestSpec、规则及用户引导始终保持完整。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from tracefix.runtime.contracts import digest


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
    """确定性地汇总旧步骤，同时保留失败证据和最近步骤。

    成功旧步骤只留下动作摘要与证据引用，失败步骤完整保留；这样可以减少
    token，却不会丢失诊断失败所需的错误、证据和当前操作上下文。
    """
    older = steps[:-keep_recent] if keep_recent else steps
    recent = steps[-keep_recent:] if keep_recent else []
    failures = []
    summaries = []
    for index, step in enumerate(older, 1):
        if failed_fact(step):
            # 失败步骤是诊断证据，不能像成功步骤一样只保留摘要。
            failures.append(deepcopy(step))
            continue
        action = step.get('action') or {}
        if not isinstance(action, dict):
            action = {'kind': str(action)}
        summaries.append({'step': step.get('step', index), 'kind': action.get('kind'),
                          'locator': action.get('locator'), 'value': action.get('value'),
                          'summary': step.get('summary', ''),
                          'evidence_refs': list(step.get('evidence_refs') or []),
                          'observation_ref': step.get('observation_ref')})
    return {'progress': summaries, 'failures': failures, 'recent_steps': deepcopy(recent),
            'merged_steps': len(older)}


def compress_snapshot(snapshot: str, counter: TokenCounter, token_limit: int) -> tuple[str, list[int]]:
    """按完整行裁剪快照，并交替保留首尾行。

    快照压缩只改变发送给模型的视图；省略的行号与完整快照仍通过
    observation artifact 可追溯，因此不会把压缩结果当成原始证据。
    """
    if counter.count(snapshot) <= token_limit:
        return snapshot, []
    lines = snapshot.splitlines()
    marker = '... [上下文快照已压缩，完整内容保留在 observation artifact] ...'
    selected = set()
    candidates = []
    for index in range(len(lines)):
        # 交替尝试首尾行，避免从行中间截断 snapshot。
        candidates.extend((index, len(lines) - index - 1))
    for index in dict.fromkeys(candidates):
        candidate = selected | {index}
        text = '\n'.join([lines[position] for position in sorted(candidate)] + [marker])
        if counter.count(text) <= token_limit:
            selected = candidate
    text = '\n'.join([lines[position] for position in sorted(selected)] + ([marker] if selected else []))
    return text, [index + 1 for index in range(len(lines)) if index not in selected]


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

    def _fit(self, field, value, limit):
        """在字段自己的预算内裁剪，返回保留值及可追踪的省略标识。"""
        if self.counter.count(value) <= limit:
            return value, []
        if field == 'observation' and isinstance(value, dict):
            # 先压缩 snapshot，再在仍超限时折叠 console/network；完整原件留在 artifact。
            observation = deepcopy(value)
            snapshot = observation.pop('snapshot', '')
            remaining = max(0, limit - self.counter.count(observation) - 20)
            compacted, omitted = compress_snapshot(snapshot, self.counter, remaining)
            observation['snapshot'] = compacted
            if self.counter.count(observation) > limit:
                for channel in ('console', 'network'):
                    if channel in observation:
                        observation[channel] = '[完整内容见 observation artifact]'
                remaining = max(0, limit - self.counter.count({key: item for key, item in observation.items()
                                                            if key != 'snapshot'}) - 20)
                observation['snapshot'], omitted = compress_snapshot(snapshot, self.counter, remaining)
            return observation, ['snapshot_line:' + str(index) for index in omitted]
        if isinstance(value, list):
            # 列表按原顺序尝试保留，超出预算的项用 id/path 或索引记录。
            retained = []
            omitted = []
            for index, item in enumerate(value):
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
            for key in ('progress', 'hypothesis', 'todo', 'finding', 'excluded'):
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
        facts = {}
        for field in ('recent_steps', 'recent_action_results'):
            failures = [item for item in original.get(field) or [] if failed_fact(item)]
            if failures:
                facts[field] = failures
        memory = original.get('working_memory')
        if isinstance(memory, dict):
            notes = {key: memory[key] for key in ('finding', 'excluded') if memory.get(key)}
            if notes:
                facts['working_memory'] = notes
        observation = original.get('observation')
        if isinstance(observation, dict):
            diagnostics = {key: observation[key] for key in ('console', 'network')
                           if observation.get(key)}
            if diagnostics:
                facts['observation'] = diagnostics
        if facts:
            if 'pruning_facts' in original:
                facts['previous'] = original['pruning_facts']
            original['pruning_facts'] = facts
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
                ordered[field], omitted[field] = self._fit(field, ordered[field], int(available * share))
        for field in self.OPTIONAL:
            if self.counter.count(ordered) <= available:
                break
            if field not in ordered:
                continue
            excess = self.counter.count(ordered) - available
            limit = max(0, self.counter.count(ordered[field]) - excess - 16)
            ordered[field], additional = self._fit(field, ordered[field], limit)
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
                    'context_hash': digest(ordered), 'blocks': blocks}
        return ContextAssembly(ordered, manifest, before != after)
