from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tracefix.rules.models import Rule, RuleDetection
from tracefix.runtime.checks import make_check_plan
from tracefix.runtime.contracts import (
    Assertion, BehaviorScenario, BehaviorStep, BrowserAction, CheckJudgement, GoalCheckDraft, Locator, Phase,
    RunState, TestSpec, digest, reduce_state,
)
from tracefix.runtime.engine import Engine
from tracefix.runtime.smoke import PNG, make_engine
from tracefix.runtime.tool_handlers import build_runtime_tools


def suite_fixture(*, rules=(), mode='test', goal='页面应显示任务板且保存后出现反馈'):
    engine = object.__new__(Engine)
    state = RunState(scope_id='frontend', goal=goal, url='http://app:3000',
                     mode=mode, phase=Phase.EXPLORE, source_manifest='source',
                     source_aligned=True, check_suite_stage='explore')
    spec = TestSpec(goal=goal,
                    assertions=[Assertion(locator=Locator(role='heading', name='Task board'))],
                    regression_assertions=[Assertion(locator=Locator(role='heading', name='Task board'))])
    state.test_spec_ref = 'spec.json'
    state.test_spec_hash = digest(spec)
    records = {'spec.json': spec.model_dump(mode='json')}
    events = []

    def put(current, value, ext='json', **options):
        reference = f'evidence-{len(records)}.{ext}'
        records[reference] = deepcopy(value)
        return reference

    engine.put = put
    engine.get = lambda current, reference: deepcopy(records[reference])
    engine.changed = lambda current, **delta: reduce_state(current, current.revision, **delta)
    engine.event = lambda current, kind, payload=None: events.append((kind, payload or {}))
    engine.rule_library = None
    engine.workspace = SimpleNamespace(files=lambda: ['src/page.tsx'],
        read=lambda path: 'export const Page = () => <h1>Task board</h1>;')
    engine.artifacts = SimpleNamespace(read=lambda *arguments: PNG,
                                      exists=lambda *arguments: True)
    engine.phase_observations = lambda current, phase: []
    engine.verification_context = lambda current: {'plan_hash': digest([])}
    engine.store = SimpleNamespace(trace=lambda *arguments: [], save=lambda current: None)
    state.observation_ref = put(state, {
        'id': 'obs-current', 'type': 'gui_observation', 'url': state.url,
        'snapshot': '- heading "Task board" [ref=e1]\n- button "Save" [ref=e2]',
        'console': '[{"level":"error","message":"save failed"}]', 'network': '',
        'screenshot_ref': 'page.png', 'collection': {'channels': {
            'snapshot': 'available', 'console': 'available', 'network': 'available'}},
    })
    plan = make_check_plan(state, list(rules), GoalCheckDraft(
        name='任务板与保存反馈检查', criteria=goal))
    state.check_plan_ref = put(state, plan.model_dump(mode='json'))
    state.check_plan_hash = digest(plan)
    model_calls = []

    async def model_call(current, schema, context, image=None, **options):
        assert schema is CheckJudgement
        model_calls.append({'context': context, 'image': image})
        return CheckJudgement(status='pass', actual='页面与已有证据符合指标',
                              evidence_refs=[current.observation_ref])

    async def freeze_plan(current):
        return engine.changed(current, reproduction_plan_frozen=True,
                              replay_plan_ref=current.replay_plan_ref or put(current, []))

    engine.model_call = model_call
    engine.freeze_reproduction_plan = freeze_plan
    return engine, state, records, events, model_calls


async def finish_suite(engine, state):
    for _ in range(len(engine.check_plan(state).items) + 2):
        output = await engine.check_suite(state, None)
        state = RunState.model_validate(output['data'])
        if state.check_suite_completed:
            return state
    raise AssertionError('检查套件未完整执行')


def console_rule():
    return Rule(id='console-blocker', name='Console 不得出现错误', status='enabled',
                severity='blocker', detection=RuleDetection(type='oracle',
                    oracle={'kind': 'console_no_error'}))


async def test_blocker_failure_does_not_skip_other_rules_or_user_goal():
    rules = [console_rule(),
             Rule(id='source-check', name='源码不得使用危险调用', status='enabled',
                  severity='minor', detection=RuleDetection(type='static',
                      static={'regex': r'forbiddenCall\('})),
             Rule(id='feedback-check', name='检查保存反馈', status='enabled',
                  severity='major', detection=RuleDetection(type='guided',
                      guided={'instruction': '确认保存反馈'}))]
    engine, state, records, events, model_calls = suite_fixture(rules=rules)
    finished = await finish_suite(engine, state)
    results = [records[reference] for reference in finished.check_result_refs]
    assert [item['id'] for item in results] == [item.id for item in engine.check_plan(finished).items]
    assert results[0]['status'] == 'fail'
    assert all(item['status'] == 'pass' for item in results[1:])
    assert finished.overall_status == 'FAILED'
    assert finished.check_summary['coverage_complete'] is True
    assert finished.phase == Phase.FINALIZE
    assert finished.reproduced is False
    assert len([event for event in events if event[0] == 'check.completed']) == 4


