"""One LangGraph, one writer, bounded microsteps, evidence-driven transitions."""
from __future__ import annotations

import asyncio
import dataclasses
import html
import json
import os
import re
import time
from collections import Counter
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph.errors import GraphInterrupt

from tracefix.execution.browser import (MCPActionUnknown, MCPConnectionError, assertions,
    resolve_locator, validate_spec_observation)
from tracefix.model.gateway import ModelError, ModelOutputError
from tracefix.knowledge.context import build_context
from tracefix.knowledge.selection import select_documents
from tracefix.runtime.contracts import (BudgetExceeded, BrowserAction, Decision, Outcome,
    PatchProposal, Phase, ReplayUnbound, RunState, RunStatus, TestSpec, Validation, digest, new_id,
    reduce_state, verification_gate)
from tracefix.storage.artifacts import redact, sanitize
from tracefix.storage.presentation import label, readable, report_page
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


def stable_snapshot(snapshot):
    snapshot = re.sub(r'^- Console:.*$', '', snapshot, flags=re.MULTILINE)
    return re.sub(r'\s*\[ref=[^\]]+\]', '', snapshot)


class Engine:
    def __init__(self, store, artifacts, scopes, context, profile, workspace, runner,
                 browser, model, retriever, source, checkpointer, notify=None):
        self.store, self.artifacts, self.scopes, self.context = store, artifacts, scopes, context
        self.profile, self.workspace, self.runner = profile, workspace, runner
        self.browser, self.model, self.retriever, self.source = browser, model, retriever, source
        self.notify = notify
        self.control = None
        self.notes = []
        self.documents = None
        self.document_context = {}
        self.graph = self._graph(checkpointer)

    def put(self, s, value, ext='json', *, name='数据记录'):
        return self.artifacts.put(s.scope_id, s.run_id, value, ext, label=f'{label(s.phase)}_{name}')

    def get(self, s, ref):
        return self.artifacts.json(s.scope_id, s.run_id, ref)

    def spec(self, s):
        return TestSpec(**self.get(s, s.test_spec_ref))

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
        self._notify(e)

    def _notify(self, event):
        if self.notify:
            try:
                self.notify(event)
            except Exception:
                pass

    def changed(self, s, **delta):
        # 乐观并发：期望版本取自已持久化的状态，过期的内存状态不得覆盖更新的版本。
        persisted = self.store.load(s.run_id, s.scope_id)
        new = reduce_state(s, persisted.revision, **delta)
        # 探索期的进展只认快照变化（见 execute）；其余阶段每一次成功推进都刷新停滞计时器。
        if new.phase != Phase.EXPLORE or new.phase != s.phase:
            new.budget.last_progress = time.time()
        self.store.save(new)
        self.event(new, 'state.changed', {'phase': new.phase, 'status': new.run_status})
        return new

    async def operation(self, s, name, intent, fn, reconcile=None):
        op_id = f'{s.run_id}:{s.revision}:{name}'
        try:
            receipt = self.store.begin(s, op_id, intent, notify=self._notify)
        except UnknownOperation:
            if not reconcile:
                raise
            receipt = reconcile()
            self.store.finish(s, op_id, receipt, notify=self._notify)
        if receipt is not None:
            return receipt
        try:
            result = await fn()
        except (MCPConnectionError, MCPActionUnknown) as error:
            self.event(s, 'tool.error', {'operation_id': op_id,
                'error': sanitize(error_message(error)), 'status': error.status,
                'category': error.category, 'details': redact(error.details)})
            raise
        self.store.finish(s, op_id, result, notify=self._notify)
        return result

    async def model_call(self, s, schema, ctx, image=None, validate_output=None):
        if s.continuation_instruction:
            ctx = {**ctx, 'user_continuation': s.continuation_instruction,
                   'continuation_count': s.continuation_count,
                   'previous_termination': s.continuation_markers[-1] if s.continuation_markers else None}
        budget = s.budget
        logical_call = s.budget.model_calls + 1

        def attempt(model, request, attempt_number):
            nonlocal budget
            budget = s.budget
            budget.check_time()
            budget = budget.charge('model_calls')
            # A conservative UTF-8 byte bound plus output reservation prevents
            # accepting a request with no accounting headroom.
            reserve = len(json.dumps(ctx, ensure_ascii=False).encode()) + 4096 + (4096 if image else 0)
            if budget.tokens + reserve > budget.max_tokens:
                raise BudgetExceeded('令牌预留不足')
            s.budget = budget
            self.store.save(s)
            exchange = {'exchange_id': request.get('logical_exchange_id') or new_id('model'),
                        'logical_exchange_id': request.get('logical_exchange_id'),
                        'logical_call': logical_call,
                        'attempt': attempt_number, 'phase': str(s.phase),
                        'schema': schema.__name__, 'model': model}
            request_ref = self.put(s, {**exchange, 'request': request},
                                   name=f'模型调用{logical_call:03d}_尝试{attempt_number}_输入')
            exchange['request_ref'] = request_ref
            s.model_exchange_refs.append(request_ref)
            self.store.save(s)
            self.event(s, 'model.started', exchange)
            self.event(s, 'model.request.persisted', exchange)
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
            reasoning_present = any(isinstance(choice, dict) and
                isinstance(choice.get('message'), dict) and
                choice['message'].get('reasoning_content') is not None for choice in choices)
            self.event(s, 'model.response.persisted', {**exchange,
                'response_ref': response_ref, 'http_status': raw.get('http_status'),
                'reasoning_content_present': reasoning_present})

        def error(exchange, raw):
            exchange = exchange or {'exchange_id': new_id('model'), 'schema': schema.__name__,
                                    'phase': str(s.phase)}
            raw = raw if isinstance(raw, dict) else {'message': sanitize(str(raw))}
            error_ref = self.put(s, {**exchange, 'error': raw},
                                 name=f'模型调用{logical_call:03d}_尝试{exchange.get("attempt", 1)}_错误')
            s.model_exchange_refs.append(error_ref)
            self.store.save(s)
            self.event(s, 'model.error.persisted', {**exchange, 'error_ref': error_ref,
                'status': raw.get('status'), 'category': raw.get('category'),
                'details': redact(raw)})

        def usage(u):
            nonlocal budget
            # Actual usage is never discarded/refunded even when it exceeds a cap.
            budget = s.budget
            data = budget.model_dump()
            data['tokens'] += int(u['total_tokens'])
            input_rate = float(os.getenv('TRACEFIX_INPUT_USD_PER_M', '1'))
            output_rate = float(os.getenv('TRACEFIX_OUTPUT_USD_PER_M', '4'))
            data['cost_usd'] += (u.get('prompt_tokens', 0)*input_rate + u.get('completion_tokens', 0)*output_rate)/1_000_000
            budget = type(budget)(**data)
            s.budget = budget
            self.store.save(s)
            self.event(s, 'model.usage', {'usage': u, 'cost_is_configured_estimate': True})
            if budget.tokens > budget.max_tokens or budget.cost_usd > budget.max_cost_usd:
                raise BudgetExceeded('实际用量超过预算')

        validation = {'validate_output': validate_output} if validate_output else {}
        result = await self.model.generate(schema, ctx, image=image,
            agent_instructions=self.agent_instructions(s), on_attempt=attempt,
            on_response=response, on_error=error, on_usage=usage, **validation)
        self.event(s, 'model.called', {'model_revision': result.model_revision, 'usage': result.usage,
                                      'finish_reason': result.finish_reason})
        if isinstance(result.value, (Decision, PatchProposal)):
            self.event(s, 'model.decision', {'summary': result.value.summary,
                'evidence_refs': result.value.evidence_refs,
                'action': result.value.action.model_dump() if isinstance(result.value, Decision) else
                          {'kind': 'propose_patch', 'files': [edit.path for edit in result.value.edits]}})
        return result.value

    async def capture(self, s, raw):
        png = raw.pop('png')
        if self.profile.screenshot_redaction != 'public_demo':
            raise PermissionError('私有截图在捕获前需要经过批准的脱敏适配器处理')
        screenshot = self.put(s, png, 'png', name='页面截图')
        raw['screenshot_ref'] = screenshot
        raw['redaction'] = 'public_demo_no_credentials'
        return self.put(s, raw, name='页面观察')

    async def act(self, s, action, *, frozen=False):
        self.scopes.assert_current(self.context)
        spec = self.spec(s)
        obs = self.get(s, s.observation_ref) if s.observation_ref else None
        if frozen and action.locator:
            try:
                element_ref = resolve_locator(obs['snapshot'], action.locator)
            except ValueError as e:
                # 重放的是已记录的动作，没有可回退的模型；定位器不再唯一绑定说明该场景无法重放，
                # 这不是基础设施故障，应作为无结论收尾。
                raise ReplayUnbound('已记录的动作无法在当前页面重放：' + str(e)) from e
            action = action.model_copy(update={'observation_id': obs['id'], 'element_ref': element_ref})
        self.browser.policy.browser(s, action, spec, obs)
        s.budget = s.budget.charge('browser_actions')
        self.store.save(s)
        async def perform():
            raw = await self.browser.action(action)
            return {'observation_ref': await self.capture(s, raw)}
        receipt = await self.operation(s, 'browser', action.model_dump(), perform)
        return receipt['observation_ref']

    def _graph(self, saver):
        graph = StateGraph(GraphState)
        names = ['prelude', 'prepare', 'decide', 'execute', 'explore_gate', 'reproduce',
                 'diagnose', 'patch', 'verify', 'review', 'paused', 'finalize']
        for name in names:
            async def wrapped(data, node=name):
                s = RunState(**data['data'])
                # Usage committed before a crash takes precedence over old graph state.
                saved = self.store.load(s.run_id, s.scope_id)
                for metric in ('model_calls', 'browser_actions', 'patches', 'subtasks', 'tokens', 'cost_usd'):
                    setattr(s.budget, metric, max(getattr(s.budget, metric), getattr(saved.budget, metric)))
                try:
                    return await getattr(self, node)(s, data.get('next_node', 'prepare'))
                except UnknownOperation as e:
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
                    error_details = None
                    if isinstance(e, (ModelError, MCPConnectionError, MCPActionUnknown)):
                        error_details = redact({**e.details, 'status': e.status,
                            'category': e.category, 'type': type(e).__name__,
                            'source': 'model' if isinstance(e, ModelError) else 'browser'})
                        if e.status in {'UNKNOWN_OPERATION', 'WAITING_NETWORK'}:
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
                    outcome = (Outcome.POLICY_BLOCKED if isinstance(e, PermissionError) else
                               Outcome.INCONCLUSIVE if isinstance(e, (BudgetExceeded, ModelOutputError, ReplayUnbound)) else Outcome.INFRA_FAILURE)
                    s = self.changed(self.store.load(s.run_id, s.scope_id), phase=Phase.FINALIZE, outcome=outcome,
                                     error=sanitize(f'{type(e).__name__}: {error_message(e)}')[:1500],
                                     error_details=error_details, pending_action=None)
                    self.event(s, 'run.error', {'error': s.error, 'error_details': error_details})
                    return {'data': s.model_dump(mode='json'), 'next_node': 'finalize'}
            graph.add_node(name, wrapped)
            graph.add_conditional_edges(name, lambda data: data['next_node'], {n: n for n in names} | {'end': END})
        graph.add_edge(START, 'prelude')
        return graph.compile(checkpointer=saver)

    def output(self, s, next_node):
        return {'data': s.model_dump(mode='json'), 'next_node': next_node}

    def route(self, s):
        return {Phase.PREPARE: 'prepare', Phase.EXPLORE: 'decide', Phase.REPRODUCE: 'reproduce',
                Phase.DIAGNOSE: 'diagnose', Phase.PATCH: 'patch', Phase.VERIFY: 'verify',
                Phase.REVIEW: 'review', Phase.FINALIZE: 'finalize'}[s.phase]

    async def prelude(self, s, _):
        self.scopes.assert_current(self.context)
        if self.control == 'cancel':
            self.control = None
            s = self.changed(s, phase=Phase.FINALIZE, error='用户已取消', outcome=Outcome.INCONCLUSIVE)
            return self.output(s, 'finalize')
        if self.control == 'pause':
            self.control = None
            s = self.changed(s, run_status=RunStatus.PAUSED)
            return self.output(s, 'paused')
        s.budget.check_time()
        if self.notes:
            for note in self.notes:
                self.event(s, 'input.applied', {'text': note, 'effect': '已记录澄清；冻结的 TestSpec 未改变'})
            self.notes.clear()
        return self.output(s, self.route(s))

    async def prepare(self, s, _):
        if s.step == 0:
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
            spec = await self.model_call(s, TestSpec, {'goal': s.goal, 'url': s.url,
                'observation': observation,
                'reference_documents': knowledge,
                'instruction': '根据用户目标和首次只读观测编译 TestSpec。授权动作必须使用 schema 枚举；'
                               '定位器使用页面中的完整可访问名称；保留用户要求的刷新后断言，并提供独立回归场景。'},
                validate_output=lambda candidate: validate_spec_observation(candidate, observation))
            validate_spec_observation(spec, observation)
            ref = self.put(s, spec.model_dump(), name='测试规范')
            s = self.changed(s, test_spec_ref=ref, test_spec_hash=digest(spec))
            return self.output(s, 'prelude')
        phase = Phase.VERIFY if s.continuation_count and s.patch_hash and s.reproduced and s.replay_plan_ref else Phase.EXPLORE
        s = self.changed(s, phase=phase, step=0)
        return self.output(s, 'prelude')

    async def decide(self, s, _):
        spec = self.spec(s)
        obs = self.get(s, s.observation_ref)
        image = self.artifacts.read(s.scope_id, s.run_id, obs['screenshot_ref'])
        plan = self.get(s, s.replay_plan_ref) if s.replay_plan_ref else []
        context = build_context(s, spec.model_dump(), obs, pairs=[{'action': a, 'result': 'see current observation'} for a in plan[-4:]])
        context['reference_documents'] = await select_documents(self, s, obs)
        context['instruction'] = '每次只返回一个浏览器动作。必须匹配当前 observation_id 和 element_ref。完成请求的交互后使用 finish；它只是请求运行时执行确定性断言检查。'
        decision = await self.model_call(s, Decision, context, image=image)
        decision.evidence_refs = normalize_decision_evidence_refs(
            decision.evidence_refs, s.evidence_refs, s.observation_ref, obs)
        self.browser.policy.browser(s, decision.action, spec, obs)
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
        if ref != s.observation_ref:
            # Only a changed snapshot counts as progress (new UUIDs do not).
            before = self.get(s, s.observation_ref)
            after = self.get(s, ref)
            if before['snapshot'] != after['snapshot']:
                s.budget.last_progress = time.time()
        s = self.changed(s, observation_ref=ref, replay_plan_ref=plan_ref, step=s.step+1)
        return self.output(s, 'explore_gate')

    def check(self, s, checks):
        obs = self.get(s, s.observation_ref)
        result = assertions(obs['snapshot'], checks)
        result.update(observation_ref=s.observation_ref, test_spec_hash=s.test_spec_hash,
                      plan_hash=digest(self.get(s, s.replay_plan_ref)) if s.replay_plan_ref else None,
                      source_manifest=s.source_manifest, environment_digest=s.environment_digest,
                      patch_hash=s.patch_hash)
        ref = self.put(s, result, name='断言检查结果')
        self.event(s, 'gate.decided', {'passed': result['passed'], 'evidence_ref': ref})
        return result, ref

    async def explore_gate(self, s, _):
        done = s.pending_action['kind'] == 'finish'
        limit = s.step >= self.spec(s).max_steps
        stalled = repeated_action(s.action_fingerprints)
        if not done and not limit and not stalled:
            return self.output(self.changed(s, pending_action=None), 'prelude')
        result, ref = self.check(s, self.spec(s).assertions)
        if not done and limit:
            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.INCONCLUSIVE,
                error='已达到探索步骤上限；模型没有提出停止动作', evidence_refs=s.evidence_refs+[ref], pending_action=None)
        elif result['passed']:
            s = self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.NO_BUG_FOUND,
                evidence_refs=s.evidence_refs+[ref], pending_action=None)
        else:
            s = self.changed(s, phase=Phase.REPRODUCE, replay_index=0, trial=0,
                evidence_refs=s.evidence_refs+[ref], failure_signatures=[digest(result['assertions'])], pending_action=None)
        return self.output(s, 'prelude')

    async def reset(self, s):
        async def reset_env():
            await self.browser.close()
            r = await self.runner.command('reset')
            if not r['passed']:
                raise RuntimeError('重置失败')
            await self.browser.open()
            # New isolated browser process plus server reset, never reload alone.
            raw = await self.browser.action(BrowserAction(kind='navigate', value=s.url))
            return {'observation_ref': await self.capture(s, raw)}
        s.budget = s.budget.charge('browser_actions')
        self.store.save(s)
        return await self.operation(s, 'scenario.reset', {'trial': s.trial, 'validation': s.validation_index}, reset_env)

    async def reproduce(self, s, _):
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
                matching = sum(x == signatures[0] for x in signatures[1:])
                stable = matching >= 2
                if stable and s.mode == 'repair':
                    s = self.changed(s, phase=Phase.DIAGNOSE, reproduced=True)
                else:
                    s = self.changed(s, phase=Phase.FINALIZE, reproduced=stable, outcome=Outcome.INCONCLUSIVE,
                                     error='已验证缺陷报告（仅测试模式）' if stable else '故障无法在三次试验中的至少两次复现')
        return self.output(s, 'prelude')

    async def diagnose(self, s, _):
        if s.budget.patches >= s.budget.max_patches:
            return self.output(self.changed(s, phase=Phase.FINALIZE, outcome=Outcome.REPAIR_EXHAUSTED), 'prelude')
        self.workspace.check_frozen(self.source)
        overlay = s.source_manifest + (':' + s.patch_hash if s.patch_hash else '')
        await self.retriever.index(self.workspace, overlay)
        code = await self.retriever.retrieve(s.goal, overlay, 'M1', 10)
        recipes = await self.retriever.retrieve(s.goal, s.source_manifest, 'M3', 3)
        # Authorized current file reads validate retrieved cards and provide before_hash.
        preferred = [json.loads(card['content']).get('path') for card in code if card.get('content', '').startswith('{')]
        cards = self.workspace.cards(preferred_paths=preferred)
        context = build_context(s, self.spec(s).model_dump(), cards=cards)
        context['reference_documents'] = await select_documents(self, s)
        context.update(instruction='请诊断并返回最小化的完整文件替换内容。只能编辑当前允许的文件。引用已有证据；每个 before_hash 必须匹配已提供的文件。',
                       allowed_files=self.workspace.allowed_files, repair_memory=recipes, retrieval_ids=[x['id'] for x in code],
                       failures=[self.get(s, r) for r in s.evidence_refs[-4:]],
                       previous_validation=[self.get(s, r) for r in s.validation_refs])
        if os.getenv('TRACEFIX_WORKER') == '1' and s.budget.subtasks < s.budget.max_subtasks:
            from tracefix.runtime.worker import ReadOnlyWorker, SubtaskSpec
            spec = SubtaskSpec(s.goal, 'code_investigator', s.revision,
                tuple(c['path'] for c in cards[:3]), tuple(s.evidence_refs[-2:]))
            result = await ReadOnlyWorker(self).run(s, spec)
            worker_ref = self.put(s, result.model_dump(), name='子任务调查结果')
            s.subtask_refs.append(worker_ref)
            context['read_only_investigation'] = result.model_dump()
            self.event(s, 'subtask.completed', {'artifact_ref': worker_ref})
        patch = await self.model_call(s, PatchProposal, context)
        if not set(patch.evidence_refs) <= set(s.evidence_refs):
            raise ValueError('补丁引用了不存在的证据')
        ref = self.put(s, patch.model_dump(), name='补丁方案')
        s = self.changed(s, phase=Phase.PATCH, patch_ref=ref, hypothesis_refs=s.hypothesis_refs+[ref])
        return self.output(s, 'prelude')

    async def patch(self, s, _):
        self.scopes.assert_current(self.context)
        if not all(self.bundle_exists(s,r) for r in s.evidence_refs):
            raise ValueError('所需的复现证据缺失或已损坏')
        proposal = PatchProposal(**self.get(s, s.patch_ref))
        s.budget = s.budget.charge('patches')
        self.store.save(s)
        async def apply():
            return self.workspace.apply(proposal)
        r = await self.operation(s, 'workspace.patch', proposal.model_dump(), apply,
                                 reconcile=lambda: self.workspace.reconcile(proposal))
        diff = self.put(s, self.workspace.diff(), 'diff', name='补丁差异')
        self.event(s, 'patch.applied', {'diff_ref': diff, 'patch_hash': r['patch_hash']})
        s = self.changed(s, phase=Phase.VERIFY, patch_hash=r['patch_hash'], validation_index=0, replay_index=0)
        return self.output(s, 'prelude')

    async def verify(self, s, _):
        self.workspace.check_frozen(self.source)
        if digest(self.workspace.diff().encode()) != s.patch_hash:
            raise PermissionError('补丁在验证开始后发生了变化')
        kinds = ['static', 'unit', 'build', 'health', 'original', 'regression']
        kind = kinds[s.validation_index]
        if kind in {'original', 'regression'}:
            spec = self.spec(s)
            plan = ([BrowserAction(**a) for a in self.get(s, s.replay_plan_ref)] if kind == 'original' else spec.regression_plan)
            if s.replay_index == 0:
                r = await self.reset(s)
                return self.output(self.changed(s, observation_ref=r['observation_ref'], replay_index=1), 'prelude')
            if s.replay_index <= len(plan):
                ref = await self.act(s, plan[s.replay_index-1], frozen=True)
                return self.output(self.changed(s, observation_ref=ref, replay_index=s.replay_index+1), 'prelude')
            result, _ = self.check(s, spec.assertions if kind == 'original' else spec.regression_assertions)
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
                           'health': '健康检查', 'original': '原问题重测', 'regression': '回归测试'}[kind]
        artifact = self.put(s, result, name=validation_name+'结果')
        validation = Validation(kind=kind, passed=result['passed'], source_manifest=s.source_manifest,
            patch_hash=s.patch_hash, environment_digest=s.environment_digest,
            test_spec_hash=s.test_spec_hash, artifact_ref=artifact)
        ref = self.put(s, validation.model_dump(), name=validation_name+'验证记录')
        s = self.changed(s, validation_refs=s.validation_refs+[ref], replay_index=0)
        if not validation.passed:
            self.event(s, 'gate.decided', {'validation': kind, 'passed': False})
            return self.output(self.changed(s, phase=Phase.DIAGNOSE), 'prelude')
        if s.validation_index < len(kinds)-1:
            return self.output(self.changed(s, validation_index=s.validation_index+1), 'prelude')
        vals = [Validation(**self.get(s, r)) for r in s.validation_refs]
        if not verification_gate(s, vals, lambda ref: self.bundle_exists(s, ref)):
            raise ValueError('确定性验证门禁拒绝了该证据')
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
        if digest(self.workspace.diff().encode()) != s.patch_hash:
            raise PermissionError('审批对应的补丁哈希已变化')
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
        answer = interrupt({'run_id': s.run_id, 'status': 'PAUSED', 'reason': s.error,
                            'error_details': s.error_details})
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
        s = self.changed(s, run_status=RunStatus.RUNNING, error=None, error_details=None)
        return self.output(s, 'prelude')

    async def finalize(self, s, _):
        cleanup = []
        for name, fn in [('browser', self.browser.close), ('sandbox', self.runner.close)]:
            try:
                await fn()
            except Exception as e:
                cleanup.append(label(name) + '：' + sanitize(error_message(e))[:300])
        status = (RunStatus.CANCELLED if s.error == '用户已取消' else
                  RunStatus.FAILED if s.outcome in {Outcome.INFRA_FAILURE, Outcome.POLICY_BLOCKED, Outcome.REPAIR_EXHAUSTED}
                  or isinstance(s.error, str) and s.error.startswith('ModelOutputError:') else RunStatus.COMPLETED)
        report = {'schema_version': s.schema_version, 'run_id': s.run_id, 'scope_id': s.scope_id,
                  'run_status': status, 'mode': s.mode,
                  'goal': s.goal, 'parent_run_id': s.parent_run_id,
                  'continuation_instruction': s.continuation_instruction,
                  'continuation_count': s.continuation_count,
                  'abnormal_termination': s.abnormal_termination,
                  'continuation_markers': s.continuation_markers,
                  'remote_config': s.remote_config,
                  'outcome': s.outcome, 'reproduced': s.reproduced,
                  'patch_hash': s.patch_hash, 'branch': s.local_branch, 'merged': False,
                  'budget': s.budget.model_dump(), 'evidence_refs': s.evidence_refs,
                  'execution_backend': type(self.runner).__name__, 'source_manifest': s.source_manifest,
                  'environment_digest': s.environment_digest, 'baseline_validation_refs': s.baseline_validation_refs,
                   'validation_refs': s.validation_refs, 'error': s.error,
                   'error_details': s.error_details, 'cleanup_errors': cleanup,
                   'model_exchange_refs': s.model_exchange_refs,
                  'agent_instructions_ref': s.agent_instructions_ref,
                  'agent_instructions_hash': s.agent_instructions_hash,
                  'agent_instructions_path': s.agent_instructions_path,
                  'limits': '未发现问题仅适用于本次测试规范覆盖的范围。修复通过仅代表本地验证通过，不代表已合并或发布。'}
        ref = self.put(s, report, name='修复报告数据')
        page = report_page(report)
        for evidence in s.evidence_refs:
            item = self.get(s, evidence)
            obs_ref = item.get('observation_ref')
            if obs_ref:
                obs = self.get(s, obs_ref)
                page += f'<h2>证据：{html.escape(evidence)}</h2><img alt="页面证据截图" style="max-width:100%" src="{html.escape(obs["screenshot_ref"], quote=True)}">'
        patches = [e for e in self.store.trace(s.run_id,s.scope_id) if e['type']=='patch.applied']
        if patches:
            diff_text = self.artifacts.read(s.scope_id,s.run_id,patches[-1]['payload']['diff_ref']).decode()
            page += '<h2>候选补丁差异</h2><pre>'+html.escape(diff_text)+'</pre>'
        html_ref = self.put(s, page+'</html>', 'html', name='修复报告')
        if s.reproduced:
            self.retriever.candidate(s, json.dumps(report, ensure_ascii=False), 'success_recipe' if s.outcome == Outcome.FIX_VERIFIED else 'failure_recipe')
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
                return await self.graph.ainvoke(self.output(state, 'prepare'), config)
            return await self.graph.ainvoke(Command(resume=resume), config)
