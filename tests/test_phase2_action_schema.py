import pytest

from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import Assertion, BrowserAction, Decision, Locator, ObservableWait
from tracefix.runtime.engine import Engine
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.tool_handlers import build_runtime_tools
from tracefix.runtime.tools import ToolRejected


def test_native_browser_actions_preserve_optional_execution_conditions():
    target = Locator(role='checkbox', name='Complete task')
    action = BrowserAction(kind='click', observation_id='latest', element_ref='current',
        page_generation=7, locator=target,
        preconditions=[Assertion(locator=target, condition='unchecked')],
        postconditions=[Assertion(locator=target, condition='checked')],
        wait=ObservableWait(assertions=[Assertion(locator=target, condition='checked')]))
    arguments = action.model_dump(exclude_none=True)
    arguments.pop('kind')

    assert Gateway.browser_action('BrowserClick', arguments) == action
    schema = next(tool['function']['parameters'] for tool in Gateway._native_tools(Decision)
                  if tool['function']['name'] == 'BrowserClick')
    assert schema['required'] == ['observation_id', 'element_ref', 'locator']


def test_native_browser_action_remains_compatible_and_rejects_unknown_fields():
    arguments = {'observation_id': 'latest', 'element_ref': 'current',
                 'locator': {'role': 'button', 'name': 'Save'}}
    assert Gateway.browser_action('BrowserClick', arguments).preconditions == []
    with pytest.raises(ValueError, match='工具参数校验失败'):
        Gateway.browser_action('BrowserClick', {**arguments, 'unexpected': True})


def test_bounded_projection_reports_exact_omitted_lines_around_middle_target():
    lines = [f'- text "row-{index}"' for index in range(100)]
    lines[50] = '- checkbox "Complete task" [ref=middle]'
    snapshot = '\n'.join(lines)
    text, omitted = Engine._bounded_text(snapshot, 500,
                                         [Locator(role='checkbox', name='Complete task')])

    assert 'Complete task' in text
    assert len(text) <= 500
    assert omitted == [{'start': 2, 'end': 49}, {'start': 53, 'end': 99}]


async def test_single_mode_disables_registered_and_inflight_model_delegation(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    monkeypatch.setenv('TRACEFIX_AGENT_MODE', 'multi')
    tools = build_runtime_tools(engine, state, Decision)
    assert 'agent.delegate' in tools.handlers

    monkeypatch.setenv('TRACEFIX_AGENT_MODE', 'single')
    single_tools = build_runtime_tools(engine, state, Decision)
    assert 'agent.delegate' not in single_tools.handlers
    with pytest.raises(ToolRejected, match='single'):
        await tools.handlers['agent.delegate'](None, 'stale-delegation')
    monkeypatch.setenv('TRACEFIX_BROWSER_WORKERS', '1')
    engine.store.save(state)
    await engine.discover(state)
    assert engine.store.trace(state.run_id, state.scope_id)[-1]['type'] == 'subtask.discover.disabled'
    assert not hasattr(engine, 'subagent_runtime')
