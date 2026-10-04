"""One LangGraph, one writer, usage accounting, and evidence-driven transitions."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import html
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph.errors import GraphInterrupt, GraphRecursionError

from tracefix.execution.browser import (MCPActionUnknown, MCPConnectionError, assertions,
    resolve_locator, validate_spec_observation)
from tracefix.model.gateway import Gateway, ModelError, ModelOutputError
from tracefix.model.chat_completions import reasoning_records
from tracefix.knowledge.context import SkillCatalog, build_context
from tracefix.knowledge.selection import select_documents
from tracefix.rules import Finding, RuleResolver, RuleSnapshot, evaluate_oracle, evaluate_static
from tracefix.rules.resolver import render_rule_context, rule_applies
from tracefix.runtime.contracts import (BudgetExceeded, BrowserAction, Decision, Outcome,
    PatchProposal, Phase, ReplayUnbound, ReproductionPlan, RunState, RunStatus, TestSpec, Validation, digest, new_id,
    reduce_state, verification_gate)
from tracefix.runtime.guidance import (GuidanceLedger, GuidanceRejected, active_guidance,
                                      retarget_state)
from tracefix.runtime.tool_handlers import build_runtime_tools, materialize_patch_proposal
from tracefix.runtime.skills import SkillStore
from tracefix.runtime.validation_feedback import (ValidationFeedback, build_validation_feedback,
    feedback_is_current)
from tracefix.runtime.verification import verification_binding
from tracefix.runtime.effects import file_resource, make_operation_executor
from tracefix.runtime.event_adapter import EventAdapter, EventCursor
from tracefix.runtime.diagnosis import (DiagnosisDraft, DiagnosisReport, binding_from_observation,
    symptom_query, validate_report)
from tracefix.knowledge.assembler import ContextAssembler, ContextWindowError
from tracefix.storage.artifacts import redact, sanitize
from tracefix.storage.presentation import label, readable, report_page
from tracefix.storage.reporting import collect_issues
from tracefix.storage.store import UnknownOperation
from tracefix.messages import error_message


class GraphState(TypedDict):
    data: dict
    next_node: str


def normalize_decision_evidence_refs(refs, known_refs, observation_ref, observation):
    aliases = {observation.get('id'): observation_ref,
               observation.get('screenshot_ref'): observation_ref}
    aliases = {key: value for key, value in aliases.items() if key and value}
    normalized = [aliases.get(ref, ref) for ref in refs]
    allowed = set(known_refs)
    if observation_ref:
        allowed.add(observation_ref)
    return list(dict.fromkeys(ref for ref in normalized if ref in allowed))


def repeated_action(fingerprints, count=2):
    return len(fingerprints) >= count and len(set(fingerprints[-count:])) == 1


def repeated_action_cycles(fingerprints, *, max_period=4, cycles=3):
    """Return the shortest repeated action period, if one is present."""
    if not fingerprints:
        return None
    for period in range(1, min(max_period, len(fingerprints)) + 1):
        required = period * cycles
        if len(fingerprints) < required:
            continue
        tail = fingerprints[-required:]
        if all(tail[index] == tail[index % period] for index in range(required)):
            return period
    return None


def stable_snapshot(snapshot):
    snapshot = re.sub(r'^- Console:.*$', '', snapshot, flags=re.MULTILINE)
    return re.sub(r'\s*\[ref=[^\]]+\]', '', snapshot)


_PUBLIC_CONTEXT_BINDING_KEYS = {
    'scope_id', 'run_id', 'revision', 'source_manifest', 'patch_hash',
    'page_generation', 'environment_digest', 'test_spec_hash', 'spec_hash',
}
_PUBLIC_CONTEXT_ITEM_KEYS = {'field', 'id', 'refs', 'binding', 'reason', 'ref', 'version',
                             'content_hash', 'coverage', 'source_coverage', 'span',
                             'character_span', 'line_count', 'off', 'start', 'end'}
_PUBLIC_CONTEXT_MARKERS = {'hidden', 'private', 'secret', 'oracle', 'held_out', 'held-out',
                           'heldout', 'final_scoring', 'final_scoring_only', 'final_oracle'}


def _is_private_context_marker(value):
    normalized = str(value).casefold().replace('-', '_')
    return normalized in _PUBLIC_CONTEXT_MARKERS or any(
        normalized.startswith(marker.replace('-', '_') + '_') for marker in _PUBLIC_CONTEXT_MARKERS)


def _public_context_value(value):
    if isinstance(value, str):
        return None if _is_private_context_marker(value) else value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        result = [_public_context_value(item) for item in value]
        return None if any(item is None and original is not None for item, original in zip(result, value)) else result
    if isinstance(value, dict):
        if any(_is_private_context_marker(key) for key in value):
            return None
        result = {}
        for key, item in value.items():
            projected = _public_context_value(item)
            if projected is None and item is not None:
                return None
            result[key] = projected
        return result
    return None


def _project_context_binding(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in _PUBLIC_CONTEXT_BINDING_KEYS:
        if key in value:
            projected = _public_context_value(value[key])
            if projected is not None:
                result[key] = projected
    return result


def _project_context_items(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        projected = {}
        for key in _PUBLIC_CONTEXT_ITEM_KEYS:
            if key not in item:
                continue
            if key == 'binding':
                projected[key] = _project_context_binding(item[key])
                continue
            value = _public_context_value(item[key])
            if value is None and item[key] is not None:
                projected = None
                break
            projected[key] = value
        if projected is not None:
            result.append(projected)
    return result


def public_context_manifest(manifest, *, run_id=None, revision=None):
    """Return the public workset index without context bodies or private markers."""
    if not isinstance(manifest, dict):
        return {}
    workset = manifest.get('workset') if isinstance(manifest.get('workset'), dict) else {}
    result = {
        'version': _public_context_value(manifest.get('version')),
        'binding': _project_context_binding(workset.get('binding') or manifest.get('binding')),
        'selected': _project_context_items(workset.get('selected')),
        'dropped': _project_context_items(workset.get('dropped')),
        'limitations': [value for value in (_public_context_value(item)
                                             for item in workset.get('limitations', []))
                        if value is not None],
    }
    if run_id is not None:
        result['binding']['run_id'] = run_id
    if revision is not None:
        result['binding']['revision'] = revision
    snapshot = workset.get('snapshot') if isinstance(workset.get('snapshot'), dict) else None
    if snapshot is not None:
        result['snapshot'] = {
            'ref': _public_context_value(snapshot.get('ref')),
            'version': _public_context_value(snapshot.get('version')),
            'content_hash': _public_context_value(snapshot.get('content_hash')),
            'binding': _project_context_binding(snapshot.get('binding')),
            'selected': _project_context_items(snapshot.get('selected')),
            'dropped': _project_context_items(snapshot.get('dropped')),
            'coverage': _public_context_value(snapshot.get('coverage')),
            'source_coverage': _public_context_value(snapshot.get('source_coverage')),
            'span': _public_context_value(snapshot.get('span') or snapshot.get('character_span')),
            'off': _public_context_value(snapshot.get('off')),
            'line_count': _public_context_value(snapshot.get('line_count')),
        }
    return redact(result)


class Engine:
    LOOP_STATE_WARNING = 2
    LOOP_STATE_LIMIT = 3
    LOOP_ACTION_WARNING = 2
    LOOP_ACTION_LIMIT = 3
    LOOP_ERROR_WARNING = 3
    LOOP_ERROR_LIMIT = 5
    LOOP_NO_PROGRESS_WARNING = 20
    LOOP_NO_PROGRESS_LIMIT = 40
    DIAGNOSIS_RETRY_LIMIT = 3

    def __init__(self, store, artifacts, scopes, context, profile, workspace, runner,
                 browser, model, retriever, source, checkpointer, notify=None,
                 rule_resolver=None, rule_library=None):
        self.store, self.artifacts, self.scopes, self.context = store, artifacts, scopes, context
        self.profile, self.workspace, self.runner = profile, workspace, runner
        self.browser, self.model, self.retriever, self.source = browser, model, retriever, source
        self.worker_model = None
        self.notify = notify
        self.rule_resolver = rule_resolver
        self.rule_library = rule_library
        self.control = None
        self.notes = []
        self.guidance_ledger = GuidanceLedger(self.artifacts.root.parent / 'guidance.sqlite3')
        self.assembler = ContextAssembler(context_window=int(os.getenv('TRACEFIX_CONTEXT_WINDOW', '131072')))
        self.memory = None
        self.retarget_child = None
        self.documents = None
        self.document_context = {}
        self._trace_sequences = {}
        self.event_adapter = EventAdapter(store)
        self.skills = SkillCatalog(Path(__file__).resolve().parents[5] / 'skills')
        self.skill_store = SkillStore(self.artifacts)
        self.graph = self._graph(checkpointer)

    def load_skill(self, s, name):
        prior = {(entry['name'], entry['version'], entry['content_hash']) for entry in s.skills_loaded}
        result = self.skill_store.load(s, self.skills, name, str(s.phase))
        identity = self._skill_event_identity(result)
        if (identity['name'], identity['version'], identity['content_hash']) not in prior:
            self.store.save(s)
            self.event(s, 'skill.loaded', identity)
        return result

    @staticmethod
    def _skill_event_identity(skill):
        identity = {key: skill[key] for key in ('name', 'version', 'content_hash')}
        if skill.get('snapshot_ref'):
            identity['snapshot_ref'] = skill['snapshot_ref']
        identity['references'] = [
            {key: reference[key] for key in ('path', 'content_hash', 'byte_count',
                                              'source_hash', 'frozen_source_hash', 'source_redacted')
             if key in reference}
            for reference in skill.get('references', [])
            if isinstance(reference, dict)
        ]
        return identity

    def inject_skills(self, s, context):
        selection_context = {**context, 'files': list(self.source.get('files', {}))}
        prior = {(entry['name'], entry['version'], entry['content_hash']) for entry in s.skills_loaded}
        result = self.skill_store.context(s, self.skills, str(s.phase), selection_context,
            explicit=list(context.get('load_skills', [])), limit=6)
        updated = False
        for skill in result['skills']:
            identity = self._skill_event_identity(skill)
            if (identity['name'], identity['version'], identity['content_hash']) not in prior:
                self.event(s, 'skill.loaded', identity)
                prior.add((identity['name'], identity['version'], identity['content_hash']))
                updated = True
        if updated:
            self.store.save(s)
        return {**context, **result}

    def _rule_snapshot(self, s):
        if not self.rule_resolver:
            return None
        if s.rule_snapshot_ref:
            try:
                return RuleSnapshot.model_validate(self.get(s, s.rule_snapshot_ref))
            except (FileNotFoundError, ValueError):
                pass
        if s.rule_refs:
            return RuleSnapshot(run_id=s.parent_run_id or s.run_id, parent_run_id=None,
                                refs=s.rule_refs, hash=s.rule_snapshot_hash or "")
        if self.rule_library and s.parent_run_id:
            snapshot = self.rule_library.snapshot(s.parent_run_id)
            if snapshot is None:
                raise ValueError('父 Run 尚未生成规则快照，无法派生')
            return snapshot
        return None

    def submit_guidance(self, s, text, *, level=None, expires='run', constraints=None, author='user'):
        if s.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                            RunStatus.FAILED, RunStatus.SUPERSEDED}:
            raise ValueError('只有活动 Run 可以接收运行中引导')
        entry = self.guidance_ledger.submit(s.run_id, s.scope_id, text, level=level,
            author=author, phase=s.phase, expires=expires, constraints=constraints)
        self.event(s, 'guidance.rejected' if entry.status == 'rejected' else 'guidance.queued',
                   entry.model_dump(mode='json'))
        return entry

    def guidance_status(self, s):
        return self.guidance_ledger.list(s.run_id, s.scope_id)

    def sync_guidance(self, s):
        # CLI/Web 通过同一 ledger 提交引导，运行时在安全边界同步并落入状态快照。
        entries = {entry.id: entry for entry in s.guidance}
        changed = False
        for entry in self.guidance_status(s):
            previous = entries.get(entry.id)
            if previous is not None and previous == entry:
                continue
            if entry.level == 'constraint' and entry.status == 'queued':
                # L2 先进入确定性执行门禁；模型尚未 ack 也必须遵守这些收窄约束。
                if (entry.constraints.allowed_actions is not None and s.test_spec_ref
                        and not set(entry.constraints.allowed_actions) <= set(self.spec(s).authorized_actions)):
                    entry.status, entry.rejection_reason = 'rejected', '动作约束只能收窄冻结的 TestSpec 权限'
                    self.event(s, 'guidance.rejected', entry.model_dump(mode='json'))
                else:
                    entry.status, entry.applied_step = 'applied', s.step
                    s.guidance_constraints.append(entry.constraints)
                    self.event(s, 'guidance.applied', entry.model_dump(mode='json'))
                self.guidance_ledger.save(entry)
            entries[entry.id] = entry
            changed = True
        if changed:
            s.guidance = list(entries.values())
            self.store.save(s)
        self.workspace.guidance_constraints = list(s.guidance_constraints)
        return next((entry for entry in s.guidance if entry.level == 'retarget'
                     and entry.status == 'queued' and entry.confirmed_at), None)

    def copy_retarget_evidence(self, previous, child):
        # 将证据及其嵌套 artifact 复制到子 Run，并重写引用，保证作用域内可访问。
        copied = {}
        def transfer(ref):
            if ref in copied:
                return copied[ref]
            raw = self.artifacts.read(previous.scope_id, previous.run_id, ref)
            extension = ref.rsplit('.', 1)[-1]
            if extension == 'json':
                def rewrite(value):
                    if isinstance(value, dict):
                        return {key: rewrite(item) for key, item in value.items()}
                    if isinstance(value, list):
                        return [rewrite(item) for item in value]
                    if isinstance(value, str) and self.artifacts.exists(previous.scope_id, previous.run_id, value):
                        return transfer(value)
                    return value
                raw = rewrite(json.loads(raw))
            copied[ref] = self.artifacts.put(child.scope_id, child.run_id, raw, extension,
                                           label='继承已校验的父任务证据')
            return copied[ref]
        child.evidence_refs = [transfer(ref) for ref in previous.evidence_refs if self.bundle_exists(previous, ref)]
        child.inherited_evidence_refs = list(child.evidence_refs)
        self.event(previous, 'guidance.evidence.inherited', {'child_run_id': child.run_id,
                   'references': copied})
        return child

    def guidance_context(self, s, ctx, logical_call):
        # 受保护区块同时携带引导内容、实际约束和本次输出必须回应的 ID。
        self.sync_guidance(s)
        entries = active_guidance(s, logical_call=logical_call)
        return {**ctx, 'user_guidance': [entry.model_dump(mode='json') for entry in entries],
                'effective_constraints': [entry.model_dump(mode='json') for entry in s.guidance_constraints],
                'guidance_ack_required': [entry.id for entry in entries]}

    def acknowledge_guidance(self, s, candidate, ctx):
        # ack 必须与本次注入集合一一对应；业务执行门禁仍独立校验约束。
        if not hasattr(candidate, 'guidance_ack'):
            return
        expected = set(ctx.get('guidance_ack_required', []))
        acknowledgements = candidate.guidance_ack
        ids = [entry.id for entry in acknowledgements]
        if set(ids) != expected or len(ids) != len(set(ids)):
            raise ModelOutputError('guidance_ack 必须逐条回应本次注入的引导，不能遗漏或编造 id')

    def record_guidance_ack(self, s, candidate):
        acknowledgements = {entry.id: entry.how_applied for entry in getattr(candidate, 'guidance_ack', [])}
        changed = False
        for entry in s.guidance:
            if entry.id not in acknowledgements:
                continue
            how_applied = acknowledgements[entry.id]
            # 仅允许 applied -> acknowledged；拒绝把未应用或已失效的引导恢复为已确认。
            if entry.status not in {'applied', 'acknowledged'}:
                raise ModelOutputError('guidance_ack 只能确认已应用的引导')
            if entry.status == 'acknowledged':
                # 相同确认是幂等空操作；新说明须单独审计，不能重发首次确认事件或覆盖旧证据。
                if entry.how_applied == how_applied:
                    continue
                previous_how_applied = entry.how_applied
                entry.how_applied = how_applied
                event_type = 'guidance.acknowledgement.updated'
                payload = {**entry.model_dump(mode='json'), 'previous_how_applied': previous_how_applied}
            else:
                entry.status, entry.how_applied = 'acknowledged', how_applied
                event_type = 'guidance.acknowledged'
                payload = entry.model_dump(mode='json')
            self.guidance_ledger.save(entry)
            self.event(s, event_type, payload)
            changed = True
        if changed:
            self.store.save(s)

    def active_rules(self, s, phase=None, observation=None, paths=None):
        if not self.rule_resolver:
            return []
        snapshot = self._rule_snapshot(s)
        rules = []
        if snapshot:
            for ref in snapshot.refs:
                try:
                    rule = self.rule_resolver.by_id(ref.id)
                    if rule.version != ref.version and self.rule_library:
                        rule = self.rule_library.version(ref.id, ref.version)
                    rules.append(rule)
                except (KeyError, FileNotFoundError) as error:
                    raise ValueError(f'冻结规则版本不可用：{ref.id}@{ref.version}') from error
        observation = observation if observation is not None else self.get(s, s.observation_ref) if s.observation_ref else {}
        paths = list(self.workspace.files()) if paths is None else list(paths)
        return [rule for rule in rules if rule_applies(rule, phase=phase or str(s.phase),
                url=observation.get('url') or s.url, paths=paths)]

    def inject_rules(self, s, context):
        cards = context.get('cards')
        paths = [card['path'] for card in cards if 'path' in card] if cards else None
        injection = render_rule_context(self.active_rules(s, observation=context.get('observation'), paths=paths))
        self.event(s, 'rules.injected', {'rule_ids': injection['rule_ids'],
            'rule_versions': injection['rule_versions'], 'tokens': injection['tokens'],
            'snapshot_hash': s.rule_snapshot_hash, 'context_hash': injection['snapshot_hash']})
        return {**context, 'detection_rules': injection, 'rule_snapshot_hash': s.rule_snapshot_hash}

    def ensure_rule_snapshot(self, s):
        if not self.rule_resolver or s.rule_snapshot_ref:
            return s
        parent = self._rule_snapshot(s)
        snapshot, rules = self.rule_resolver.resolve_snapshot(
            run_id=s.run_id, project_id=s.scope_id, phase="*", parent=parent,
            additional=s.additional_rule_ids, url=s.url, paths=self.workspace.files())
        ref = self.put(s, snapshot.model_dump(mode="json"), name="检测规则快照")
        delta = {"rule_snapshot_ref": ref, "rule_snapshot_hash": snapshot.hash,
                 "rule_refs": [item.model_dump(mode="json") for item in snapshot.refs]}
        if self.rule_library:
            self.rule_library.save_snapshot(snapshot)
        return self.changed(s, **delta)

    def validate_rule_refs(self, s, refs):
        allowed = {rule.id for rule in self.active_rules(s)}
        invalid = sorted(set(refs) - allowed)
        if invalid:
            raise ValueError("模型引用了本次注入集合之外的规则：" + ", ".join(invalid))

    def put(self, s, value, ext='json', *, name='数据记录'):
        return self.artifacts.put(s.scope_id, s.run_id, value, ext, label=f'{label(s.phase)}_{name}')

    def get(self, s, ref):
        return self.artifacts.json(s.scope_id, s.run_id, ref)

    def read_observation_channel(self, s, observation_ref, channel, *, start_line=1, end_line=None):
        """按 observation 引用展开未经模型裁剪的页面通道；行范围是 1-based 且闭区间。"""
        observation = self.get(s, observation_ref)
        ref = (observation.get('raw_refs') or {}).get(channel)
        if not ref:
            raise KeyError(f'观测未保存完整通道：{channel}')
        content = self.artifacts.read(s.scope_id, s.run_id, ref).decode('utf-8', errors='replace')
        if type(start_line) is not int or start_line < 1:
            raise ValueError('start_line 必须从 1 开始')
        lines = content.splitlines()
        if not lines and end_line is None:
            return ''
        last = len(lines) if end_line is None else end_line
        if type(last) is not int or last < start_line:
            raise ValueError('end_line 必须不小于 start_line')
        return '\n'.join(lines[start_line - 1:last])

    @staticmethod
    def _bounded_text(value, limit, locators=()):
        if not isinstance(value, str) or len(value) <= limit:
            return value, []
        lines = value.splitlines()
        if not lines:
            return value[:limit], [{'start': 1, 'end': 1}]
        needles = [f'{item.role} "{item.name}"' for item in locators if item]
        selected = {0, len(lines) - 1}
        for index, line in enumerate(lines):
            if any(needle.casefold() in line.casefold() for needle in needles):
                selected.update(range(max(0, index - 1), min(len(lines), index + 2)))
        output = []
        retained = set()
        for index in sorted(selected):
            candidate = '\n'.join(output + [lines[index]])
            if len(candidate) + 80 <= limit:
                output.append(lines[index])
                retained.add(index)
        omitted = []
        for index in range(len(lines)):
            if index in retained:
                continue
            line_number = index + 1
            if omitted and omitted[-1]['end'] == line_number - 1:
                omitted[-1]['end'] = line_number
            else:
                omitted.append({'start': line_number, 'end': line_number})
        output.append('[省略内容可按 observation artifact 与 view_omitted 行范围取回]')
        return '\n'.join(output)[:limit], omitted

    def bounded_observation_view(self, observation, *, locators=(), max_snapshot_chars=40_000,
                                 max_log_chars=12_000):
        """给模型的有界视图；observation artifact 本身永远不被该函数修改。"""
        view = copy.deepcopy(observation)
        snapshot, omitted = self._bounded_text(view.get('snapshot', ''), max_snapshot_chars, locators)
        view['snapshot'] = snapshot
        view_omitted = {'snapshot': omitted}
        for channel in ('console', 'network'):
            value, channel_omitted = self._bounded_text(view.get(channel, ''), max_log_chars)
            view[channel] = value
            view_omitted[channel] = channel_omitted
        changed = any(view_omitted.values())
        if isinstance(view.get('channels'), dict):
            refs = view.get('raw_refs') or {}
            metadata = (view.get('collection') or {}).get('channels', {})
            view['channels'] = {channel: {'status': (
                                          metadata.get(channel, {}).get('status')
                                          if isinstance(metadata.get(channel), dict) else metadata.get(channel))
                                          or ('available' if channel in refs else 'unavailable'),
                                          'ref': refs.get(channel)}
                               for channel in ('snapshot', 'console', 'network')}
            changed = True
        if changed:
            view['view_omitted'] = view_omitted
            view['view'] = 'bounded_model_projection'
        return view

    def model_observation_context(self, context):
        """仅转换模型输入中的观察，不改变 runtime.get() 返回的完整原件。"""
        projected = copy.deepcopy(context)
        target_locators = []
        spec = context.get('test_spec') or {}
        for item in spec.get('assertions', []) if isinstance(spec, dict) else []:
            if isinstance(item, dict) and item.get('locator'):
                target_locators.append(type('LocatorView', (), item['locator'])())
        def project(value):
            if isinstance(value, dict):
                result = dict(value)
                if 'snapshot' in result and ('type' in result or 'raw_refs' in result):
                    locators = list(target_locators)
                    for item in result.get('assertions', []):
                        if isinstance(item, dict) and item.get('locator'):
                            locators.append(type('LocatorView', (), item['locator'])())
                    return self.bounded_observation_view(result, locators=locators)
                return {key: project(item) for key, item in result.items()}
            if isinstance(value, list):
                return [project(item) for item in value]
            return value
        return project(projected)

    def spec(self, s):
        raw = self.get(s, s.test_spec_ref)
        if digest(raw) != s.test_spec_hash:
            raise ValueError('冻结的 TestSpec 内容与哈希不匹配')
        return TestSpec(**raw)

    def verification_context(self, s):
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        return verification_binding(s, digest(plan))

    def verification_passed(self, s, *, sampled_environment_digest=None):
        """只验证工作区及已采样摘要；最终执行门禁必须调用异步 live helper。"""
        try:
            # 纯证据检查仍须重读冻结工作区、完整补丁差异和已采样环境摘要。
            self.workspace.check_frozen(self.source)
            current_patch_hash = digest(
                self.workspace.diff(s.patch_base_commit or 'HEAD').encode()
            )
            current_environment_digest = (sampled_environment_digest
                                          if sampled_environment_digest is not None
                                          else getattr(self.runner, 'actual_digest', ''))
            if (not s.patch_hash or current_patch_hash != s.patch_hash
                    or not current_environment_digest
                    or current_environment_digest != s.environment_digest):
                return False
            validations = [Validation(**self.get(s, ref)) for ref in s.validation_refs]
            return verification_gate(s, validations, lambda ref: self.bundle_exists(s, ref),
                artifact_read=lambda ref: self.get(s, ref),
                artifact_read_bytes=lambda ref: self.artifacts.read(s.scope_id, s.run_id, ref))
        except Exception:
            return False

    async def runtime_verification_passed(self, s):
        """在最终成功边界重新采样运行容器；采样失败或漂移必须 fail-closed。"""
        try:
            live_environment_digest = await self.runner.inspect_images(runtime=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        if (type(live_environment_digest) is not str or not live_environment_digest
                or live_environment_digest != s.environment_digest):
            return False
        return self.verification_passed(s, sampled_environment_digest=live_environment_digest)

    def agent_instructions(self, s):
        if not s.agent_instructions_ref:
            return None
        return self.artifacts.read(s.scope_id, s.run_id, s.agent_instructions_ref).decode('utf-8')

    def bundle_exists(self, s, ref, depth=0):
        if depth > 8 or not self.artifacts.exists(s.scope_id,s.run_id,ref):
            return False
        if ref.endswith('.json'):
            value = self.get(s,ref)
            if isinstance(value,dict):
                for key in ('observation_ref','screenshot_ref','artifact_ref'):
                    if value.get(key) and not self.bundle_exists(s,value[key],depth+1):
                        return False
        return True

    def event(self, s, kind, payload=None):
        e = self.store.event(s, kind, payload)
        return self._notify(e)

    def warn_retrieval_degraded(self, s):
        # 检索和记忆共用后端，但分别发出事件，便于各展示层准确说明能力降级。
        # 两类事件按 reason 独立去重，已有 retrieval 事件不能阻止 memory 事件写入。
        degradation = self.retriever.degradation()
        if not degradation:
            return
        events = self.store.trace(s.run_id, s.scope_id)
        reason = degradation['reason']
        if not any(event['type'] == 'retrieval.degraded' and
                   event.get('payload', {}).get('reason') == reason
                   for event in events):
            self.event(s, 'retrieval.degraded', degradation)
        if not any(event['type'] == 'memory.degraded' and
                   event.get('payload', {}).get('reason') == reason
                   for event in events):
            self.event(s, 'memory.degraded', degradation)

    def _notify(self, event):
        scope, run = event['scope_id'], event['run_id']
        key = (scope, run)
        if key not in self._trace_sequences:
            self._trace_sequences[key] = self.artifacts.trace_position(scope, run)
        cursor = EventCursor(scope, run, self._trace_sequences[key]).encode()
        batch = self.event_adapter.read(run, scope, cursor)
        if batch.snapshot is not None:
            return batch
        for entry in batch.events:
            self.artifacts.append_trace(entry)
            self._trace_sequences[key] = entry['seq']
            if self.notify:
                try:
                    self.notify(entry)
                except Exception:
                    pass
        return batch

    def read_events(self, s, cursor=None):
        return self.event_adapter.read(s.run_id, s.scope_id, cursor)

    def changed(self, s, **delta):
        persisted = self.store.load(s.run_id, s.scope_id)
        if 'loop_no_progress_steps' not in delta:
            progress_fields = {
                'phase', 'observation_ref', 'evidence_refs', 'failure_signatures',
                'validation_index', 'patch_hash', 'reproduced', 'source_aligned',
            }
            progressed = any(field in delta and delta[field] != getattr(s, field)
                             for field in progress_fields)
            delta['loop_no_progress_steps'] = 0 if progressed else s.loop_no_progress_steps + 1
        new = reduce_state(s, persisted.revision, **delta)
        self.store.save(new)
        self.event(new, 'state.changed', {'phase': new.phase, 'status': new.run_status})
        return new

    def _loop_state_fingerprint(self, s):
        observation = self.get(s, s.observation_ref) if s.observation_ref else None
        snapshot = stable_snapshot(observation.get('snapshot', '')) if observation else ''
        pending = s.pending_action
        if isinstance(pending, dict):
            pending = {key: value for key, value in pending.items()
                       if key not in {'observation_id', 'element_ref'}}
        return digest([str(s.phase), s.trial, s.validation_index, s.replay_index,
                       s.diagnosis_retry_count,
                       snapshot, pending, s.patch_hash,
                       s.last_error_signature or (s.failure_signatures[-1]
                                                  if s.failure_signatures else None)])

    @staticmethod
    def _error_signature(error):
        message = re.sub(r'\s+', ' ', sanitize(str(error))).strip()
        return digest([type(error).__name__, message])

    def _loop_assessment(self, s):
        fingerprint = self._loop_state_fingerprint(s)
        state_history = s.loop_state_fingerprints + [fingerprint]
        state_count = state_history.count(fingerprint)
        action_period = repeated_action_cycles(s.action_fingerprints,
                                               cycles=self.LOOP_ACTION_LIMIT)
        error_count = 0
        if s.last_error_signature:
            for value in reversed(s.loop_error_signatures):
                if value != s.last_error_signature:
                    break
                error_count += 1
            if not error_count or s.loop_error_signatures[-1] != s.last_error_signature:
                error_count += 1
        signals = []
        if state_count >= self.LOOP_STATE_LIMIT:
            signals.append({'kind': 'state_repeated', 'fingerprint': fingerprint,
                            'count': state_count})
        if action_period:
            signals.append({'kind': 'action_cycle', 'period': action_period,
                            'cycles': self.LOOP_ACTION_LIMIT})
        if error_count >= self.LOOP_ERROR_LIMIT:
            signals.append({'kind': 'error_repeated', 'signature': s.last_error_signature,
                            'count': error_count})
        if s.loop_no_progress_steps >= self.LOOP_NO_PROGRESS_LIMIT:
            signals.append({'kind': 'no_progress', 'steps': s.loop_no_progress_steps})
        warnings = []
        if state_count == self.LOOP_STATE_WARNING:
            warnings.append({'kind': 'state_repeated', 'fingerprint': fingerprint,
                             'count': state_count})
        if len(s.action_fingerprints) >= self.LOOP_ACTION_WARNING:
            period = repeated_action_cycles(s.action_fingerprints,
                                            cycles=self.LOOP_ACTION_WARNING)
            if period:
                warnings.append({'kind': 'action_cycle', 'period': period,
                                 'cycles': self.LOOP_ACTION_WARNING})
        if error_count == self.LOOP_ERROR_WARNING:
            warnings.append({'kind': 'error_repeated', 'signature': s.last_error_signature,
                             'count': error_count})
        if s.loop_no_progress_steps == self.LOOP_NO_PROGRESS_WARNING:
            warnings.append({'kind': 'no_progress', 'steps': s.loop_no_progress_steps})
        return fingerprint, warnings, signals

    def _mark_loop(self, s, fingerprint, warnings, signals):
        evidence = {'signals': signals, 'state_fingerprint': fingerprint,
                    'state_tail': (s.loop_state_fingerprints + [fingerprint])[-12:],
                    'action_tail': s.action_fingerprints[-12:],
                    'error_tail': s.loop_error_signatures[-12:],
                    'step': s.step}
        s = self.changed(s, loop_state_fingerprints=s.loop_state_fingerprints + [fingerprint],
                         loop_warnings=s.loop_warnings + warnings,
                         loop_evidence=evidence, abnormal_termination=True,
                         phase=Phase.FINALIZE, outcome=Outcome.LOOP_DETECTED,
                         run_status=RunStatus.RUNNING, pending_action=None,
                         error='检测到死循环')
        self.event(s, 'loop.detected', evidence)
        return s

    def _record_loop_boundary(self, s):
        fingerprint, warnings, signals = self._loop_assessment(s)
        if signals:
            return self._mark_loop(s, fingerprint, warnings, signals), True
        history = s.loop_state_fingerprints + [fingerprint]
        if warnings:
            s = self.changed(s, loop_state_fingerprints=history,
                             loop_warnings=s.loop_warnings + warnings)
            for warning in warnings:
                self.event(s, 'loop.suspected', warning)
            return s, False
        s = self.changed(s, loop_state_fingerprints=history)
        return s, False

    def operation_resources(self, name, intent):
        if isinstance(intent, dict) and intent.get('resources') is not None:
            return intent['resources']
        if name == 'scenario.reset':
            return ['browser', 'sandbox']
        if name.startswith('browser'):
            return ['browser']
        if name.startswith(('sandbox.', 'baseline.', 'verify.')):
            return ['sandbox']
        if name in {'workspace.patch', 'local.commit'}:
            return [file_resource(self.workspace.root)]
        return ['*']

    def operation_intent(self, s, name, intent, *, idempotency_key=None):
        execution = {
            'phase': str(s.phase), 'step': s.step, 'replay_index': s.replay_index,
            'trial': s.trial, 'validation_index': s.validation_index,
            'patch_hash': s.patch_hash,
        }
        semantic = {key: value for key, value in intent.items() if key not in {'call_id', 'tool_call_id'}}
        if idempotency_key:
            return {**semantic, 'resources': self.operation_resources(name, intent)}
        return {**semantic, 'execution': execution,
                'resources': self.operation_resources(name, intent)}

    async def operation(self, s, name, intent, fn, reconcile=None, idempotency_key=None,
                        tool_call_id=None):
        prepared = self.operation_intent(s, name, intent, idempotency_key=idempotency_key)
        correlation = tool_call_id if tool_call_id is not None else intent.get('tool_call_id')
        if correlation is not None or intent.get('call_id') is not None:
            self.event(s, 'operation.called', {'name': name, 'call_id': intent.get('call_id'),
                'tool_call_id': correlation, 'execution': prepared.get('execution')})
        executor = make_operation_executor(self.store, s, notify=self._notify)
        try:
            return await executor(name, prepared, fn, idempotency_key=idempotency_key,
                                  reconcile=reconcile)
        except (MCPConnectionError, MCPActionUnknown) as error:
            self.event(s, 'tool.error', {'call_id': intent.get('call_id'),
                'tool_call_id': tool_call_id if tool_call_id is not None else intent.get('tool_call_id'),
                'operation_id': getattr(error, 'tracefix_operation_id', None),
                'error': sanitize(error_message(error)), 'status': error.status,
                'category': error.category, 'details': redact(error.details)})
            raise

    async def model_call(self, s, schema, ctx, image=None, validate_output=None):
        ctx = {**ctx, 'phase': str(s.phase), 'execution_mode': s.execution_mode}
        # Worker calls select locally; never replace the shared supervisor model.
        selected_model = (self.worker_model if ctx.get('worker_depth') == 1
                          and not ctx.get('worker_same_model')
                          and self.worker_model is not None else self.model)
        logical_call = s.budget.model_calls + 1
        ctx = self.guidance_context(s, ctx, logical_call)
        if self.rule_resolver:
            ctx = self.inject_rules(s, ctx)
        ctx = self.inject_skills(s, ctx)
        original_validation = validate_output

        def validate_candidate(candidate):
            self.acknowledge_guidance(s, candidate, ctx)
            if original_validation:
                original_validation(candidate)
            if hasattr(candidate, 'rule_refs'):
                allowed = set(ctx.get('detection_rules', {}).get('rule_ids', []))
                if set(candidate.rule_refs) - allowed:
                    raise ModelOutputError('模型引用了当前动态注入集合之外的规则')

        validate_output = validate_candidate
        if s.continuation_instruction:
            ctx = {**ctx, 'user_continuation': s.continuation_instruction,
                   'continuation_count': s.continuation_count,
                   'previous_termination': s.continuation_markers[-1] if s.continuation_markers else None}
        if s.loop_warnings:
            ctx = {**ctx, 'loop_warning': s.loop_warnings[-1]}
        if s.error:
            ctx = {**ctx, 'runtime_feedback': s.error,
                   'runtime_error_details': s.error_details}
        if self.memory is not None and not ctx.get('worker_readonly_investigation'):
            # 每次调用重新读取 L1/L2，让工具写入的新记忆立即生效；L2 按源码 manifest 隔离。
            ctx['working_memory'] = self.memory.working_memory(
                s.scope_id, s.run_id, query=s.goal, phase=s.phase,
                source_manifest=s.source_manifest, patch_hash=s.patch_hash,
                page_generation=getattr(s, 'page_generation', None))
            ctx['job_memory'] = self.memory.job_memory(
                s.scope_id, s.job_id, s.source_manifest,
                cross_run=getattr(self.memory, 'cross_run', None), query=s.goal,
                phase=s.phase, limit=6)
        ctx = self.model_observation_context(ctx)
        budget = s.budget

        def attempt(model, request, attempt_number):
            nonlocal budget
            # 直到请求 attempt 才标记提示已应用；once 引导在同一逻辑调用内继续有效。
            for entry in active_guidance(s, logical_call=logical_call):
                if entry.status == 'queued':
                    entry.status, entry.applied_step, entry.applied_call = 'applied', s.step, logical_call
                    self.guidance_ledger.save(entry)
                    self.event(s, 'guidance.applied', entry.model_dump(mode='json'))
                elif entry.applied_call is None:
                    entry.applied_call = logical_call
                    self.guidance_ledger.save(entry)
            budget = s.budget
            budget = budget.charge('model_calls')
            s.budget = budget
            self.store.save(s)
            exchange = {'exchange_id': request.get('logical_exchange_id') or new_id('model'),
                        'logical_exchange_id': request.get('logical_exchange_id'),
                        'logical_call': logical_call,
                        'tool_round': request.get('tool_round', 0),
                        'attempt': attempt_number, 'phase': str(s.phase),
                        'schema': schema.__name__, 'model': model}
            request_ref = self.put(s, {**exchange, 'request': request},
                                   name=f'模型调用{logical_call:03d}_尝试{attempt_number}_输入')
            exchange['request_ref'] = request_ref
            s.model_exchange_refs.append(request_ref)
            self.store.save(s)
            self.event(s, 'model.started', exchange)
            self.event(s, 'model.request.persisted', exchange)
            for message in request.get('json', {}).get('messages', []):
                if message.get('role') != 'user':
                    continue
                content = message.get('content')
                if isinstance(content, list):
                    content = next((part['text'] for part in content if part.get('type') == 'text'), '')
                try:
                    injected = json.loads(content)['context'].get('skills', [])
                except (TypeError, ValueError, KeyError):
                    continue
                self.event(s, 'skills.injected', {**exchange, 'skills': [
                    self._skill_event_identity(item) for item in injected]})
                break
            return exchange

        def response(exchange, raw):
            exchange = exchange or {'exchange_id': new_id('model'), 'schema': schema.__name__,
                                    'phase': str(s.phase)}
            response_ref = self.put(s, {**exchange, 'response': raw},
                                    name=f'模型调用{logical_call:03d}_尝试{exchange.get("attempt", 1)}_输出')
            s.model_exchange_refs.append(response_ref)
            self.store.save(s)
            body = raw.get('body') if isinstance(raw, dict) else None
            choices = body.get('choices', []) if isinstance(body, dict) else []
            choices = choices if isinstance(choices, list) else []
            # 只保存供应商实际返回的 reasoning/reasoning_content，不从正文推测或生成推理。
            # 独立 artifact 通过 response_ref 关联完整响应，并保留流中断标记供审计判断完整性。
            reasoning = reasoning_records(body)
            reasoning_ref = None
            if reasoning:
                reasoning_ref = self.put(s, {**exchange, 'response_ref': response_ref,
                    'reasoning': reasoning, 'stream_incomplete': bool(raw.get('stream_incomplete'))},
                    name=f'模型调用{logical_call:03d}_尝试{exchange.get("attempt", 1)}_推理记录')
                s.reasoning_refs.append(reasoning_ref)
                self.store.save(s)
                self.event(s, 'model.reasoning.persisted', {**exchange,
                    'reasoning_ref': reasoning_ref, 'response_ref': response_ref,
                    'stream_incomplete': bool(raw.get('stream_incomplete'))})
            self.event(s, 'model.response.persisted', {**exchange,
                'response_ref': response_ref, 'http_status': raw.get('http_status'),
                'reasoning_content_present': bool(reasoning), 'reasoning_ref': reasoning_ref})

        def error(exchange, raw):
            exchange = exchange or {'exchange_id': new_id('model'), 'schema': schema.__name__,
                                    'phase': str(s.phase)}
            raw = raw if isinstance(raw, dict) else {'message': sanitize(str(raw))}
            message = re.sub(r'\s+', ' ', sanitize(str(raw.get('message') or raw.get('type') or raw.get('category') or raw))).strip()
            signature = digest([raw.get('type') or raw.get('category'), message])
            s.last_error_signature = signature
            s.loop_error_signatures.append(signature)
            error_ref = self.put(s, {**exchange, 'error': raw},
                                 name=f'模型调用{logical_call:03d}_尝试{exchange.get("attempt", 1)}_错误')
            s.model_exchange_refs.append(error_ref)
            self.store.save(s)
            self.event(s, 'model.error.persisted', {**exchange, 'error_ref': error_ref,
                'status': raw.get('status'), 'category': raw.get('category'),
                'details': redact(raw)})
            trailing = 0
            for value in reversed(s.loop_error_signatures):
                if value != signature:
                    break
                trailing += 1
            if (trailing >= self.LOOP_ERROR_LIMIT
                    and raw.get('status') not in {'UNKNOWN_OPERATION', 'WAITING_NETWORK'}):
                raise ModelOutputError('检测到同一模型错误连续重复')

        def usage(u):
            nonlocal budget
            budget = s.budget
            data = budget.model_dump()
            data['tokens'] += int(u['total_tokens'])
            budget = type(budget)(**data)
            s.budget = budget
            self.store.save(s)
            self.event(s, 'model.usage', {'usage': u, 'cost_is_configured_estimate': False})

        def tool_result(exchange, raw):
            record = {**(exchange or {}), 'tool_result': raw}
            result_ref = self.put(s, record, name=f'模型调用{logical_call:03d}_工具结果')
            s.model_exchange_refs.append(result_ref)
            self.store.save(s)
            self.event(s, 'model.tool.result.persisted', {
                'result_ref': result_ref, 'tool_call_id': raw['message']['tool_call_id'],
                'logical_exchange_id': raw['logical_exchange_id'], 'tool_round': raw['tool_round'],
                'reused': raw['reused']})

        validation = {'validate_output': validate_output} if (original_validation or ctx['guidance_ack_required'] or
            (self.rule_resolver and getattr(selected_model, 'supports_tool_executor', False))) else {}
        recovery = s.error_details or {}
        recovering_model_request = (recovery.get('status') == 'WAITING_NETWORK'
            and recovery.get('request_status') == 'not_sent'
            and not recovery.get('requires_manual_review') and not recovery.get('requires_new_run'))
        if getattr(selected_model, 'supports_tool_executor', False):
            def current_context():
                nonlocal ctx
                ctx = self.guidance_context(s, ctx, logical_call)
                ctx = self.inject_skills(s, ctx)
                ctx = self.model_observation_context(ctx)
                return ctx
            validation['context_provider'] = current_context
            failed_exchange_id = recovery.get('logical_exchange_id')
            if recovering_model_request and failed_exchange_id:
                for audit_ref in reversed(s.model_exchange_refs):
                    audit = self.get(s, audit_ref)
                    request = audit.get('request', {})
                    if (audit.get('schema') != schema.__name__
                            or request.get('logical_exchange_id') != failed_exchange_id):
                        continue
                    history = request.get('json', {}).get('messages', [])
                    if history:
                        validation['messages'] = history
                        validation['preserve_resumed_request'] = True
                        for message in history:
                            if message.get('role') != 'user':
                                continue
                            content = message['content']
                            if isinstance(content, list):
                                content = next(part['text'] for part in content if part.get('type') == 'text')
                            ctx = json.loads(content)['context']
                            break
                        break
            async def execute_tool(name, arguments, call_id):
                nonlocal ctx
                if ctx.get('worker_readonly_investigation'):
                    raise PermissionError('只读调查 Worker 未获浏览器动作权限')
                self.sync_guidance(s)
                self.scopes.assert_current(self.context)
                action = Gateway.browser_action(name, arguments)
                observation = self.get(s, s.observation_ref) if s.observation_ref else None
                try:
                    self.browser.policy.browser(s, action, self.spec(s), observation)
                except (ValueError, PermissionError, ModelOutputError) as exc:
                    if isinstance(exc, ModelOutputError) and (exc.status != 'FAILED'
                            or exc.details.get('requires_manual_review')):
                        raise
                    self.event(s, 'tool.rejected', {'tool_call_id': call_id,
                        'action': action.model_dump(), 'error': sanitize(str(exc))})
                    return {'isError': True, 'error': {'type': type(exc).__name__,
                        'message': sanitize(str(exc)), 'executed': False},
                        'observation_ref': s.observation_ref, 'observation': observation}
                ref = await self.act(s, action, tool_call_id=call_id)
                canonical = action.model_copy(update={'observation_id': None, 'element_ref': None,
                                                      'page_generation': None})
                plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
                plan.append(canonical.model_dump())
                plan_ref = self.put(s, plan, name='操作重放计划')
                fingerprint = digest([canonical.model_dump(), stable_snapshot(observation['snapshot']) if observation else ''])
                updated = self.changed(s, observation_ref=ref, replay_plan_ref=plan_ref,
                    step=s.step+1, action_fingerprints=(s.action_fingerprints+[fingerprint])[-12:])
                for field in type(s).model_fields:
                    setattr(s, field, getattr(updated, field))
                observation = self.get(s, ref)
                if self.rule_resolver:
                    ctx = self.inject_rules(s, {**ctx, 'observation': observation})
                ctx = self.guidance_context(s, {**ctx, 'observation': observation}, logical_call)
                return {'observation_ref': ref, 'observation': observation}
            validation['tool_executor'] = execute_tool
            validation['on_tool_result'] = tool_result
        runtime_tools = build_runtime_tools(self, s, schema, ctx, validate_output=validate_output)
        if schema in {DiagnosisDraft, DiagnosisReport}:
            for name in ('Read', 'Grep', 'Glob'):
                handler = runtime_tools.handlers.get(name)
                if handler is None:
                    continue
                async def read_evidence(arguments, call_id, handler=handler, name=name):
                    result = await handler(arguments, call_id)
                    ref = self.put(s, {'tool': name, 'arguments': arguments.model_dump(mode='json'),
                        'result': result, 'source_manifest': s.source_manifest}, name='诊断工具证据')
                    s.evidence_refs.append(ref)
                    self.store.save(s)
                    ctx['available_evidence_refs'] = list(dict.fromkeys(
                        ctx.get('available_evidence_refs', []) + s.evidence_refs))
                    ctx['evidence_refs'] = ctx['available_evidence_refs']
                    self.event(s, 'diagnosis.source.read', {'tool': name, 'artifact_ref': ref})
                    return {**result, 'artifact_ref': ref}
                runtime_tools.handlers[name] = read_evidence
        # 工具写入共享运行时 operation 回执，完整结果交由 artifact 保存。
        async def tool_operation(name, intent, fn, *, idempotency_key=None):
            return await self.operation(s, name, intent, fn, idempotency_key=idempotency_key)
        tool_pipeline = runtime_tools.pipeline(
            operation=tool_operation,
            emit=lambda kind, payload: self.event(s, kind, payload),
            store_artifact=lambda value: self.put(s, value, name='工具完整结果'),
            scope_check=lambda: self.scopes.assert_current(self.context))
        def context_record(manifest, compacted):
            # 每次实际组装落盘 manifest，记录模型可见区块的 hash、token 和裁剪项。
            # 事件仅引用清单及计数，避免把大段上下文重复塞入运行日志。
            manifest_ref = self.put(s, manifest, name='上下文组装清单')
            if manifest_ref not in s.context_manifest_refs:
                s.context_manifest_refs.append(manifest_ref)
                self.store.save(s)
            public_manifest = public_context_manifest(manifest, run_id=s.run_id, revision=s.revision)
            self.event(s, 'context.assembled', {'manifest_ref': manifest_ref,
                        'compacted': compacted, 'tokens': manifest['tokens_after'],
                        'workset': public_manifest})
            if compacted:
                self.event(s, 'context.compacted', {'manifest_ref': manifest_ref,
                    'tokens_before': manifest['tokens_before'], 'tokens_after': manifest['tokens_after'],
                    'workset': public_manifest})
        generation_kwargs = dict(validation)
        if getattr(selected_model, 'supports_tool_executor', False) and runtime_tools.registry.visible(s.phase):
            generation_kwargs.update(tool_registry=runtime_tools.registry, tool_pipeline=tool_pipeline)
        if getattr(selected_model, 'supports_context_assembler', False):
            # Gateway 支持时由其在每个 attempt 内重新组装，覆盖重试和工具多轮的新上下文。
            generation_kwargs.update(context_assembler=self.assembler, on_context=context_record)
        else:
            assembly = self.assembler.assemble(ctx)
            ctx = assembly.context
            context_record(assembly.manifest, assembly.compacted)
        result = await selected_model.generate(schema, ctx, image=image,
            agent_instructions=self.agent_instructions(s), on_attempt=attempt,
            on_response=response, on_error=error, on_usage=usage, **generation_kwargs)
        if tool_pipeline.submission_value is not None:
            result = type(result)(tool_pipeline.submission_value, result.usage,
                                  result.model_revision, result.finish_reason)
        validate_candidate(result.value)
        self.record_guidance_ack(s, result.value)
        if recovering_model_request:
            s.error = None
            s.error_details = None
            self.store.save(s)
        self.event(s, 'model.called', {'model_revision': result.model_revision, 'usage': result.usage,
                                      'finish_reason': result.finish_reason})
        if isinstance(result.value, (Decision, PatchProposal)):
            self.event(s, 'model.decision', {'summary': result.value.summary,
                'evidence_refs': result.value.evidence_refs,
                'action': result.value.action.model_dump() if isinstance(result.value, Decision) else
                          {'kind': 'propose_patch', 'files': [edit.path for edit in result.value.edits]}})
        return result.value

    async def capture(self, s, raw, action=None):
        previous = self.get(s, s.observation_ref) if s.observation_ref else None
        raw = dict(raw)
        png = raw.pop('png')
        if (type(png) is not bytes or not png.startswith(b'\x89PNG\r\n\x1a\n')
                or len(png) <= 8):
            raise ValueError('浏览器截图必须是非空 PNG bytes')
        if self.profile.screenshot_redaction != 'public_demo':
            raise PermissionError('私有截图在捕获前需要经过批准的脱敏适配器处理')
        screenshot = self.put(s, png, 'png', name='页面截图')
        channels = raw.get('channels') if isinstance(raw.get('channels'), dict) else {}
        raw_refs = dict(raw.get('raw_refs') or {})
        for channel in ('snapshot', 'console', 'network'):
            value = channels.get(channel, raw.get(channel))
            if isinstance(value, str):
                raw_refs[channel] = self.put(s, value, 'txt', name=f'观察原件_{channel}')
        if raw_refs:
            raw['raw_refs'] = raw_refs
        collection = dict(raw.get('collection') or {})
        collection.setdefault('channels', {
            channel: ('available' if isinstance(channels.get(channel, raw.get(channel)), str) else 'unavailable')
            for channel in ('snapshot', 'console', 'network')})
        collection.setdefault('truncated', any(
            isinstance(metadata, dict) and metadata.get('status') == 'truncated'
            for metadata in collection['channels'].values()))
        collection.setdefault('observed_at', raw.get('observed_at', time.time()))
        raw['collection'] = collection
        raw.setdefault('observed_at', collection['observed_at'])
        raw.setdefault('content_version', collection.get('content_version') or digest(raw.get('snapshot', '')))
        raw.update(self.verification_context(s), type='gui_observation',
                   screenshot_ref=screenshot, screenshot_hash=digest(png))
        raw['redaction'] = 'public_demo_no_credentials'
        observation_ref = self.put(s, raw, name='页面观察')
        if self.rule_library and self.rule_resolver:
            for rule in self.active_rules(s, str(s.phase), observation=raw):
                current_action = action or s.pending_action
                if isinstance(current_action, BrowserAction):
                    current_action = current_action.model_dump(mode='json')
                for finding in evaluate_oracle(rule, raw, job_id=s.run_id, run_id=s.run_id,
                                               evidence_ref=observation_ref, previous_observation=previous,
                                               action=current_action):
                    saved = self.rule_library.save_finding(finding)
                    self.event(s, 'finding.created', {'finding_id': saved.id, 'rule_id': saved.rule_id,
                                                     'rule_version': saved.rule_version,
                                                     'status': saved.status, 'evidence_ref': observation_ref,
                                                     'finding': saved.model_dump(mode='json'),
                                                     'expectation': rule.name})
        return observation_ref

    async def act(self, s, action, *, frozen=False, tool_call_id=None):
        self.sync_guidance(s)
        self.scopes.assert_current(self.context)
        spec = self.spec(s)
        obs = self.get(s, s.observation_ref) if s.observation_ref else None
        if action.page_generation is not None and (not obs or action.page_generation != obs.get('page_generation')):
            raise ValueError('浏览器页面代次已过期，请先获取最新观察')
        if action.preconditions:
            if not obs:
                raise ValueError('动作前置断言缺少当前观察')
            precondition_result = assertions(obs.get('snapshot', ''), action.preconditions)
            if not precondition_result['passed']:
                raise ValueError('动作前置条件不满足：' + json.dumps(precondition_result['assertions'], ensure_ascii=False))
        if frozen and action.locator:
            try:
                element_ref = resolve_locator(obs['snapshot'], action.locator)
            except ValueError as e:
                # 重放的是已记录的动作，没有可回退的模型；定位器不再唯一绑定说明该场景无法重放，
                # 这不是基础设施故障，应作为无结论收尾。
                raise ReplayUnbound('已记录的动作无法在当前页面重放：' + str(e)) from e
            action = action.model_copy(update={'observation_id': obs['id'], 'element_ref': element_ref,
                                               'page_generation': obs.get('page_generation')})
        elif frozen and action.kind == 'press':
            if not obs or not obs.get('id'):
                raise ReplayUnbound('已记录的按键动作缺少当前页面观测，无法重放')
            action = action.model_copy(update={'observation_id': obs['id'],
                                               'page_generation': obs.get('page_generation')})
        self.browser.policy.browser(s, action, spec, obs)
        s.budget = s.budget.charge('browser_actions')
        self.store.save(s)
        async def perform():
            raw = await self.browser.action(action)
            return {'observation_ref': await self.capture(s, raw, action)}
        intent = action.model_dump()
        receipt = await self.operation(s, 'browser', intent, perform, tool_call_id=tool_call_id)
        current_ref = receipt['observation_ref']
        current = self.get(s, current_ref)
        if action.postconditions:
            result = assertions(current.get('snapshot', ''), action.postconditions)
            if not result['passed']:
                raise ValueError('动作后置断言不满足：' + json.dumps(result['assertions'], ensure_ascii=False))
        wait = action.wait
        if wait and wait.assertions:
            deadline = time.monotonic() + wait.timeout_seconds
            observations = 0
            while True:
                result = assertions(current.get('snapshot', ''), wait.assertions)
                if result['passed']:
                    break
                if observations >= wait.max_observations or time.monotonic() >= deadline:
                    raise ValueError('可观察等待超时：' + json.dumps(result['assertions'], ensure_ascii=False))
                await asyncio.sleep(min(wait.interval_seconds, max(0, deadline - time.monotonic())))
                raw = await self.browser.action(BrowserAction(kind='observe'))
                current_ref = await self.capture(s, raw)
                current = self.get(s, current_ref)
                observations += 1
        return current_ref

    def _graph(self, saver):
        graph = StateGraph(GraphState)
        names = ['prelude', 'prepare', 'decide', 'execute', 'explore_gate', 'reproduce',
                 'diagnose', 'patch', 'verify', 'review', 'paused', 'finalize']
        for name in names:
            async def wrapped(data, node=name):
                s = RunState(**data['data'])
                # Usage committed before a crash takes precedence over old graph state.
                saved = self.store.load(s.run_id, s.scope_id)
                s.guidance = saved.guidance
                s.guidance_constraints = saved.guidance_constraints
                for metric in ('model_calls', 'browser_actions', 'patches', 'subtasks', 'tokens', 'cost_usd'):
                    setattr(s.budget, metric, max(getattr(s.budget, metric), getattr(saved.budget, metric)))
                try:
                    return await getattr(self, node)(s, data.get('next_node', 'prepare'))
                except UnknownOperation as e:
                    if s.execution_mode == 'batch':
                        s = self.finish_error(self.store.load(s.run_id, s.scope_id), e, details={
                            'status': 'UNKNOWN_OPERATION', 'category': 'missing_receipt',
                            'requires_manual_review': True})
                        self.event(s, 'run.error', {'error': s.error,
                            'status': s.run_status, 'error_details': s.error_details})
                        return self.output(s, 'finalize')
                    # 节点可能已经提交过变更，终止写入以持久化的最新版本为基准。
                    s = self.changed(self.store.load(s.run_id, s.scope_id),
                                     run_status=RunStatus.PAUSED, error='UNKNOWN_OPERATION（未知操作）：'+str(e),
                                     error_details={'status': 'UNKNOWN_OPERATION',
                                         'category': 'missing_receipt',
                                         'requires_manual_review': True})
                    return {'data': s.model_dump(mode='json'), 'next_node': 'paused'}
                except GraphInterrupt:
                    raise
                except Exception as e:
                    if node == 'finalize':
                        raise
                    if isinstance(e, ContextWindowError) or getattr(e, 'status', None) == 'UNKNOWN_OPERATION':
                        if s.execution_mode == 'batch':
                            s = self.finish_error(s, e, details={
                                **getattr(e, 'details', {}),
                                'status': getattr(e, 'status', 'PAUSED'),
                                'category': getattr(e, 'category', 'runtime')})
                            self.event(s, 'run.error', {'error': s.error,
                                'status': s.run_status, 'error_details': s.error_details})
                            return self.output(s, 'finalize')
                        error_details = {**getattr(e, 'details', {}), 'status': getattr(e, 'status', 'PAUSED'),
                                         'category': getattr(e, 'category', 'runtime')}
                        if isinstance(e, (MCPConnectionError, MCPActionUnknown)):
                            error_details.setdefault('source', 'browser')
                        elif isinstance(e, ModelError):
                            error_details.setdefault('source', 'model')
                        s = self.changed(self.store.load(s.run_id, s.scope_id),
                            run_status=RunStatus.PAUSED, error=sanitize(error_message(e)),
                            error_details={**error_details, 'requires_manual_review': True})
                        self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details})
                        return self.output(s, 'paused')
                    if s.execution_mode == 'batch' and getattr(e, 'tracefix_operation_id', None):
                        s = self.finish_error(s, e, details={
                            'status': 'UNKNOWN_OPERATION', 'category': 'missing_receipt',
                            'operation_id': e.tracefix_operation_id,
                            'requires_manual_review': True})
                        self.event(s, 'run.error', {'error': s.error,
                            'status': s.run_status, 'error_details': s.error_details})
                        return self.output(s, 'finalize')
                    current = self.store.load(s.run_id, s.scope_id)
                    signature = self._error_signature(e)
                    s = self.changed(current, last_error_signature=signature,
                                     loop_error_signatures=current.loop_error_signatures + [signature])
                    _, _, loop_signals = self._loop_assessment(s)
                    diagnostic_feedback = (s.phase in {Phase.DIAGNOSE, Phase.PATCH}
                        and (isinstance(e, (ModelOutputError, BudgetExceeded))
                             or s.execution_mode == 'batch' and isinstance(e, ValueError)))
                    if (loop_signals and not diagnostic_feedback
                            and not (isinstance(e, (ModelError, MCPConnectionError, MCPActionUnknown))
                            and e.status in {'UNKNOWN_OPERATION', 'WAITING_NETWORK'})):
                        s = self._mark_loop(s, self._loop_state_fingerprint(s), [], loop_signals)
                        return self.output(s, 'finalize')
                    error_details = None
                    if isinstance(e, (ModelError, MCPConnectionError, MCPActionUnknown)):
                        error_details = redact({**e.details, 'status': e.status,
                            'category': e.category, 'type': type(e).__name__,
                            'source': 'model' if isinstance(e, ModelError) else 'browser'})
                        if e.status in {'UNKNOWN_OPERATION', 'WAITING_NETWORK'}:
                            if s.execution_mode == 'batch':
                                s = self.finish_error(self.store.load(s.run_id, s.scope_id), e,
                                    details=error_details)
                                self.event(s, 'run.error', {'error': s.error,
                                    'status': s.run_status, 'error_details': s.error_details})
                                return self.output(s, 'finalize')
                            safe_unsent_model_retry = (
                                isinstance(e, ModelError) and e.status == 'WAITING_NETWORK'
                                and e.details.get('request_status') == 'not_sent'
                                and not e.details.get('requires_manual_review', False)
                                and not e.details.get('requires_new_run', False))
                            error_details['requires_manual_review'] = not safe_unsent_model_retry
                            s = self.changed(self.store.load(s.run_id, s.scope_id),
                                run_status=RunStatus.PAUSED,
                                error=sanitize(f'{label(e.status)}（{type(e).__name__}）：{error_message(e)}')[:1500],
                                error_details=error_details)
                            self.event(s, 'run.error', {'error': s.error,
                                'status': s.run_status, 'error_details': error_details})
                            return self.output(s, 'paused')
                        if s.execution_mode == 'batch' and not isinstance(e, ModelOutputError):
                            s = self.finish_error(self.store.load(s.run_id, s.scope_id), e,
                                details=error_details)
                            self.event(s, 'run.error', {'error': s.error,
                                'status': s.run_status, 'error_details': s.error_details})
                            return self.output(s, 'finalize')
                    if isinstance(e, ReplayUnbound):
                        if s.patch_hash:
                            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.INCONCLUSIVE,
                                             replay_index=0, pending_action=None,
                                             error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                             error_details={'terminal_reason': 'frozen_replay_unbound'})
                            self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details})
                            return self.output(s, 'finalize')
                        target = Phase.EXPLORE if s.phase == Phase.REPRODUCE else Phase.DIAGNOSE
                        s = self.changed(s, phase=target, replay_plan_ref=None,
                                         exploration_plan_ref=None, reproduction_plan_frozen=False,
                                         replay_index=0, pending_action=None,
                                         error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500])
                        self.event(s, 'run.error', {'error': s.error, 'error_details': error_details,
                                                    'action': '重新录制重放计划'})
                        return self.output(s, 'prelude')
                    if (isinstance(e, (ModelOutputError, BudgetExceeded))
                            or s.execution_mode == 'batch' and isinstance(e, ValueError)):
                        current = self.store.load(s.run_id, s.scope_id)
                        retryable_phase = current.phase in {Phase.DIAGNOSE, Phase.PATCH}
                        retry_count = current.diagnosis_retry_count + (1 if retryable_phase else 0)
                        feedback_ref = None
                        if retryable_phase:
                            feedback_ref, feedback = self._diagnosis_feedback(current, e)
                            feedback_text = ('输出未通过校验。请重新读取相关文件和失败证据，'
                                              '提出有实际差异的最小补丁；本次反馈：'
                                              + feedback['message'])
                            details = {'feedback': feedback_text,
                                       'retry_count': retry_count,
                                       'feedback_ref': feedback_ref,
                                       'additional_context': ['diagnosis_feedback_refs',
                                                              'current_workspace_diff',
                                                              'latest_failure_evidence']}
                            if s.execution_mode == 'batch' and retry_count >= self.DIAGNOSIS_RETRY_LIMIT:
                                s = self.changed(current, phase=Phase.FINALIZE,
                                    run_status=RunStatus.RUNNING,
                                    outcome=Outcome.REPAIR_EXHAUSTED,
                                    diagnosis_retry_count=retry_count,
                                    diagnosis_feedback_refs=current.diagnosis_feedback_refs + [feedback_ref],
                                    error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                    error_details={**details, 'terminal_reason': 'diagnosis_retry_limit'},
                                    pending_action=None)
                                self.event(s, 'run.error', {'error': s.error,
                                    'error_details': s.error_details,
                                    'action': '诊断重试次数耗尽，输出最终报告'})
                                return self.output(s, 'finalize')
                        else:
                            details = {'feedback': '输出未通过校验，请依据错误重新输出'}
                        delta = {'error': sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                 'error_details': details, 'pending_action': None}
                        if retryable_phase:
                            delta.update(diagnosis_retry_count=retry_count,
                                         diagnosis_feedback_refs=current.diagnosis_feedback_refs + [feedback_ref])
                            if current.phase == Phase.PATCH:
                                delta.update(phase=Phase.DIAGNOSE, patch_ref=None)
                        s = self.changed(current, **delta)
                        self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details,
                                                    'action': '反馈给模型并重试'})
                        return self.output(s, 'prelude')
                    if isinstance(e, PermissionError):
                        s = self.changed(s, error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                         error_details={'feedback': '该动作被策略拒绝，请根据原因重新决策',
                                                        **(e.details if isinstance(e, GuidanceRejected) else {})},
                                         pending_action=None,
                                         **({'phase': Phase.DIAGNOSE, 'patch_ref': None}
                                            if isinstance(e, GuidanceRejected) and s.phase == Phase.PATCH else {}))
                        self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details,
                                                    'action': '反馈给模型'})
                        return self.output(s, 'prelude')
                    current = self.store.load(s.run_id, s.scope_id)
                    if s.execution_mode == 'batch':
                        s = self.finish_error(current, e)
                        self.event(s, 'run.error', {'error': s.error,
                            'status': s.run_status, 'error_details': s.error_details})
                        return self.output(s, 'finalize')
                    s = self.changed(current, run_status=RunStatus.PAUSED,
                                     error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                     error_details=error_details or {'requires_manual_review': True},
                                     pending_action=None)
                    self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details})
                    return self.output(s, 'paused')
            graph.add_node(name, wrapped)
            graph.add_conditional_edges(name, lambda data: data['next_node'], {n: n for n in names} | {'end': END})
        graph.add_edge(START, 'prelude')
        return graph.compile(checkpointer=saver)

    def output(self, s, next_node):
        return {'data': s.model_dump(mode='json'), 'next_node': next_node}

    def finish_error(self, s, error, *, outcome=Outcome.INFRA_FAILURE, details=None):
        """Return a FINALIZE state without replaying an uncertain operation."""
        s = self.store.load(s.run_id, s.scope_id)
        message = sanitize(f'{type(error).__name__}: {error_message(error)}')[:1500]
        details = redact(details or getattr(error, 'details', {}) or {})
        details.setdefault('requires_manual_review', True)
        details.setdefault('terminal_reason', 'batch_runtime_error')
        return self.changed(s, phase=Phase.FINALIZE, run_status=RunStatus.RUNNING,
                            outcome=outcome, error=message, error_details=details,
                            pending_action=None)

    def _diagnosis_feedback(self, s, error):
        """Persist rich feedback so the next patch proposal has new context."""
        detail = {'type': type(error).__name__, 'message': sanitize(error_message(error)),
                  'phase': str(s.phase), 'attempt': s.diagnosis_retry_count + 1,
                  'error_details': redact(getattr(error, 'details', {}) or {})}
        ref = self.put(s, detail, name='诊断重试反馈')
        return ref, detail

    def route(self, s):
        return {Phase.PREPARE: 'prepare', Phase.EXPLORE: 'decide', Phase.REPRODUCE: 'reproduce',
                Phase.DIAGNOSE: 'diagnose', Phase.PATCH: 'patch', Phase.VERIFY: 'verify',
                Phase.REVIEW: 'review', Phase.FINALIZE: 'finalize'}[s.phase]

    async def prelude(self, s, _):
        self.scopes.assert_current(self.context)
        if self.control == 'cancel':
            self.cancel_subagents(s)
            self.control = None
            s = self.changed(s, phase=Phase.FINALIZE, error='用户已取消', outcome=Outcome.INCONCLUSIVE)
            return self.output(s, 'finalize')
        s = self.ensure_rule_snapshot(s)
        retarget = self.sync_guidance(s)
        if retarget:
            # 已确认的 L3 在阶段边界派生子 Run；父 Run 先收尾，再移交继承证据。
            self.retarget_child = retarget_state(s, retarget)
            retarget.status, retarget.child_run_id = 'superseded', self.retarget_child.run_id
            self.guidance_ledger.save(retarget)
            s = self.changed(s, phase=Phase.FINALIZE, superseded_by_run_id=self.retarget_child.run_id,
                             outcome=Outcome.INCONCLUSIVE, pending_action=None)
            self.event(s, 'run.retargeted', {'guidance_id': retarget.id, 'child_run_id': retarget.child_run_id,
                       'goal': self.retarget_child.goal, 'inherited_evidence_refs': s.evidence_refs})
            return self.output(s, 'finalize')
        if self.control == 'pause':
            self.cancel_subagents(s)
            self.control = None
            s = self.changed(s, run_status=RunStatus.PAUSED)
            return self.output(s, 'paused')
        if s.phase != Phase.FINALIZE and (s.phase != Phase.PREPARE or s.last_error_signature):
            s, looped = self._record_loop_boundary(s)
            if looped:
                return self.output(s, 'finalize')
        if self.notes:
            for note in self.notes:
                self.submit_guidance(s, note, level='hint')
            self.notes.clear()
            self.sync_guidance(s)
        if getattr(self, 'compact_requested', False):
            self.compact_requested = False
            self.compact_context(s, reason='manual')
        return self.output(s, self.route(s))

    def cancel_subagents(self, s):
        # 父 Run 暂停或取消时同步终止子任务，避免后台任务继续消耗资源。
        runtime = getattr(self, 'subagent_runtime', None)
        if runtime:
            runtime.cancel(s.run_id)

    def compact_context(self, s, *, reason='manual'):
        # 手动 compact 生成确定性摘要，不修改冻结的 TestSpec、权限或原始证据。
        # 摘要保留近期动作和失败事件，完整历史仍可从事件与 artifact 回放。
        from tracefix.knowledge.assembler import TokenCounter, compact_steps
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        steps = [{'step': index + 1, 'action': action} for index, action in enumerate(plan)]
        summary = compact_steps(steps)
        failure_events = [event for event in self.store.trace(s.run_id, s.scope_id)
            if event['type'] in {'tool.error', 'tool.rejected', 'model.error.persisted', 'run.error'}
            or event['type'] == 'gate.decided' and event.get('payload', {}).get('passed') is False]
        summary['failure_events'] = failure_events
        summary['phase'] = str(s.phase)
        summary['source_manifest'] = s.source_manifest
        ref = self.put(s, summary, name='上下文压缩摘要')
        if hasattr(s, 'compaction_refs'):
            s.compaction_refs.append(ref)
        if hasattr(s, 'working_memory_ref'):
            s.working_memory_ref = ref
        self.store.save(s)
        if getattr(self, 'memory', None):
            # 同步写入 L1 progress，使下次模型调用能读取压缩摘要。
            self.memory.note(s.scope_id, s.run_id,
                {'id': 'runtime_compaction', 'kind': 'progress',
                 'text': json.dumps(summary, ensure_ascii=False)[:4000], 'evidence_refs': []},
                source_manifest=s.source_manifest)
        counter = TokenCounter()
        self.event(s, 'context.compacted', {'reason': reason, 'summary_ref': ref,
            'step_range': [1, summary['merged_steps']], 'tokens_before': counter.count(steps),
            'tokens_after': counter.count(summary)})
        return ref

    def compact(self, s, *, reason='manual'):
        return self.compact_context(s, reason=reason)

    def agents(self, s):
        # 合并已持久化事件和运行中的层级 trace，供 CLI 查询同一份子任务视图。
        records = {}
        for event in self.store.trace(s.run_id, s.scope_id):
            if not event['type'].startswith('subtask.'):
                continue
            payload = event.get('payload') or {}
            key = payload.get('child_run_id') or payload.get('task_id') or payload.get('role')
            if key:
                records[key] = {**records.get(key, {}), **payload, 'event': event['type']}
        runtime = getattr(self, 'subagent_runtime', None)
        if runtime:
            for record in runtime.trace.children(s.run_id):
                key = record.get('child_run_id') or record.get('task_id')
                if key:
                    records[key] = {**records.get(key, {}), **record}
        return list(records.values())

    async def discover(self, s):
        if os.getenv('TRACEFIX_AGENT_MODE', '').lower() == 'single':
            self.event(s, 'subtask.discover.disabled', {'agent_mode': 'single'})
            return
        from tracefix.workers import HierarchyTrace, IsolatedGuiScout, SubAgentRuntime, WorkerTask
        # GUI scout 仅由父 Run 派发；已完成记录和深度检查阻止重复或递归探索。
        if os.getenv('TRACEFIX_BROWSER_WORKERS', '0') != '1' or getattr(self, 'subagent_depth', 0):
            return
        if any(event['type'] == 'subtask.discover.finished' for event in self.store.trace(s.run_id, s.scope_id)):
            return
        step_id = f'{s.run_id}:{s.revision}'
        runtime = SubAgentRuntime(profile=self.profile, workspace=self.workspace,
            browser_concurrency=max(1, int(os.getenv('TRACEFIX_BROWSER_WORKER_CONCURRENCY', '1'))),
            trace=HierarchyTrace(lambda record: self.event(s, record['type'],
                {key: value for key, value in record.items() if key != 'type'})))
        self.subagent_runtime = runtime
        task = WorkerTask(run_id=s.run_id, phase='DISCOVER', role='gui-scout',
            goal='在独立应用和浏览器中探索冻结测试目标并返回真实页面与失败证据。',
            prompt='依据冻结 TestSpec 从授权 URL 开始探索。只执行已授权浏览器动作，记录观察、页面地图及规则命中；成功必须由确定性断言判定。',
            tools=['browser'], source_revision=s.revision,
            metadata={'source_manifest': s.source_manifest, 'parent_step_id': step_id, 'url': s.url})
        s.budget = s.budget.charge('subtasks')
        self.store.save(s)
        try:
            result = await runtime.dispatch(task, parent_run_id=s.run_id, parent_step_id=step_id,
                                            generation=s.revision, runner=IsolatedGuiScout(self, s))
            usage = result.data.get('usage', {})
            totals = s.budget.model_dump()
            for metric in ('model_calls', 'browser_actions', 'tokens', 'cost_usd'):
                totals[metric] += usage.get(metric, 0)
            s.budget = type(s.budget)(**totals)
            ref = self.put(s, result.model_dump(mode='json'), name='独立GUI探索结果')
            s.subtask_refs.append(ref)
            self.store.save(s)
            self.event(s, 'subtask.discover.finished', {'artifact_ref': ref, **result.data})
            if self.memory and getattr(s, 'job_id', None):
                self.memory.save_job_memory(s.scope_id, s.job_id, result.data.get('page_map', {}),
                    source_run_id=s.run_id, source_manifest=s.source_manifest)
        except asyncio.CancelledError:
            runtime.cancel(s.run_id)
            raise
        except Exception as error:
            self.event(s, 'subtask.discover.finished', {'status': 'failed', 'error': sanitize(str(error))})
        finally:
            runtime.close()

    async def prepare(self, s, _):
        self.warn_retrieval_degraded(s)
        if s.step == 0:
            self.scopes.assert_current(self.context)
            if s.scope_id != self.context.active_scope:
                raise PermissionError('运行状态与当前 scope 不一致')
            snapshot = self.workspace.require_repository_snapshot(self.source)
            if digest(snapshot) != s.source_manifest:
                raise PermissionError('运行状态与源码快照摘要不一致')
            if not s.repo_snapshot_ref or self.get(s, s.repo_snapshot_ref) != snapshot:
                raise PermissionError('运行状态与绑定源码快照不一致')
            self.workspace.validate_repository(self.scopes, self.context, snapshot,
                base_commit=s.patch_base_commit, patch_hash=s.patch_hash,
                branch=s.local_branch or 'tracefix/' + s.run_id)
            async def prepare_env():
                if s.continuation_count:
                    await self.runner.close()
                environment = await self.runner.inspect_images()
                health = await self.runner.start(s.source_manifest)
                if not health['passed']:
                    raise RuntimeError('初始沙箱未通过健康检查')
                version = await self.runner.version()
                if version != s.source_manifest:
                    raise PermissionError('已部署的源码与清单不匹配')
                return {'environment_digest': environment, 'source_aligned': True}
            r = await self.operation(s, 'sandbox.start', {'source': s.source_manifest}, prepare_env)
            await self.browser.open()
            s = self.changed(s, step=1, **r)
            return self.output(s, 'prelude')
        if s.step == 1:
            async def baseline():
                return await self.runner.command('unit')
            result = await self.operation(s, 'baseline.unit', {'source': s.source_manifest}, baseline)
            ref = self.put(s, {**result, 'kind': 'baseline_unit', 'source_manifest': s.source_manifest}, name='基线单元测试')
            s = self.changed(s, step=2, baseline_validation_refs=[ref])
            return self.output(s, 'prelude')
        if not s.observation_ref:
            action = BrowserAction(kind='navigate', value=s.url)
            if s.test_spec_ref:
                ref = await self.act(s, action)
            else:
                self.scopes.assert_current(self.context)
                self.browser.policy.initial_navigation(s, action)
                s.budget = s.budget.charge('browser_actions')
                self.store.save(s)
                async def observe_initial_page():
                    return {'observation_ref': await self.capture(s, await self.browser.action(action))}
                receipt = await self.operation(s, 'browser.initial_navigation', action.model_dump(), observe_initial_page)
                ref = receipt['observation_ref']
            return self.output(self.changed(s, observation_ref=ref, step=3), 'prelude')
        if not s.test_spec_ref:
            observation = self.get(s, s.observation_ref)
            knowledge = await select_documents(self, s, observation)
            rules = self.active_rules(s, str(Phase.PREPARE))
            spec = await self.model_call(s, TestSpec, {'goal': s.goal, 'url': s.url,
                'observation': observation,
                'reference_documents': knowledge,
                'detection_rules': [rule.summary() for rule in rules],
                'instruction': '根据用户目标和首次只读观测编译 TestSpec。授权动作必须使用 schema 枚举；'
                               '定位器使用页面中的完整可访问名称；保留用户要求的刷新后断言，并提供独立回归场景。'},
                validate_output=lambda candidate: validate_spec_observation(candidate, observation))
            validate_spec_observation(spec, observation)
            ref = self.put(s, spec.model_dump(), name='测试规范')
            s = self.changed(s, test_spec_ref=ref, test_spec_hash=digest(spec))
            return self.output(s, 'prelude')
        phase = Phase.VERIFY if s.continuation_count and s.patch_hash and s.reproduced and s.replay_plan_ref else Phase.EXPLORE
        if phase == Phase.EXPLORE:
            await self.discover(s)
        s = self.changed(s, phase=phase, step=0)
        return self.output(s, 'prelude')

    def record_gui_issues(self, s, decision, observation):
        for issue in decision.issues:
            references = normalize_decision_evidence_refs(
                issue.evidence_refs, [], s.observation_ref, observation)
            if not references:
                continue
            location = {'url': observation.get('url') or s.url}
            finding = Finding(job_id=s.job_id or s.run_id, run_id=s.run_id, source='guided', severity='minor',
                title=issue.title, location=location, evidence_refs=references,
                fingerprint=digest([s.run_id, issue.title, issue.expected, issue.actual, location]),
                details={'expected': issue.expected, 'actual': issue.actual})
            if self.rule_library:
                finding = self.rule_library.save_finding(finding)
            self.event(s, 'finding.created', {'finding_id': finding.id, 'source': finding.source,
                'status': finding.status, 'finding': finding.model_dump(mode='json'),
                'expectation': issue.expected})

    async def decide(self, s, _):
        spec = self.spec(s)
        obs = self.get(s, s.observation_ref)
        image = (self.artifacts.read(s.scope_id, s.run_id, obs['screenshot_ref'])
                 if getattr(self.model, 'vision_model', None) else None)
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        context = build_context(s, spec.model_dump(), obs, pairs=[{'action': a, 'result': 'see current observation'} for a in plan[-4:]],
                                rules=self.active_rules(s, str(Phase.EXPLORE)))
        context['reference_documents'] = await select_documents(self, s, obs)
        context['instruction'] = '每次只请求一个浏览器动作，并等待最新观测。必须匹配当前 observation_id 和 element_ref。完成请求的交互后使用 finish；它只是请求运行时执行确定性断言检查。'
        decision = await self.model_call(s, Decision, context, image=image)
        self.validate_rule_refs(s, decision.rule_refs)
        obs = self.get(s, s.observation_ref)
        decision.evidence_refs = normalize_decision_evidence_refs(
            decision.evidence_refs, s.evidence_refs, s.observation_ref, obs)
        self.browser.policy.browser(s, decision.action, spec, obs)
        self.record_gui_issues(s, decision, obs)
        canonical = decision.action.model_copy(update={'observation_id': None, 'element_ref': None})
        fingerprint = digest([canonical.model_dump(), stable_snapshot(obs['snapshot'])])
        s = self.changed(s, pending_action=decision.action.model_dump(),
                         action_fingerprints=(s.action_fingerprints+[fingerprint])[-6:])
        return self.output(s, 'execute')

    async def execute(self, s, _):
        action = BrowserAction(**s.pending_action)
        ref = await self.act(s, action)
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        if action.kind != 'finish':
            canonical = action.model_copy(update={'observation_id': None, 'element_ref': None}).model_dump()
            plan.append(canonical)
        plan_ref = self.put(s, plan, name='操作重放计划')
        s = self.changed(s, observation_ref=ref, replay_plan_ref=plan_ref, step=s.step+1)
        return self.output(s, 'explore_gate')

    def check(self, s, checks, *, kind='original', scenario=None, scenario_step=None):
        obs = self.get(s, s.observation_ref)
        result = assertions(obs['snapshot'], checks)
        binding = self.verification_context(s)
        result.update(binding, type='validation_result', kind=kind,
                      observation_ref=s.observation_ref, observation_hash=digest(obs),
                      execution_plan_hash=binding['plan_hash'] if kind == 'original' else digest(
                          [action.model_dump(mode='json') for action in self.spec(s).regression_plan]))
        if scenario is not None:
            result.update(type='assertion_checkpoint', kind='behavior', scenario_id=scenario.id,
                          scenario_hash=digest(scenario), scenario_step=scenario_step,
                          action_hash=digest(scenario.steps[scenario_step - 1].action),
                          execution_plan_hash=digest([step.action.model_dump(mode='json') for step in scenario.steps]))
        ref = self.put(s, result, name='断言检查结果')
        self.event(s, 'gate.decided', {'passed': result['passed'], 'evidence_ref': ref})
        return result, ref

    async def explore_gate(self, s, _):
        done = s.pending_action['kind'] == 'finish'
        if not done:
            return self.output(self.changed(s, pending_action=None), 'prelude')
        result, ref = self.check(s, self.spec(s).assertions)
        if result['passed']:
            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.NO_BUG_FOUND,
                evidence_refs=s.evidence_refs+[ref], pending_action=None)
        else:
            s = self.changed(s, phase=Phase.REPRODUCE, replay_index=0, trial=0,
                evidence_refs=s.evidence_refs+[ref], failure_signatures=[digest(result['assertions'])], pending_action=None)
        return self.output(s, 'prelude')

    async def reset(self, s):
        async def reset_env():
            if s.reproduction_binding_ref:
                binding = self.get(s, s.reproduction_binding_ref)
                expected = {'source_manifest': s.source_manifest,
                            'environment_digest': s.environment_digest,
                            'test_spec_hash': s.test_spec_hash,
                            'scope_id': s.scope_id, 'url': s.url}
                if any(binding.get(key) != value for key, value in expected.items()):
                    raise ValueError('冻结复现绑定与当前环境、源码或 TestSpec 不一致')
            await self.browser.close()
            r = await self.runner.command('reset')
            if not r['passed']:
                raise RuntimeError('重置失败')
            await self.browser.open()
            raw = await self.browser.action(BrowserAction(kind='navigate', value=s.url))
            ref = await self.capture(s, raw)
            checks = self.spec(s).executable_preconditions
            if checks:
                result = assertions(self.get(s, ref).get('snapshot', ''), checks)
                if not result['passed']:
                    raise ValueError('复现前置条件不满足：' + json.dumps(result['assertions'], ensure_ascii=False))
            return {'observation_ref': ref}
        s.budget = s.budget.charge('browser_actions')
        self.store.save(s)
        return await self.operation(s, 'scenario.reset', {'trial': s.trial, 'validation': s.validation_index}, reset_env)

    async def reproduce(self, s, _):
        if not s.reproduction_plan_frozen:
            s = await self.freeze_reproduction_plan(s)
        plan = [BrowserAction(**a) for a in self.get(s, s.replay_plan_ref)]
        if s.replay_index == 0:
            r = await self.reset(s)
            s = self.changed(s, observation_ref=r['observation_ref'], replay_index=1)
        elif s.replay_index <= len(plan):
            ref = await self.act(s, plan[s.replay_index-1], frozen=True)
            s = self.changed(s, observation_ref=ref, replay_index=s.replay_index+1)
        else:
            r, ref = self.check(s, self.spec(s).assertions)
            signatures = s.failure_signatures + [digest(r['assertions']) if not r['passed'] else 'PASS']
            trials = s.trial + 1
            s = self.changed(s, trial=trials, replay_index=0, evidence_refs=s.evidence_refs+[ref], failure_signatures=signatures)
            if trials == 3:
                failures = Counter(value for value in signatures[-3:] if value != 'PASS')
                stable = max(failures.values(), default=0) >= 2
                if stable and s.mode == 'repair':
                    s = self.changed(s, phase=Phase.DIAGNOSE, reproduced=True)
                else:
                    s = self.changed(s, phase=Phase.FINALIZE, reproduced=stable,
                                     outcome=Outcome.BUG_CONFIRMED if stable else Outcome.INCONCLUSIVE,
                                     error='已验证缺陷报告（仅测试模式）' if stable else '故障无法在三次试验中的至少两次复现')
        return self.output(s, 'prelude')

    def phase_observations(self, s, phase):
        started = {}
        observations = []
        for event in self.store.trace(s.run_id, s.scope_id):
            if event['phase'] != str(phase):
                continue
            payload = event['payload']
            if event['type'] == 'tool.started':
                operation_id = payload.get('operation_id')
                if operation_id:
                    started[operation_id] = payload.get('intent', {})
            elif event['type'] == 'tool.completed':
                ref = payload.get('receipt', {}).get('observation_ref')
                if ref:
                    operation_id = payload.get('operation_id')
                    observations.append({'action': started.get(operation_id, {}),
                                         'observation_ref': ref, 'observation': self.get(s, ref)})
        return observations

    async def freeze_reproduction_plan(self, s):
        if s.patch_hash:
            raise ValueError('修补后不能重新选择复现计划')
        if s.reproduction_plan_frozen:
            return s
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        if not plan:
            binding = self.put(s, {'source_manifest': s.source_manifest,
                'environment_digest': s.environment_digest, 'test_spec_hash': s.test_spec_hash,
                'scope_id': s.scope_id, 'url': s.url,
                'preconditions': [item.model_dump(mode='json') for item in self.spec(s).executable_preconditions]},
                name='复现绑定')
            return self.changed(s, exploration_plan_ref=s.replay_plan_ref,
                                reproduction_binding_ref=binding, reproduction_plan_frozen=True)
        selection = await self.model_call(s, ReproductionPlan, {
            'test_spec': self.spec(s).model_dump(), 'goal': s.goal,
            'exploration_actions': plan,
            'exploration_observations': self.phase_observations(s, Phase.EXPLORE),
            'instruction': '只选择已有动作的递增索引，保留交互及随后刷新，排除无关重试。'})
        indices = selection.action_indices
        if indices != sorted(set(indices)) or any(index < 0 or index >= len(plan) for index in indices):
            raise ModelOutputError('复现计划索引必须唯一、递增且在探索记录范围内')
        selected = []
        for index in indices:
            action = BrowserAction(**plan[index])
            selected.append(action.model_copy(update={
                'observation_id': None, 'element_ref': None, 'page_generation': None}).model_dump(mode='json'))
        interactions = [index for index, action in enumerate(selected) if action['kind'] in {'click', 'type', 'select', 'press'}]
        original_interactions = [index for index, action in enumerate(plan) if action['kind'] in {'click', 'type', 'select', 'press'}]
        needs_reload = bool(original_interactions) and any(
            action['kind'] == 'navigate' for action in plan[original_interactions[0] + 1:])
        if (original_interactions and not interactions) or (needs_reload and not any(
                action['kind'] == 'navigate' for action in selected[interactions[-1] + 1:])):
            raise ModelOutputError('复现计划必须包含交互及随后刷新验证')
        ref = self.put(s, selected, name='冻结复现计划')
        binding = self.put(s, {'source_manifest': s.source_manifest,
            'environment_digest': s.environment_digest, 'test_spec_hash': s.test_spec_hash,
            'scope_id': s.scope_id, 'url': s.url,
            'preconditions': [item.model_dump(mode='json') for item in self.spec(s).executable_preconditions]},
            name='复现绑定')
        return self.changed(s, exploration_plan_ref=s.replay_plan_ref, replay_plan_ref=ref,
                            reproduction_binding_ref=binding, reproduction_plan_frozen=True)

    async def diagnose(self, s, _):
        self.warn_retrieval_degraded(s)
        self.workspace.check_frozen(self.source)
        overlay = s.source_manifest + (':' + s.patch_hash if s.patch_hash else '')
        await self.retriever.index(self.workspace, overlay)
        readers = {
            'artifact_exists': lambda ref: self.artifacts.exists(s.scope_id, s.run_id, ref),
            'artifact_read': lambda ref: self.get(s, ref),
        }
        code = await self.retriever.retrieve(s.goal, overlay, 'M1', 10,
            state=s, phase=s.phase)
        recipes = await self.retriever.retrieve(s.goal, s.source_manifest, 'M3', 3,
            state=s, phase=s.phase, allow_compatible=False, **readers)
        observation = self.get(s, s.observation_ref) if s.observation_ref else None
        action_observations = self.phase_observations(s, Phase.VERIFY if s.patch_hash else Phase.REPRODUCE)[-4:]
        if not action_observations:
            action_observations = self.phase_observations(s, Phase.EXPLORE)[-4:]
        query_observation = {**(observation or {}), 'network': '\n'.join(
            [str((observation or {}).get('network', ''))] +
            [str(item['observation'].get('network', '')) for item in action_observations])}
        query = symptom_query(s.goal, self.spec(s).assertions, query_observation)
        # 直接读取当前授权文件以核验检索卡片，并提供补丁校验所需的 before_hash。
        preferred = [json.loads(card['content']).get('path') for card in code if card.get('content', '').startswith('{')]
        card_limit = 60_000 * (s.diagnosis_retry_count + 1)
        broad_cards = self.workspace.cards(limit_chars=card_limit, preferred_paths=preferred)
        if hasattr(self.workspace, 'fragments'):
            cards = self.workspace.fragments(query, preferred_paths=preferred, limit_chars=24_000)
            if not cards:
                cards = broad_cards
        else:
            cards = broad_cards
        if self.rule_library and self.rule_resolver:
            files = []
            for path in self.workspace.files():
                try:
                    files.append((path, self.workspace.read(path)))
                except (OSError, PermissionError, ValueError):
                    continue
            for rule in self.active_rules(s, str(Phase.DIAGNOSE)):
                for finding in evaluate_static(rule, files, job_id=s.run_id, run_id=s.run_id):
                    saved = self.rule_library.save_finding(finding)
                    self.event(s, 'finding.created', {'finding_id': saved.id, 'rule_id': saved.rule_id,
                                                     'rule_version': saved.rule_version, 'source': 'static',
                                                     'finding': saved.model_dump(mode='json'),
                                                     'expectation': rule.name})
        observation = self.get(s, s.observation_ref) if s.observation_ref else None
        if s.observation_ref and s.observation_ref not in s.evidence_refs:
            s = self.changed(s, evidence_refs=s.evidence_refs + [s.observation_ref])
        for item in action_observations:
            if item['observation_ref'] not in s.evidence_refs:
                s.evidence_refs.append(item['observation_ref'])
        for card in cards:
            card_ref = self.put(s, card, name='诊断源码片段')
            card['artifact_ref'] = card_ref
            if card_ref not in s.evidence_refs:
                s.evidence_refs.append(card_ref)
        context = build_context(s, self.spec(s).model_dump(), observation=observation, cards=cards,
                                rules=self.active_rules(s, str(Phase.DIAGNOSE)))
        context['reference_documents'] = await select_documents(self, s)
        validations = []
        validation_observations = []
        for validation_ref in s.validation_refs:
            validation = self.get(s, validation_ref)
            if validation.get('patch_hash') != s.patch_hash:
                continue
            result = self.get(s, validation['artifact_ref'])
            validations.append({**validation, 'result': result})
            observation_ref = result.get('observation_ref')
            if observation_ref:
                validation_observations.append({'observation_ref': observation_ref,
                                               'observation': self.get(s, observation_ref)})
                if observation_ref not in s.evidence_refs:
                    s.evidence_refs.append(observation_ref)
        context['available_evidence_refs'] = list(dict.fromkeys(context['available_evidence_refs'] + s.evidence_refs))
        context.update(instruction='请诊断并提出最小局部补丁。优先 Read 取得当前 overlay_hash/revision，使用唯一 exact anchor 的 Edit(edits) 生成候选；检查真实 diff 后提交 staged_refs。运行时物化完整内容，模型无需重写未变全文。兼容旧 whole edits。只能编辑当前允许的文件并引用已有证据，失败按 error_code 重读/修正。',
                       allowed_files=self.workspace.allowed_files, repair_memory=recipes, retrieval_ids=[x['id'] for x in code],
                       failures=[self.get(s, r) for r in s.evidence_refs[-4:]],
                       previous_validation=validations, validation_observations=validation_observations,
                       replay_plan=self.get(s, s.replay_plan_ref) if s.replay_plan_ref else [],
                       current_workspace_diff=self.workspace.diff(),
                       diagnosis_retry_count=s.diagnosis_retry_count,
                       diagnosis_feedback=self.current_diagnosis_feedback(s),
                       staged_candidates=list(s.staged_candidate_refs))
        diagnosis_report = None
        action_id, window_start = None, None
        for event in reversed(self.store.trace(s.run_id, s.scope_id)):
            if event['type'] == 'tool.started' and event.get('payload', {}).get('intent', {}).get('kind') in {'click', 'type', 'select', 'press'}:
                action_id, window_start = event['payload'].get('operation_id'), event.get('at')
                break
        binding = binding_from_observation(s, observation or {}, action_id=action_id,
            window_start=window_start, window_end=(observation or {}).get('observed_at'),
            dom_assertions=assertions((observation or {}).get('snapshot', ''), self.spec(s).assertions)['assertions'])
        channel_refs = list((observation or {}).get('raw_refs', {}).values())
        channel_refs.extend(ref for item in action_observations
                            for ref in item['observation'].get('raw_refs', {}).values())
        diagnosis_refs = list(dict.fromkeys(context['available_evidence_refs'] + [
            ref for ref in channel_refs if self.artifacts.exists(s.scope_id, s.run_id, ref)]))
        diagnosis_context = {'policy': context['policy'], 'goal': s.goal,
                'scope': s.scope_id, 'test_spec': self.spec(s).model_dump(mode='json'),
                'observation': observation,
                'symptom_binding': binding.model_dump(mode='json'),
                'source_fragments': cards,
                'cards': cards,
                'workspace_root': str(self.workspace.root),
                'action_observations': [{
                    'action': item['action'], 'observation_ref': item['observation_ref'],
                    'observed_at': item['observation'].get('observed_at'),
                    'url': item['observation'].get('url'),
                    'raw_refs': item['observation'].get('raw_refs', {}),
                    'assertions': assertions(item['observation'].get('snapshot', ''), self.spec(s).assertions)['assertions'],
                    'console': self._bounded_text(item['observation'].get('console', ''), 6000)[0],
                    'network': self._bounded_text(item['observation'].get('network', ''), 6000)[0],
                    'collection': item['observation'].get('collection', {})} for item in action_observations],
                'evidence_refs': diagnosis_refs,
                'available_evidence_refs': diagnosis_refs,
                'validation_results': [{'kind': item['kind'], 'passed': item['passed'],
                    'assertions': item['result'].get('assertions', []),
                    'observation_ref': item['result'].get('observation_ref')} for item in validations],
                'worker_depth': 1, 'worker_same_model': True,
                'worker_readonly_investigation': True,
                'worker_allowed_files': list(dict.fromkeys(card['path'] for card in cards))[:15],
                'allowed_tools': ['Read', 'Grep', 'Glob'],
                'worker_allowed_tools': ['Read', 'Grep', 'Glob'],
                'worker_write_enabled': False, 'worker_shell_mode': 'disabled',
                'instruction': '输出可反驳的少量诊断假设；只能引用已有 evidence_refs 和当前源码片段。'
                               'Read 参数是 file_path（workspace_root 下绝对路径）、offset（起始行）、limit（行数），'
                               '可选 expected_content_version；按需读取命中邻域即可。'
                               '沿文案/API搜索命中追踪 handler、store/state、API、hydration；'
                               '无关404仅为线索，不能单独证明因果。source-map/initiator 不可用时标 unavailable，不推断无请求。'}
        input_ref = self.put(s, diagnosis_context, name='诊断输入')
        self.event(s, 'diagnosis.started', {'input_ref': input_ref, 'source_manifest': s.source_manifest,
                                         'observation_ref': s.observation_ref})
        model_support = getattr(self.model, 'supports_structured_diagnosis', False)
        if model_support:
            versions = {card['path']: card.get('content_version', digest(self.workspace.source_bytes(card['path'])[1]))
                        for card in cards}
            def validate_diagnosis(candidate):
                try:
                    candidate = DiagnosisReport(**candidate.model_dump(mode='json'),
                        binding=binding, source_version=s.source_manifest)
                    candidate_versions = dict(versions)
                    for hypothesis in candidate.hypotheses:
                        for path in hypothesis.candidate_paths:
                            self.workspace.path(path.path)
                            candidate_versions[path.path] = digest(self.workspace.source_bytes(path.path)[1])
                    validate_report(candidate, evidence_refs=s.evidence_refs + diagnosis_refs,
                        allowed_files=list(self.workspace.files()), source_manifest=s.source_manifest,
                        environment_digest=s.environment_digest, binding=binding, content_versions=candidate_versions)
                except (ValueError, PermissionError) as error:
                    raise ModelOutputError(str(error), category='diagnosis_validation') from error
            diagnosis_draft = await self.model_call(s, DiagnosisDraft, diagnosis_context,
                                                   validate_output=validate_diagnosis)
            diagnosis_report = DiagnosisReport(**diagnosis_draft.model_dump(mode='json'),
                binding=binding, source_version=s.source_manifest)
            validate_report(diagnosis_report, evidence_refs=s.evidence_refs + diagnosis_refs,
                allowed_files=list(self.workspace.files()), source_manifest=s.source_manifest,
                environment_digest=s.environment_digest, binding=binding,
                content_versions={path: digest(self.workspace.source_bytes(path)[1])
                                  for hypothesis in diagnosis_report.hypotheses
                                  for path in (candidate.path for candidate in hypothesis.candidate_paths)})
            diagnosis_ref = self.put(s, diagnosis_report.model_dump(mode='json'), name='结构化诊断')
            context['diagnosis_report_ref'] = diagnosis_ref
            context['diagnosis'] = diagnosis_report.model_dump(mode='json')
            context['hypothesis_refs'] = [item.id for item in diagnosis_report.hypotheses]
            s.hypothesis_refs.append(diagnosis_ref)
            self.store.save(s)
            self.event(s, 'diagnosis.completed', {'report_ref': diagnosis_ref, 'generated_by': 'model',
                'statuses': [item.status for item in diagnosis_report.hypotheses]})
            if not any(item.status == 'supported' for item in diagnosis_report.hypotheses):
                raise ModelOutputError('诊断尚无当前证据支持的根因；需执行区分性探针或补充来源',
                    category='diagnosis_unresolved', details={'diagnosis_ref': diagnosis_ref,
                        'unresolved': diagnosis_report.unresolved})
        else:
            diagnosis_report = DiagnosisReport(binding=binding, source_version=s.source_manifest,
                generated_by='deterministic_unavailable', unresolved=['当前模型未声明结构化诊断支持'])
            diagnosis_ref = self.put(s, diagnosis_report.model_dump(mode='json'), name='诊断不可用')
            context['diagnosis'] = diagnosis_report.model_dump(mode='json')
            self.event(s, 'diagnosis.unavailable', {'report_ref': diagnosis_ref,
                'generated_by': 'deterministic_unavailable'})
        if s.diagnosis_retry_count:
            context['instruction'] += (' 上一次未形成有效补丁。重新核对完整复现步骤、当前页面、失败断言与源码调用链；'
                                       '按未决预测定向搜索/重读片段；只包含实际有改动的文件，'
                                       '不要为了满足输出要求编造无意义改动。')
        worker_enabled = (os.getenv('TRACEFIX_WORKER', '1') != '0'
            and os.getenv('TRACEFIX_AGENT_MODE', '').lower() != 'single'
            and getattr(self, 'subagent_enabled', True)
            and not getattr(self, 'subagent_depth', 0))
        self.event(s, 'diagnosis.worker.policy', {'agent_mode': os.getenv('TRACEFIX_AGENT_MODE', 'multi'),
            'legacy_worker_switch': os.getenv('TRACEFIX_WORKER', '1'), 'enabled': worker_enabled})
        if worker_enabled:
            from tracefix.runtime.worker import ReadOnlyWorker, SubtaskSpec
            specs = [SubtaskSpec(s.goal, 'code-explorer', s.revision,
                tuple(card['path'] for card in cards[:3]), tuple(s.evidence_refs[-2:])),
                SubtaskSpec(s.goal, 'evidence-reviewer', s.revision, (),
                    tuple(dict.fromkeys([s.observation_ref] +
                                        [item['observation_ref'] for item in action_observations])))]
            results = await ReadOnlyWorker(self).group(s, specs)
            accepted = []
            for result in results:
                worker_ref = self.put(s, result.model_dump(), name='子任务调查结果')
                s.subtask_refs.append(worker_ref)
                if result.status in {'completed', 'partial'} and (not result.unresolved or result.evidence_refs or result.files):
                    accepted.append(result.model_dump())
            context['investigations'] = accepted
            context['read_only_investigations'] = accepted
            claims = {}
            for investigation in accepted:
                for hypothesis in investigation.get('hypotheses', []):
                    for candidate in hypothesis.get('candidate_paths', []):
                        claims.setdefault(candidate['path'], []).append(hypothesis['summary'])
            context['investigation_conflicts'] = [{'path': path, 'hypotheses': list(dict.fromkeys(summaries))}
                for path, summaries in claims.items() if len(set(summaries)) > 1]
            if context['investigation_conflicts']:
                context['instruction'] += ' 只读调查存在冲突；先依据原件消歧，调查结果不能覆盖当前观察或补丁。'
            self.store.save(s)
        patch = await self.model_call(s, PatchProposal, context)
        try:
            patch = materialize_patch_proposal(self, s, patch)
        except (ValueError, PermissionError) as error:
            raise ModelOutputError(str(error), category='output_validation',
                                   details=getattr(error, 'details', {})) from error
        self.validate_rule_refs(s, patch.rule_refs)
        if not set(patch.evidence_refs) <= set(s.evidence_refs):
            invalid = sorted(set(patch.evidence_refs) - set(s.evidence_refs))
            raise ModelOutputError(
                '补丁引用了不存在的证据：' + '、'.join(invalid),
                category='output_validation',
                details={'invalid_evidence_refs': invalid})
        self.validate_patch_candidate(patch, state=s)
        ref = self.put(s, patch.model_dump(), name='补丁方案')
        s = self.changed(s, phase=Phase.PATCH, patch_ref=ref, hypothesis_refs=s.hypothesis_refs+[ref],
                         diagnosis_retry_count=0, error=None, error_details=None,
                         last_error_signature=None)
        return self.output(s, 'prelude')

    def validate_patch_candidate(self, patch, *, state=None):
        paths = [edit.path for edit in patch.edits]
        if not paths:
            raise ModelOutputError('候选必须先物化非空 edits', category='output_validation')
        if len(paths) != len(set(paths)):
            raise ModelOutputError('补丁包含重复的编辑路径', category='output_validation')
        for edit in patch.edits:
            try:
                path = self.workspace.path(edit.path, write=True)
            except (PermissionError, ValueError, FileNotFoundError) as error:
                raise ModelOutputError('补丁路径未通过授权校验：' + edit.path,
                    category='output_validation',
                    details={'path': edit.path, 'reason': sanitize(error_message(error))}) from error
            current = path.read_bytes()
            if digest(current) != edit.before_hash:
                raise ModelOutputError(
                    '补丁基准哈希不匹配：' + edit.path,
                    category='output_validation',
                    details={'error_code': 'STALE_BASE', 'path': edit.path,
                             'expected_before_hash': edit.before_hash, 'written': False,
                             'actual_before_hash': digest(current),
                             'next_step': '重新读取当前源码并重建局部候选，不能只更新 before_hash。'})
            if not edit.content.strip() or current == edit.content.encode('utf-8'):
                raise ModelOutputError(
                    '补丁包含无实际改动的文件：' + edit.path,
                    category='output_validation',
                    details={'error_code': 'NO_CHANGE', 'written': False,
                             'path': edit.path, 'reason': 'no_effective_change',
                             'proposal_summary': patch.summary,
                             'proposed_paths': paths})
        try:
            self.workspace.validate_guidance_patch(patch)
        except PermissionError as error:
            raise ModelOutputError('补丁不符合当前编辑约束：' + sanitize(error_message(error)),
                category='output_validation', details=getattr(error, 'details', {})) from error
        try:
            self.workspace.validate_candidate_syntax(patch.edits)
        except ValueError as error:
            raise ModelOutputError(str(error), category='output_validation',
                                   details=getattr(error, 'details', {})) from error
        if state is not None:
            fingerprint = self.candidate_fingerprint(patch)
            for record in state.failed_candidate_signatures:
                if (record.get('fingerprint') == fingerprint
                        and record.get('source_manifest') == state.source_manifest
                        and record.get('environment_digest') == state.environment_digest
                        and record.get('test_spec_hash') == state.test_spec_hash):
                    raise ModelOutputError('公开验证已经否定相同候选；请依据反证修正实际内容。',
                        category='output_validation', details={'error_code': 'FAILED_CANDIDATE_REPEATED',
                            'written': False, 'feedback_ref': record['feedback_ref'],
                            'failed_patch_hash': record['patch_hash'],
                            'next_step': '读取绑定失败反馈，修改实际候选后重新提交。'})

    def candidate_fingerprint(self, patch=None):
        replacements = {edit.path: edit.content.encode('utf-8') for edit in patch.edits} if patch else {}
        changes = {}
        for path, baseline_hash in self.source.get('files', {}).items():
            current = replacements.get(path)
            if current is None:
                current = self.workspace.path(path).read_bytes()
            current_hash = digest(current)
            if current_hash != baseline_hash:
                changes[path] = current_hash
        return digest(changes)

    def current_diagnosis_feedback(self, state):
        records = []
        plan_hash = self.verification_context(state)['plan_hash']
        for ref in state.diagnosis_feedback_refs:
            raw = self.get(state, ref)
            if raw.get('type') == 'validation_feedback':
                feedback = ValidationFeedback.model_validate(raw)
                if not feedback_is_current(feedback, state, replay_plan_hash=plan_hash,
                        artifact_exists=lambda item: self.artifacts.exists(state.scope_id, state.run_id, item),
                        artifact_read=lambda item: self.get(state, item),
                        artifact_read_bytes=lambda item: self.artifacts.read(state.scope_id, state.run_id, item)):
                    continue
            records.append(raw)
        return records[-self.DIAGNOSIS_RETRY_LIMIT:]

    async def patch(self, s, _):
        self.scopes.assert_current(self.context)
        self.sync_guidance(s)
        spec = self.spec(s)
        if spec.behavior_scenarios and not (s.reproduction_plan_frozen and s.replay_plan_ref):
            raise ValueError('业务验证要求在补丁前冻结复现计划')
        if not all(self.bundle_exists(s,r) for r in s.evidence_refs):
            raise ValueError('所需的复现证据缺失或已损坏')
        proposal = PatchProposal(**self.get(s, s.patch_ref))
        if not s.patch_base_commit:
            s = self.changed(s, patch_base_commit=self.workspace.head())
        s.budget = s.budget.charge('patches')
        self.store.save(s)
        async def apply():
            return self.workspace.apply(proposal, base=s.patch_base_commit)
        r = await self.operation(s, 'workspace.patch',
                                 {**proposal.model_dump(), 'base_commit': s.patch_base_commit}, apply,
                                 reconcile=lambda: self.workspace.reconcile(proposal, base=s.patch_base_commit))
        diff = self.put(s, self.workspace.diff(s.patch_base_commit), 'diff', name='补丁差异')
        self.event(s, 'patch.applied', {'diff_ref': diff, 'patch_hash': r['patch_hash']})
        s = self.changed(s, phase=Phase.VERIFY, patch_hash=r['patch_hash'], validation_index=0, replay_index=0)
        return self.output(s, 'prelude')

    async def verify(self, s, _):
        self.workspace.check_frozen(self.source)
        if digest(self.workspace.diff(s.patch_base_commit or 'HEAD').encode()) != s.patch_hash:
            raise PermissionError('补丁在验证开始后发生了变化')
        spec = self.spec(s)
        kinds = ['static', 'unit', 'build', 'health', 'original', 'regression'] + ['behavior'] * len(spec.behavior_scenarios)
        kind = kinds[s.validation_index]
        scenario = spec.behavior_scenarios[s.validation_index - 6] if kind == 'behavior' else None
        if scenario is not None:
            if s.replay_index == 0:
                # 每个场景须自行建立前置状态，隔离探索/上一场景的浏览器与服务端状态，并清空旧检查点。
                result = await self.reset(s)
                return self.output(self.changed(s, observation_ref=result['observation_ref'],
                                               replay_index=1, behavior_check_refs=[]), 'prelude')
            if s.replay_index <= len(scenario.steps):
                step_index = s.replay_index
                step = scenario.steps[step_index - 1]
                ref = await self.act(s, step.action, frozen=True)
                s = self.changed(s, observation_ref=ref, replay_index=step_index + 1)
                result = None
                if step.assertions:
                    # 在下一动作覆盖页面状态前取证，才能区分每次转移与刷新后的持久化结果。
                    result, ref = self.check(s, step.assertions, scenario=scenario, scenario_step=step_index)
                    s = self.changed(s, behavior_check_refs=s.behavior_check_refs + [ref])
                if (result is None or result['passed']) and step_index < len(scenario.steps):
                    return self.output(s, 'prelude')
            checkpoints = [self.get(s, ref) for ref in s.behavior_check_refs]
            expected_steps = [index for index, step in enumerate(scenario.steps, 1) if step.assertions]
            last_checkpoint = checkpoints[-1] if checkpoints else {}
            # 检查点必须按冻结步骤完整覆盖；提前失败或只执行终态断言都不能汇总为通过。
            result = {'passed': ([item['scenario_step'] for item in checkpoints] == expected_steps
                                 and all(item['passed'] for item in checkpoints)),
                      'scenario_id': scenario.id, 'scenario_hash': digest(scenario),
                      'checkpoint_refs': list(s.behavior_check_refs),
                      'observation_ref': last_checkpoint.get('observation_ref'),
                      'observation_hash': last_checkpoint.get('observation_hash'),
                      'execution_plan_hash': digest([step.action.model_dump(mode='json') for step in scenario.steps])}
        elif kind in {'original', 'regression'}:
            plan = ([BrowserAction(**a) for a in self.get(s, s.replay_plan_ref)] if kind == 'original' else spec.regression_plan)
            if s.replay_index == 0:
                r = await self.reset(s)
                return self.output(self.changed(s, observation_ref=r['observation_ref'], replay_index=1), 'prelude')
            if s.replay_index <= len(plan):
                ref = await self.act(s, plan[s.replay_index-1], frozen=True)
                return self.output(self.changed(s, observation_ref=ref, replay_index=s.replay_index+1), 'prelude')
            result, _ = self.check(s, spec.assertions if kind == 'original' else spec.regression_assertions, kind=kind)
        else:
            async def run():
                if kind == 'health':
                    return await self.runner.health()
                if kind == 'build':
                    return await self.runner.rebuild()
                return await self.runner.command(kind)
            result = await self.operation(s, 'verify.'+kind, {'kind': kind, 'patch_hash': s.patch_hash}, run)
        self.workspace.check_frozen(self.source)
        validation_name = {'static': '静态检查', 'unit': '单元测试', 'build': '构建',
                           'health': '健康检查', 'original': '原问题重测', 'regression': '回归测试',
                           'behavior': '业务行为场景'}[kind]
        binding = self.verification_context(s)
        result = {**result, **binding, 'type': 'validation_result', 'kind': kind}
        artifact = self.put(s, result, name=validation_name+'结果')
        validation = Validation(kind=kind, passed=result['passed'], scope_id=s.scope_id, run_id=s.run_id,
            verifier_version=binding['verifier_version'], source_manifest=s.source_manifest,
            patch_hash=s.patch_hash, environment_digest=s.environment_digest,
            test_spec_hash=s.test_spec_hash, artifact_ref=artifact,
            scenario_id=scenario.id if scenario else None, scenario_hash=digest(scenario) if scenario else None,
            replay_plan_hash=binding['plan_hash'])
        ref = self.put(s, validation.model_dump(), name=validation_name+'验证记录')
        s = self.changed(s, validation_refs=s.validation_refs+[ref], replay_index=0)
        if not validation.passed:
            feedback = build_validation_feedback(s, validation,
                artifact_exists=lambda item: self.artifacts.exists(s.scope_id, s.run_id, item),
                artifact_read=lambda item: self.get(s, item),
                artifact_read_bytes=lambda item: self.artifacts.read(s.scope_id, s.run_id, item),
                replay_plan_hash=binding['plan_hash'], validation_ref=ref)
            feedback_ref = self.put(s, feedback.model_dump(mode='json'), name='公开开发验证反馈')
            failed = list(s.failed_candidate_signatures)
            if (feedback.status == 'failed' and feedback.failed_candidate
                    and feedback.failure_class in {'editing', 'counterevidence'}):
                failed.append({'fingerprint': self.candidate_fingerprint(),
                    'source_manifest': s.source_manifest, 'environment_digest': s.environment_digest,
                    'test_spec_hash': s.test_spec_hash, 'patch_hash': s.patch_hash,
                    'candidate_ref': s.patch_ref, 'feedback_ref': feedback_ref})
            s = self.changed(s, diagnosis_feedback_refs=s.diagnosis_feedback_refs + [feedback_ref],
                             failed_candidate_signatures=failed[-50:])
            self.event(s, 'gate.decided', {'validation': kind, 'passed': False})
            return self.output(self.changed(s, phase=Phase.DIAGNOSE), 'prelude')
        if s.validation_index < len(kinds)-1:
            return self.output(self.changed(s, validation_index=s.validation_index+1), 'prelude')
        if not await self.runtime_verification_passed(s):
            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.INFRA_FAILURE,
                             run_status=RunStatus.RUNNING,
                             error='最终运行环境采样失败、发生漂移或验证证据失效，拒绝输出已验证修复',
                             error_details={'terminal_reason': 'runtime_environment_gate_failed'})
            self.event(s, 'run.error', {'error': s.error, 'error_details': s.error_details,
                                        'action': '保留未验证补丁并清理运行环境'})
            return self.output(s, 'finalize')
        if s.execution_mode == 'batch':
            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.FIX_VERIFIED,
                             run_status=RunStatus.RUNNING, approval_ref=None,
                             error=None, error_details=None)
            return self.output(s, 'finalize')
        approval = self.store.approval(s, {'action': 'local_commit', 'branch': 'tracefix/'+s.run_id})
        s = self.changed(s, phase=Phase.REVIEW, outcome=Outcome.FIX_VERIFIED,
                         approval_ref=approval, run_status=RunStatus.WAITING_APPROVAL)
        return self.output(s, 'review')

    async def review(self, s, _):
        # This node re-enters after interrupt. Approval parser wrote a bound record;
        # model or webpage strings cannot supply authorization.
        interrupt({'approval_id': s.approval_ref, 'patch_hash': s.patch_hash,
                   'action': '创建本地候选提交；不得发布到远程或合并'})
        action = {'action': 'local_commit', 'branch': 'tracefix/'+s.run_id}
        self.workspace.check_frozen(self.source)
        if not s.patch_base_commit:
            s = self.changed(s, patch_base_commit=self.workspace.head())
        if digest(self.workspace.diff(s.patch_base_commit).encode()) != s.patch_hash:
            raise PermissionError('审批对应的补丁哈希已变化')
        persisted = self.store.load(s.run_id, s.scope_id)
        approval_fields = ('validation_refs', 'evidence_refs', 'patch_hash', 'patch_base_commit',
            'source_manifest', 'environment_digest', 'test_spec_ref', 'test_spec_hash',
            'replay_plan_ref', 'reproduction_plan_frozen', 'reproduced', 'source_aligned')
        if (any(getattr(persisted, field) != getattr(s, field) for field in approval_fields)
                or not await self.runtime_verification_passed(persisted)):
            s = self.changed(persisted, phase=Phase.FINALIZE, run_status=RunStatus.RUNNING,
                outcome=Outcome.INFRA_FAILURE, error='审批对应的验证证据已失效',
                error_details={'terminal_reason': 'approval_verification_invalid'})
            return self.output(s, 'finalize')
        approved = self.store.consume_approval(s.approval_ref, s, action)
        branch = None
        if approved:
            async def commit():
                return {'branch': self.workspace.branch(action['branch'])}
            receipt = await self.operation(s, 'local.commit', action, commit)
            branch = receipt['branch']
        s = self.changed(s, phase=Phase.FINALIZE, run_status=RunStatus.RUNNING, local_branch=branch)
        return self.output(s, 'finalize')

    async def paused(self, s, _):
        if s.execution_mode == 'batch' and s.error:
            s = self.finish_error(s, RuntimeError(s.error), details=s.error_details)
            return self.output(s, 'finalize')
        answer = interrupt({'run_id': s.run_id, 'status': 'PAUSED', 'reason': s.error,
                            'error_details': s.error_details})
        if answer == 'retarget' and self.sync_guidance(s):
            s = self.changed(s, run_status=RunStatus.RUNNING)
            return self.output(s, 'prelude')
        if answer == 'cancel':
            s = self.changed(s, phase=Phase.FINALIZE, run_status=RunStatus.RUNNING,
                             outcome=Outcome.INCONCLUSIVE, error='用户已取消', error_details=None)
            return self.output(s, 'finalize')
        if (s.error and s.error.startswith('UNKNOWN')
                or s.error_details and s.error_details.get('requires_manual_review')):
            self.event(s, 'run.error', {'error': s.error, 'status': s.run_status,
                'error_details': s.error_details,
                'reason': '恢复已阻止：操作结果或浏览器页面状态需要人工核对；可取消当前 Run。'})
            return self.output(s, 'paused')
        recovery = s.error_details or {}
        preserve_request = (recovery.get('source') == 'model'
            and recovery.get('status') == 'WAITING_NETWORK'
            and recovery.get('request_status') == 'not_sent'
            and not recovery.get('requires_manual_review') and not recovery.get('requires_new_run'))
        s = self.changed(s, run_status=RunStatus.RUNNING, error=None,
                         error_details=recovery if preserve_request else None)
        return self.output(s, 'prelude')

    async def finalize(self, s, _):
        if s.outcome is None:
            s = self.changed(s, outcome=Outcome.INCONCLUSIVE,
                             error=s.error or '未形成确定性验收结论')
        diff_error = None
        try:
            diff_text = self.workspace.diff(s.patch_base_commit or 'HEAD')
        except Exception as e:
            diff_text = ''
            diff_error = sanitize(error_message(e))[:300]
        diff_ref = self.put(s, diff_text, 'diff', name='最终候选补丁差异')
        patch_available = bool(diff_text.strip())
        verified = False
        if s.outcome == Outcome.FIX_VERIFIED:
            try:
                verified = (patch_available and digest(diff_text.encode()) == s.patch_hash
                            and await self.runtime_verification_passed(s))
            except (OSError, ValueError, KeyError):
                verified = False
            if not verified:
                s = self.changed(s, outcome=Outcome.INFRA_FAILURE,
                    error='最终运行环境、补丁或验证证据已失效，无法输出已验证修复',
                    error_details={'terminal_reason': 'final_verification_invalid',
                                   'patch_export_error': diff_error})
        patch_verification = ('verified' if verified else 'unverified' if patch_available else 'none')
        summaries = {
            Outcome.FIX_VERIFIED: '修复补丁通过全部确定性验证，可供后续流程消费。',
            Outcome.NO_BUG_FOUND: '本次测试规范未发现缺陷，未生成修复补丁。',
            Outcome.BUG_CONFIRMED: '缺陷已稳定复现；仅测试模式未执行修复。',
            Outcome.REPAIR_EXHAUSTED: '补充上下文并多次追问后仍未形成有效修复，已输出最终结果。',
            Outcome.LOOP_DETECTED: '检测到重复状态或操作循环，已终止并输出最终结果。',
            Outcome.INFRA_FAILURE: '模型调用、浏览器或运行环境发生重大异常，已输出失败结果。',
            Outcome.POLICY_BLOCKED: '运行被执行策略阻止，已输出最终结果。',
            Outcome.INCONCLUSIVE: '现有证据不足以得出确定性验收结论，已输出最终结果。',
        }
        result_summary = summaries.get(s.outcome, str(s.outcome))
        if patch_available and patch_verification != 'verified':
            result_summary += ' 已导出候选补丁，但该补丁尚未通过全部验证。'
        if s.error:
            result_summary += ' 原因：' + s.error
        cleanup = []
        for name, fn in [('browser', self.browser.close), ('sandbox', self.runner.close)]:
            try:
                await fn()
            except Exception as e:
                cleanup.append(label(name) + '：' + sanitize(error_message(e))[:300])
        status = (RunStatus.SUPERSEDED if s.superseded_by_run_id else
                  RunStatus.CANCELLED if s.error == '用户已取消' else
                  RunStatus.ABNORMAL if s.outcome == Outcome.LOOP_DETECTED else
                  RunStatus.FAILED if s.outcome in {Outcome.INFRA_FAILURE, Outcome.POLICY_BLOCKED, Outcome.REPAIR_EXHAUSTED}
                  or isinstance(s.error, str) and s.error.startswith('ModelOutputError:') else RunStatus.COMPLETED)
        report = {'schema_version': s.schema_version, 'run_id': s.run_id, 'scope_id': s.scope_id,
                  'run_status': status, 'mode': s.mode, 'execution_mode': s.execution_mode,
                  'goal': s.goal, 'parent_run_id': s.parent_run_id,
                  'continuation_instruction': s.continuation_instruction,
                  'continuation_count': s.continuation_count,
                  'abnormal_termination': s.abnormal_termination,
                  'continuation_markers': s.continuation_markers,
                  'guidance': [entry.model_dump(mode='json') for entry in s.guidance],
                  'guidance_constraints': [entry.model_dump(mode='json') for entry in s.guidance_constraints],
                  'superseded_by_run_id': s.superseded_by_run_id,
                  'inherited_evidence_refs': s.inherited_evidence_refs,
                  'remote_config': s.remote_config,
                  'outcome': s.outcome, 'reproduced': s.reproduced,
                  'patch_hash': s.patch_hash, 'branch': s.local_branch, 'merged': False,
                  'patch_base_commit': s.patch_base_commit,
                  'patch_diff_ref': diff_ref, 'patch_available': patch_available,
                  'patch_verification': patch_verification, 'patch_export_error': diff_error,
                  'result_summary': result_summary,
                  'diagnosis_retry_count': s.diagnosis_retry_count,
                  'diagnosis_feedback_refs': s.diagnosis_feedback_refs,
                  'budget': s.budget.model_dump(), 'evidence_refs': s.evidence_refs,
                  'execution_backend': type(self.runner).__name__, 'source_manifest': s.source_manifest,
                  'environment_digest': s.environment_digest, 'baseline_validation_refs': s.baseline_validation_refs,
                   'validation_refs': s.validation_refs, 'error': s.error,
                   'error_details': s.error_details, 'cleanup_errors': cleanup,
                   'model_exchange_refs': s.model_exchange_refs,
                   'reasoning_refs': s.reasoning_refs,
                   'context_manifest_refs': s.context_manifest_refs,
                   'compaction_refs': s.compaction_refs,
                  'agent_instructions_ref': s.agent_instructions_ref,
                  'agent_instructions_hash': s.agent_instructions_hash,
                  'agent_instructions_path': s.agent_instructions_path,
                  'limits': '未发现问题仅适用于本次测试规范覆盖的范围。修复通过仅代表本地验证通过，不代表已合并或发布。'}
        report['behavior_scenarios_error'] = None
        if s.test_spec_ref:
            try:
                report['behavior_scenarios'] = [scenario.id for scenario in self.spec(s).behavior_scenarios]
            except (OSError, ValueError, KeyError, TypeError) as error:
                report['behavior_scenarios'] = None
                report['behavior_scenarios_error'] = sanitize(f'{type(error).__name__}: {error_message(error)}')[:1500]
                self.event(s, 'run.warning', {'source': 'report_behavior_scenarios',
                    'test_spec_ref': s.test_spec_ref, 'error': report['behavior_scenarios_error']})
        try:
            report.update(collect_issues(s, self.store.trace(s.run_id, s.scope_id), lambda reference: self.get(s, reference)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            report['issues'] = []
            report['issues_error'] = sanitize(error_message(error))[:300]
            report['summary'] = '问题明细不可用：关联证据损坏或无法读取，不能确认问题数量。'
            report['coverage'] = '问题明细覆盖不可用；请核对损坏证据和运行警告。'
            report['tested_urls'] = []
            self.event(s, 'run.warning', {'source': 'report_issues',
                'error': report['issues_error']})
        ref = self.put(s, report, name='修复报告数据')
        page = report_page(report)
        if report.get('issues_error'):
            page = page.replace('<p>未形成有证据的问题记录。</p>',
                '<p>问题明细不可用：' + html.escape(report['issues_error']) + '</p>')
        if report['behavior_scenarios_error']:
            page += '<h2>业务场景覆盖不可用</h2><p>' + html.escape(report['behavior_scenarios_error']) + '</p>'
        for evidence in s.evidence_refs:
            try:
                item = self.get(s, evidence)
                obs_ref = item.get('observation_ref')
                if obs_ref:
                    obs = self.get(s, obs_ref)
                    page += f'<h2>证据：{html.escape(evidence)}</h2><img alt="页面证据截图" style="max-width:100%" src="{html.escape(obs["screenshot_ref"], quote=True)}">'
            except (OSError, ValueError, KeyError) as error:
                page += '<p>证据附件无法读取：' + html.escape(evidence) + '</p>'
                self.event(s, 'run.warning', {'source': 'report_evidence', 'evidence_ref': evidence,
                                            'error': sanitize(error_message(error))[:300]})
        if patch_available:
            page += '<h2>候选补丁差异</h2><pre>'+html.escape(diff_text)+'</pre>'
        html_ref = self.put(s, page+'</html>', 'html', name='修复报告')
        if s.reproduced:
            try:
                self.warn_retrieval_degraded(s)
                self.retriever.candidate(s, json.dumps(report, ensure_ascii=False), 'success_recipe' if s.outcome == Outcome.FIX_VERIFIED else 'failure_recipe')
            except Exception as error:
                self.event(s, 'run.warning', {'source': 'report_memory',
                                            'error': sanitize(error_message(error))[:300]})
        s = self.changed(s, report_ref=ref, run_status=status, pending_action=None)
        self.event(s, 'run.finished', {'report_ref': ref, 'html_ref': html_ref, 'outcome': s.outcome})
        events = self.store.trace(s.run_id, s.scope_id)
        self.put(s, events, name='完整事件数据')
        self.put(s, '\n\n'.join(json.dumps(readable(event), ensure_ascii=False, indent=2) for event in events),
                 'txt', name='中文事件日志')
        return self.output(s, 'end')

    async def run(self, state=None, resume=None):
        run_id = state.run_id if state else self.current_run
        self.current_run = run_id
        config = {'configurable': {'thread_id': run_id}, 'recursion_limit': 1500}
        with self.store.writer(run_id):
            if state:
                self.store.save(state)
                if state.continuation_count:
                    self.event(state, 'run.continued', {
                        'continuation_count': state.continuation_count,
                        'marker': state.continuation_markers[-1] if state.continuation_markers else None,
                        'abnormal_termination': state.abnormal_termination,
                    })
                else:
                    self.event(state, 'run.started', {'manifest_ref': state.repo_snapshot_ref})
                invocation = self.output(state, 'prepare')
            else:
                invocation = Command(resume=resume)
            try:
                return await self.graph.ainvoke(invocation, config)
            except GraphInterrupt:
                raise
            except Exception as e:
                scope_id = state.scope_id if state else self.context.active_scope
                current = self.store.load(run_id, scope_id)
                if current.execution_mode != 'batch' and not isinstance(e, GraphRecursionError):
                    raise
                if current.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                                          RunStatus.FAILED, RunStatus.SUPERSEDED}:
                    raise
                if isinstance(e, GraphRecursionError):
                    current = self.changed(current, phase=Phase.FINALIZE,
                        outcome=Outcome.LOOP_DETECTED, run_status=RunStatus.RUNNING,
                        abnormal_termination=True, pending_action=None,
                        error='图执行次数达到上限，检测到流程未能收敛',
                        error_details={'terminal_reason': 'graph_recursion_limit'})
                else:
                    current = self.finish_error(current, e)
                self.event(current, 'run.error', {'error': current.error,
                    'error_details': current.error_details})
                return await self.finalize(current, 'finalize')