async def test_source_read_failure_still_uses_model_with_screenshot():
    rule = Rule(id='ast-rule', name='前端可操作元素名称检查', status='enabled',
                severity='blocker', detection=RuleDetection(type='static',
                    static={'engine': 'ast', 'check': 'a11y_name'}))
    engine, state, records, events, model_calls = suite_fixture(rules=[rule])

    def unavailable_source(path):
        raise OSError('source adapter unavailable')

    engine.workspace.read = unavailable_source
    item = engine.check_plan(state).items[0]
    result = await engine.evaluate_check_item(state, item)
    assert result.status == 'pass'
    assert result.fallback is not None and result.fallback['attempted'] is True
    assert result.fallback['source'] == 'multimodal'
    assert model_calls
    assert model_calls[-1]['image'] == PNG
    assert 'source adapter unavailable' in model_calls[-1]['context']['fallback_reason']


async def test_user_goal_additional_criterion_cannot_pass_only_from_main_dom_assertion():
    goal = '页面应显示任务板，保存后必须出现成功反馈且提示不能遮挡操作按钮'
    engine, state, records, events, model_calls = suite_fixture(goal=goal)

    async def model_call(current, schema, context, image=None, **options):
        assert schema is CheckJudgement
        assert context['check_item']['criteria'] == goal
        model_calls.append({'context': context, 'image': image})
        return CheckJudgement(status='fail', actual='任务板可见，但成功提示遮挡了保存按钮',
                              evidence_refs=[current.observation_ref])

    engine.model_call = model_call
    finished = await finish_suite(engine, state)
    goal_result = records[finished.check_result_refs[0]]
    assert model_calls
    assert goal_result['status'] == 'fail'
    assert '遮挡' in goal_result['actual']
    assert finished.overall_status == 'FAILED'
    assert finished.phase == Phase.FINALIZE
    assert finished.reproduced is False


async def test_repair_rule_failure_enters_diagnosis_without_claiming_reproduction():
    engine, state, records, events, model_calls = suite_fixture(rules=[console_rule()], mode='repair')
    state.replay_plan_ref = engine.put(state, [BrowserAction(kind='observe').model_dump(mode='json')])
    finished = await finish_suite(engine, state)
    results = [records[reference] for reference in finished.check_result_refs]
    assert results[0]['status'] == 'fail'
    assert results[1]['source'] == 'user_goal' and results[1]['status'] == 'pass'
    assert finished.phase == Phase.DIAGNOSE
    assert finished.reproduced is False
    assert finished.reproduction_plan_frozen is True
    assert finished.trial == 0
    assert finished.initial_check_result_refs == finished.check_result_refs


async def test_goal_runs_second_behavior_scenario_after_first_assertion_failure():
    engine, state, records, events, model_calls = suite_fixture()
    spec = engine.spec(state).model_copy(update={'behavior_scenarios': [
        BehaviorScenario(id='first', steps=[BehaviorStep(action=BrowserAction(kind='observe'),
            assertions=[Assertion(locator=Locator(role='heading', name='Missing heading'))])]),
        BehaviorScenario(id='second', steps=[BehaviorStep(action=BrowserAction(kind='observe'),
            assertions=[Assertion(locator=Locator(role='heading', name='Task board'))])]),
    ]})
    records[state.test_spec_ref] = spec.model_dump(mode='json')
    state.test_spec_hash = digest(spec)
    resets = []
    actions = []

    async def reset(current):
        resets.append(current.observation_ref)
        return {'observation_ref': current.observation_ref}

    async def act(current, action, *, frozen=False):
        actions.append((action.kind, frozen))
        return current.observation_ref

    engine.reset = reset
    engine.act = act
    result = await engine.evaluate_check_item(state, engine.check_plan(state).items[0])
    details = json.loads(result.actual)
    checkpoints = [item for item in details['deterministic_checks'] if item.get('scenario_id')]
    assert [item['scenario_id'] for item in checkpoints] == ['first', 'second']
    assert [item['passed'] for item in checkpoints] == [False, True]
    assert len(resets) == 2 and actions == [('observe', True), ('observe', True)]
    assert result.status == 'fail'
    assert model_calls[-1]['context']['deterministic_checks'] == details['deterministic_checks']


async def test_main_dom_failure_remains_failure_when_model_check_raises():
    engine, state, records, events, model_calls = suite_fixture()
    records[state.observation_ref]['snapshot'] = '- button "Save" [ref=e2]'

    async def unavailable_model(current, schema, context, image=None, **options):
        raise RuntimeError('model endpoint unavailable')

    engine.model_call = unavailable_model
    result = await engine.evaluate_check_item(state, engine.check_plan(state).items[0])
    details = json.loads(result.actual)
    assert details['deterministic_checks'][0]['passed'] is False
    assert 'model endpoint unavailable' in details['criteria_judgement']
    assert result.status == 'fail'
    assert result.error is None
    assert result.fallback is None


@pytest.mark.parametrize('phase', [Phase.EXPLORE, Phase.DIAGNOSE, Phase.VERIFY])
def test_check_judgement_registry_exposes_only_readonly_check_tools(tmp_path, phase):
    engine, state = make_engine(tmp_path / str(phase))
    state.phase = phase
    engine.rule_resolver = object()
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    expected = {'Read', 'Grep', 'Glob', 'code.analyze',
                'rules.get', 'rules.applicable', 'context.expand'}
    assert {tool.name for tool in runtime.registry.visible(phase)} == expected
    assert set(runtime.handlers) == expected
    assert all(tool.side_effect == 'read' for tool in runtime.registry.visible(phase))
    assert not any(runtime.registry.contains(name) for name in (
        'Bash', 'Write', 'Edit', 'NotebookEdit', 'agent.delegate',
        'BrowserClick', 'BrowserNavigate', 'BrowserSnapshot', 'finish_exploration'))
