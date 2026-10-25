import pytest

from tracefix.execution.browser import elements, resolve_locator
from tracefix.model.gateway import Gateway, ModelOutputError, ModelResult
from tracefix.runtime.contracts import (Assertion, BrowserAction, Decision, FileEdit, Locator, ObservableWait, Outcome,
    PatchProposal, Phase, ReproductionPlan, RunState, RunStatus, Validation, digest)
from tracefix.runtime.smoke import PNG, FakeBrowser, make_engine
from tests.test_harness_graph import create_harness_engine
from tests.support_sdk_candidate import verified_sdk_candidate


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_snapshot_parser_preserves_first_child_after_empty_container(newline):
    snapshot = newline.join([
        '- generic [ref=e73]:',
        '  - button "上一页" [ref=e74] [cursor=pointer]',
        '  - button "下一页" [disabled] [ref=e75]',
        '- generic [ref=e76]: ',
        '',
        '  - button "刷新" [ref=e77]',
    ])
    parsed = elements(snapshot)
    assert [item['ref'] for item in parsed] == ['e73', 'e74', 'e75', 'e76', 'e77']
    assert resolve_locator(snapshot, Locator(role='button', name='上一页')) == 'e74'
    assert resolve_locator(snapshot, Locator(role='button', name='刷新')) == 'e77'
    assert '[disabled]' in parsed[2]['attrs']


def test_snapshot_parser_counts_duplicate_first_child_locators():
    snapshot = ('- generic [ref=e1]:\n'
                '  - button "保存" [ref=e2]\n'
                '- button "保存" [ref=e3]')
    with pytest.raises(ValueError, match='无法唯一匹配'):
        resolve_locator(snapshot, Locator(role='button', name='保存'))


def exploration_actions():
    checkbox = Locator(role='checkbox', name='Complete task')
    return [BrowserAction(kind='click', locator=checkbox), BrowserAction(kind='observe'),
            BrowserAction(kind='navigate', value='http://app:3000'),
            BrowserAction(kind='click', locator=checkbox), BrowserAction(kind='press', value='Tab'),
            BrowserAction(kind='click', locator=Locator(role='button', name='Done')),
            BrowserAction(kind='navigate', value='http://app:3000')]


def test_reproduction_selection_has_no_browser_tools():
    assert Gateway._native_tools(ReproductionPlan) == []


class ToggleBrowser(FakeBrowser):
    checked = False
    lose_on_reload = False

    async def action(self, action):
        raw = await super().action(action)
        fixed = 'true' in self.workspace.read('src/value.ts')
        if action.kind == 'click' and action.locator.role == 'checkbox' and fixed:
            self.checked = not self.checked
        if action.kind == 'navigate' and self.lose_on_reload:
            self.checked = False
        raw['snapshot'] = ('- heading "Task board" [ref=e1]\n'
                           '- checkbox "Complete task" [ref=e2]' + (' [checked]' if self.checked else '') +
                           '\n- button "Done" [ref=e3]')
        raw['network'] = 'PATCH /api/tasks/1 => [200] OK' if fixed else 'POST /api/tasks/1 => [404] Not Found'
        return raw


