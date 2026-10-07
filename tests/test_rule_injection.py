import pytest

from tracefix.model.gateway import ModelOutputError, ModelResult
from tracefix.rules import Rule, RuleLibrary, RuleResolver, evaluate_oracle, render_rule_context
from tracefix.rules.builtins import builtin_rules
from tracefix.runtime.contracts import BrowserAction, Decision, Phase
from tracefix.runtime.smoke import make_engine


def guided(rule_id, **fields):
    return Rule(id=rule_id, name=rule_id, status='enabled',
                detection={'type': 'guided', 'guided': {'prompt': '检查必需的反馈并记录证据'}}, **fields)


def test_child_and_grandchild_keep_frozen_versions_when_same_id_is_added(tmp_path):
    library = RuleLibrary(tmp_path / 'console.sqlite3')
    original = library.save_rule(guided('parent_rule'))
    resolver = RuleResolver(library)
    parent, _ = resolver.resolve_snapshot(run_id='parent', project_id='b', phase='*')
    library.save_snapshot(parent)
    library.save_rule(original.model_copy(update={'name': '新的检测要求'}), original.id)
    extra = library.save_rule(guided('extra_rule'))
    child, rules = resolver.resolve_snapshot(run_id='child', project_id='b', phase='*',
                                            parent=parent, additional=[original.id, extra.id])
    inherited = next(rule for rule in rules if rule.id == original.id)
    assert inherited.version == original.version and inherited.name == original.name
    grandchild, _ = resolver.resolve_snapshot(run_id='grandchild', project_id='b', phase='*', parent=child)
    assert grandchild.refs == child.refs
    assert {extra.id, original.id}.issubset({ref.id for ref in grandchild.refs})


def test_rules_keep_complete_guidance_when_token_estimate_exceeds_budget():
    rules = [guided(f'rule_{index}', fix_guidance='完整修复要求' * 50) for index in range(8)]
    context = render_rule_context(rules, max_tokens=128)
    assert context['over_budget']
    assert context['full_ids'] == context['rule_ids']
    assert all(item['check']['guided']['prompt'] and item['fix_guidance'] for item in context['items'])
    assert '不得自行忽略' in context['prompt']


async def test_injection_tracks_phase_url_files_and_frozen_version(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.rule_library = RuleLibrary(tmp_path / 'console.sqlite3')
    rule = engine.rule_library.save_rule(guided('checkout_rule', phases=['EXPLORE'],
        scope={'level': 'project', 'project_ids': ['b'], 'url_patterns': ['/checkout*']}))
    engine.rule_library.save_rule(guided('code_rule', phases=['DIAGNOSE'], scope={'path_globs': ['src/value.ts']}))
    engine.rule_resolver = RuleResolver(engine.rule_library)
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    assert rule.id in {ref['id'] for ref in state.rule_refs}
    engine.rule_library.save_rule(rule.model_copy(update={'status': 'disabled', 'name': '新版'}), rule.id)
    state.phase = Phase.EXPLORE
    contexts = []

    class RecordingModel:
        async def generate(self, schema, context, **options):
            contexts.append(context)
            return ModelResult(Decision(action=BrowserAction(kind='finish')), {}, 'fixture', 'stop')

    engine.model = RecordingModel()
    await engine.model_call(state, Decision, {'observation': {'url': 'http://app:3000/home'}})
    await engine.model_call(state, Decision, {'observation': {'url': 'http://app:3000/checkout'}})
    state.phase = Phase.DIAGNOSE
    await engine.model_call(state, Decision, {'cards': [{'path': 'src/other.ts'}]})
    await engine.model_call(state, Decision, {'cards': [{'path': 'src/value.ts'}]})
    assert rule.id not in contexts[0]['detection_rules']['rule_ids']
    selected = next(item for item in contexts[1]['detection_rules']['items'] if item['id'] == rule.id)
    assert selected['version'] == 1 and selected['name'] == rule.name
    assert 'code_rule' not in contexts[2]['detection_rules']['rule_ids']
    assert 'code_rule' in contexts[3]['detection_rules']['rule_ids']
    assert len([event for event in engine.store.trace(state.run_id, state.scope_id) if event['type'] == 'rules.injected']) == 4

async def test_model_cannot_reference_rule_not_in_current_injection(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.rule_resolver = RuleResolver([guided('checkout_rule', scope={'url_patterns': ['/checkout*']})])
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    state.phase = Phase.EXPLORE

    class InvalidModel:
        async def generate(self, schema, context, **options):
            return ModelResult(Decision(action=BrowserAction(kind='finish'), rule_refs=['checkout_rule']), {}, 'fixture', 'stop')

    engine.model = InvalidModel()
    with pytest.raises(ModelOutputError, match='动态注入'):
        await engine.model_call(state, Decision, {})


async def test_oracle_accepts_browser_action_and_does_not_trigger_without_action(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.rule_library = RuleLibrary(tmp_path / 'console.sqlite3')
    engine.rule_resolver = RuleResolver(engine.rule_library)
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    state.phase = Phase.EXPLORE
    observation = await engine.browser.action(BrowserAction(kind='observe'))
    await engine.capture(state, dict(observation))
    assert engine.rule_library.findings() == []
    state.pending_action = BrowserAction(kind='click', locator={'role': 'button', 'name': '保存'})
    await engine.capture(state, dict(observation))
    assert engine.rule_library.findings(rule_id='builtin_submit_feedback')
    assert {rule.id for rule in builtin_rules()}.issubset({ref['id'] for ref in state.rule_refs})


def test_network_oracle_reads_real_mcp_text_requests():
    rule = next(rule for rule in builtin_rules() if rule.id == 'builtin_api_no_5xx')
    findings = evaluate_oracle(rule, {'network': '[GET] http://app:3000/api/items => [500] Internal Server Error'},
                              job_id='run', run_id='run')
    assert len(findings) == 1
    assert findings[0].location['url'] == 'http://app:3000/api/items'