async def test_retries_are_excluded_before_patch_and_same_plan_verifies_persistence(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.LOOP_STATE_LIMIT = 100
    fallback = engine.model
    actions = exploration_actions()
    plan_requests = []

    class ExploringModel:
        supports_tool_executor = True
        index = 0

        async def generate(self, schema, context, **options):
            if schema is ReproductionPlan:
                assert 'false' in engine.workspace.read('src/value.ts')
                assert context['exploration_actions'] == [action.model_dump() for action in actions]
                assert len(context['exploration_observations']) >= len(actions)
                plan_requests.append(context)
                value = ReproductionPlan(action_indices=[0, 1, 2], summary='只勾选一次并刷新，排除失败重试和额外筛选')
                return ModelResult(value, {}, 'fake', 'stop')
            if schema is Decision:
                observation = context['observation']
                while self.index < len(actions):
                    action = actions[self.index].model_copy()
                    self.index += 1
                    if action.kind in {'click', 'press'}:
                        action.observation_id = observation['id']
                    if action.locator:
                        action.element_ref = resolve_locator(observation['snapshot'], action.locator)
                    names = {'click': 'BrowserClick', 'observe': 'BrowserSnapshot',
                             'navigate': 'BrowserNavigate', 'press': 'BrowserPress'}
                    arguments = action.model_dump(exclude_none=True)
                    arguments.pop('kind')
                    result = await options['tool_executor'](names[action.kind], arguments, f'call-{self.index}')
                    observation = result['observation']
                return ModelResult(Decision(action=BrowserAction(kind='finish')), {}, 'fake', 'stop')
            forwarded = {key: value for key, value in options.items()
                         if key not in {'tool_executor', 'on_tool_result', 'context_provider'}}
            return await fallback.generate(schema, context, **forwarded)

    engine.model = ExploringModel()
    spec_hash = state.test_spec_hash
    engine, state, harness, _ = await create_harness_engine(tmp_path / 'sdk', engine_state=(engine, state))
    original_rebind = harness.rebind

    async def rebind(current, workspace, source):
        await original_rebind(current, workspace, source)
        current.browser = ToggleBrowser(workspace)
        original_command = current.runner.command

        async def command(kind):
            if kind == 'reset':
                current.browser.checked = False
            return await original_command(kind)

        current.runner.command = command

    async def stop_at_review(current, active):
        return current.output(active, 'end')

    harness.rebind = rebind
    harness.review = stop_at_review
    output = await engine.run(state)
    saved = RunState.model_validate(output['data'])
    assert saved.run_status == RunStatus.RUNNING and saved.phase == Phase.REVIEW, saved.error
    assert saved.approval_ref is None and saved.run_id != state.run_id
    assert saved.outcome == Outcome.FIX_VERIFIED
    assert saved.budget.patches == 1
    assert saved.trial == 0 and saved.reproduced is False and len(saved.validation_refs) == 6
    assert saved.initial_check_result_refs
    initial_results = [engine.get(saved, ref) for ref in saved.initial_check_result_refs]
    assert len(initial_results) == len(engine.check_plan(saved).items)
    assert all(result['stage'] == 'explore' for result in initial_results)
    assert any(result['status'] == 'fail' for result in initial_results)
    assert saved.test_spec_hash == spec_hash
    assert len(plan_requests) == 1
    assert engine.get(saved, saved.exploration_plan_ref) == [action.model_dump() for action in actions]
    assert engine.get(saved, saved.replay_plan_ref) == [action.model_dump() for action in actions[:3]]
    observations = engine.phase_observations(saved, Phase.VERIFY)
    clicks = [item for item in observations if item['action'].get('kind') == 'click']
    assert len(clicks) == 1
    assert '[checked]' in clicks[0]['observation']['snapshot']
    original = next(engine.get(saved, ref) for ref in saved.validation_refs
                    if engine.get(saved, ref)['kind'] == 'original')
    result = engine.get(saved, original['artifact_ref'])
    assert '[checked]' in engine.get(saved, result['observation_ref'])['snapshot']


async def plan_engine(tmp_path, indices):
    engine, state = make_engine(tmp_path)
    # 复现 fixture 也走真实初始化，源码清单和环境摘要均由生产流程验证。
    engine.store.save(state)
    output = await engine.prepare(state, None)
    state = RunState(**output['data'])
    assert engine.runner.started and not engine.runner.closed
    assert state.source_aligned and state.environment_digest == engine.runner.environment_digest
    state.phase = Phase.REPRODUCE
    state.observation_ref = await engine.capture(state, await engine.browser.action(BrowserAction(kind='observe')))
    state.replay_plan_ref = engine.put(state, [action.model_dump() for action in exploration_actions()])
    engine.store.save(state)

    async def generate(schema, context, **options):
        return ModelResult(ReproductionPlan(action_indices=indices, summary='测试复现计划'), {}, 'fake', 'stop')

    engine.model.generate = generate
    return engine, state


@pytest.mark.parametrize('indices', [[-1], [7], [2, 0], [0, 0, 2], [1, 2], [0]])
async def test_invalid_plan_cannot_freeze_or_change_spec(tmp_path, indices):
    engine, state = await plan_engine(tmp_path, indices)
    original_ref, original_spec = state.replay_plan_ref, state.test_spec_hash
    with pytest.raises(ModelOutputError):
        await engine.freeze_reproduction_plan(state)
    saved = engine.store.load(state.run_id, state.scope_id)
    assert saved.replay_plan_ref == original_ref
    assert saved.test_spec_hash == original_spec
    assert not saved.reproduction_plan_frozen


async def test_plan_keeps_repeated_actions_when_selected_for_the_goal(tmp_path):
    engine, state = await plan_engine(tmp_path, [0, 2, 3, 6])
    frozen = await engine.freeze_reproduction_plan(state)
    selected = engine.get(frozen, frozen.replay_plan_ref)
    assert [action['kind'] for action in selected] == ['click', 'navigate', 'click', 'navigate']


async def test_plan_cannot_be_reselected_after_patch(tmp_path):
    engine, state = await plan_engine(tmp_path, [0, 2])
    state.patch_hash = 'existing-patch'
    with pytest.raises(ValueError, match='修补后不能'):
        await engine.freeze_reproduction_plan(state)


@pytest.mark.parametrize('passes,confirmed', [
    ([False, False, True], True), ([True, False, False], True),
    ([False, True, True], False), ([True, True, True], False),
])
async def test_reproduction_counts_only_independent_failures(tmp_path, passes, confirmed):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    output = await engine.prepare(state, None)
    state = RunState(**output['data'])
    assert engine.runner.started and not engine.runner.closed
    assert state.source_aligned and state.environment_digest == engine.runner.environment_digest
    state.mode, state.phase = 'test', Phase.REPRODUCE
    state.reproduction_plan_frozen = True
    state.replay_plan_ref = engine.put(state, [])
    engine.store.save(state)
    while state.phase == Phase.REPRODUCE:
        engine.browser.bugfree = passes[state.trial]
        output = await engine.reproduce(state, None)
        state = RunState(**output['data'])
    assert state.trial == 3
    assert state.reproduced is confirmed
    assert state.outcome == (Outcome.BUG_CONFIRMED if confirmed else Outcome.INCONCLUSIVE)


@pytest.mark.parametrize('lose_on_reload', [False, True])
async def test_verification_preserves_reload_and_checks_final_state(tmp_path, lose_on_reload):
    engine, state = make_engine(tmp_path)
    engine.browser = ToggleBrowser(engine.workspace)
    engine.browser.lose_on_reload = lose_on_reload
    engine.store.save(state)
    output = await engine.prepare(state, None)
    state = RunState(**output['data'])
    assert engine.runner.started and not engine.runner.closed
    assert state.source_aligned and state.environment_digest == engine.runner.environment_digest
    actions = exploration_actions()[:3]
    error_state = Assertion(locator=Locator(role='alert', name=''), condition='visible')
    actions[0].preconditions = [Assertion(locator=actions[0].locator, condition='unchecked')]
    actions[0].postconditions = [error_state]
    actions[0].wait = ObservableWait(assertions=[error_state], max_observations=1)
    actions[2].wait = ObservableWait(assertions=[
        Assertion(locator=actions[0].locator, condition='unchecked')], max_observations=1)
    original_plan = [action.model_dump() for action in actions]
    state.replay_plan_ref = engine.put(state, original_plan)
    state.phase = Phase.REPRODUCE
    spec = engine.spec(state).model_dump()
    spec_hash = state.test_spec_hash

    async def select_plan(schema, context, **options):
        assert schema is ReproductionPlan
        return ModelResult(ReproductionPlan(action_indices=[0, 1, 2], summary='勾选并刷新'), {}, 'fake', 'stop')

    engine.model.generate = select_plan
    state = await engine.freeze_reproduction_plan(state)
    assert engine.get(state, state.exploration_plan_ref) == original_plan
    assert engine.spec(state).model_dump() == spec
    assert state.test_spec_hash == spec_hash
    proposal = PatchProposal(summary='修复接口', evidence_refs=['baseline.json'], edits=[FileEdit(
        path='src/value.ts', before_hash=digest(engine.workspace.read('src/value.ts').encode()),
        content='export const persisted = true;\n')])
    result = engine.workspace.apply(proposal)
    state.patch_hash = result['patch_hash']
    state.phase, state.validation_index = Phase.VERIFY, 4
    engine.store.save(state)
    while state.phase == Phase.VERIFY and state.validation_index == 4:
        output = await engine.verify(state, None)
        state = RunState(**output['data'])
    validation = engine.get(state, state.validation_refs[-1])
    assert validation['passed'] is not lose_on_reload
    assert state.phase == (Phase.DIAGNOSE if lose_on_reload else Phase.VERIFY)
    assert [action['kind'] for action in engine.browser.calls] == ['navigate', 'click', 'observe', 'navigate']


@pytest.mark.parametrize('invented_reference', [False, True])
async def test_sdk_feedback_reads_current_validation_and_rejects_missing_artifacts(tmp_path, invented_reference):
    engine, state = await verified_sdk_candidate(tmp_path)
    observation = {'id': 'latest', 'url': state.url, 'snapshot': '- checkbox "Complete task" [ref=e1]',
                   'network': 'PATCH /api/tasks/1 => [200] OK', 'console': ''}
    state.observation_ref = await engine.capture(state, {**observation, 'png': PNG})
    observation = engine.get(state, state.observation_ref)
    result, result_ref = engine.check(state, engine.spec(state).assertions)
    assert not result['passed']
    original = next(engine.get(state, ref) for ref in state.validation_refs
                    if engine.get(state, ref)['kind'] == 'original')
    current = Validation.model_validate({**original, 'passed': False,
        'artifact_ref': '9999_missing.json' if invented_reference else result_ref})
    current_ref = engine.put(state, current.model_dump())
    state.validation_refs.append(current_ref)
    engine.store.save(state)
    if invented_reference:
        with pytest.raises((OSError, ValueError)):
            engine.harness.failed_feedback(engine, state)
        assert engine.harness.session(state).feedback_ref is None
    else:
        engine.harness.failed_feedback(engine, state)
        feedback = engine.get(state, engine.harness.session(state).feedback_ref)
        verification = engine.get(state, feedback['verification_ref'])
        assert verification['passed'] is False
        assert verification['regression_refs'][-1] == current_ref
        recorded = engine.get(state, engine.get(state, current_ref)['artifact_ref'])
        assert recorded['observation_ref'] == state.observation_ref
        assert engine.get(state, recorded['observation_ref']) == observation
