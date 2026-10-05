import asyncio
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.cells import cell_len

from tracefix.cli.input import make_prompt
from tracefix.cli.main import Session
from tracefix.cli.render import Renderer
from tracefix.runtime.contracts import RunState, RunStatus
from tracefix.runtime.event_adapter import EventBatch
from tracefix.runtime.smoke import make_engine


def renderer_output(*, width=80, plain=False, terminal=False):
    renderer = Renderer(plain=plain)
    output = io.StringIO()
    renderer.console = Console(file=output, width=width, force_terminal=terminal,
        no_color=renderer.plain, markup=False, highlight=False, record=True)
    return renderer, output


def event(kind, payload=None, phase='EXPLORE'):
    return {'seq': 1, 'type': kind, 'phase': phase, 'payload': payload or {}}


@pytest.mark.parametrize('status', [RunStatus.PAUSED, RunStatus.WAITING_APPROVAL,
    RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED])
def test_renderer_terminal_states_stop_activity_and_keep_correct_status(status):
    renderer, output = renderer_output(plain=True)
    renderer.event(event('run.started'))
    renderer.event(event('model.started', {'model': 'demo-model'}))
    renderer.event(event('state.changed', {'status': status}))
    assert renderer.run_status == status
    assert not renderer.activity
    assert renderer.finished_at is not None
    assert not any(marker in str(renderer.toolbar('demo', 'repair')) for marker in '◐◓◑◒')
    assert '\x1b' not in output.getvalue()


def test_renderer_preserves_paused_status_from_unknown_operation_error():
    renderer, output = renderer_output(plain=True)
    renderer.event(event('run.error', {'status': RunStatus.PAUSED,
                                       'error_details': {'status': 'UNKNOWN_OPERATION'}}))
    assert renderer.run_status == RunStatus.PAUSED
    assert 'FAILED' not in output.getvalue()


async def test_trace_dispatch_does_not_change_live_run_or_activity():
    renderer, output = renderer_output(plain=True)
    renderer.event(event('run.started'))
    renderer.event(event('model.started', {'model': 'current-model'}))
    snapshot = {key: value.copy() if isinstance(value, set) else value
                for key, value in vars(renderer).items() if key != 'console'}
    history = [event('model.started', {'model': 'old-model'}),
        event('model.usage', {'usage': {'total_tokens': 1000}}),
        event('state.changed', {'status': 'COMPLETED'}, 'FINALIZE')]
    session = Session.__new__(Session)
    session.render, session.run_id, session.scope = renderer, 'run_test', 'demo'
    state = SimpleNamespace(run_id=session.run_id, scope_id=session.scope)
    session.store = SimpleNamespace(load=Mock(return_value=state))
    session.engine = SimpleNamespace(read_events=Mock(return_value=EventBatch(
        events=history, cursor='test-cursor', high_watermark=len(history))))
    await session.dispatch('/trace')
    session.store.load.assert_called_once_with(session.run_id, session.scope)
    session.engine.read_events.assert_called_once_with(state, None)
    assert {key: value for key, value in vars(renderer).items() if key != 'console'} == snapshot
    assert 'old-model' in output.getvalue()


async def test_fake_engine_lifecycle_reaches_approval_then_rejection_without_live_activity(tmp_path):
    engine, state = make_engine(tmp_path)
    renderer, output = renderer_output(plain=True)
    engine.notify = renderer.event
    renderer.status(state)
    await engine.run(state)
    waiting = engine.store.load(state.run_id, state.scope_id)
    renderer.status(waiting)
    assert renderer.run_status == RunStatus.WAITING_APPROVAL
    assert not renderer.activity
    assert f'/approve {waiting.approval_ref}' in output.getvalue()
    assert 'sandbox.start' in output.getvalue()
    engine.store.decide_approval(waiting.approval_ref, waiting, 'reject')
    await engine.run(resume='reject')
    final = engine.store.load(state.run_id, state.scope_id)
    renderer.status(final)
    assert renderer.run_status == RunStatus.COMPLETED
    assert not renderer.activity
    assert renderer.tokens == final.budget.tokens


def test_status_switches_budget_to_current_run():
    renderer, output = renderer_output(plain=True)
    old = RunState(scope_id='demo', goal='old', url='http://app:3000')
    old.budget.tokens = 3000
    renderer.status(old)
    current = RunState(scope_id='demo', goal='new', url='http://app:3000')
    current.budget.tokens = 20
    renderer.status(current)
    assert renderer.run_id == current.run_id
    assert renderer.tokens == 20
    assert '3,000' not in str(renderer.toolbar('demo', 'repair'))


@pytest.mark.parametrize('width', [24, 40, 80])
def test_narrow_terminal_rendering_wraps_chinese_and_preserves_literal_text(width):
    renderer, output = renderer_output(width=width, terminal=True)
    renderer.welcome('中文项目 [bold]', 'repair', 'demo-model', preview=True)
    renderer.event(event('tool.started', {'operation_id': 'run:1:browser',
        'intent': {'value': '[bold]literal[/bold] 中文路径/' * 8}}))
    exported = renderer.console.export_text()
    assert '[bold]' in exported
    assert all(cell_len(line) <= width for line in exported.splitlines())


def test_renderer_sanitizes_untrusted_fields_and_secret_values():
    renderer, output = renderer_output(plain=True)
    renderer.panel('\x1b[31m标题\x1b[0m', '中文 \x1b]0;injected\x07 token=private-token')
    renderer.event(event('tool.started', {'operation_id': 'run:1:browser',
        'intent': {'Authorization': 'Bearer private-auth', 'Cookie': 'private-cookie',
                   'value': '\x1b[2J中文\x85field'}}))
    text = output.getvalue()
    assert '中文' in text
    assert 'private-' not in text
    assert '\x1b' not in text and '\x85' not in text
    assert 'injected' not in text


@pytest.mark.parametrize('setting', ['plain', 'NO_COLOR', 'TERM'])
def test_plain_and_environment_modes_disable_dynamic_terminal_controls(monkeypatch, setting):
    if setting == 'NO_COLOR':
        monkeypatch.setenv('NO_COLOR', '')
    elif setting == 'TERM':
        monkeypatch.setenv('TERM', 'dumb')
    renderer, output = renderer_output(plain=setting == 'plain', terminal=True)
    renderer.welcome('demo', 'repair')
    renderer.event(event('run.started'))
    renderer.diff('--- before\n+++ after\n+new')
    assert '\x1b' not in output.getvalue()
    assert renderer.plain


async def test_prompt_preserves_multiline_input_during_async_event_output():
    renderer, output = renderer_output()
    with create_pipe_input() as pipe:
        prompt = make_prompt(renderer, lambda: 'demo', lambda: 'repair',
                             input=pipe, output=DummyOutput())
        task = asyncio.create_task(prompt.prompt_async())
        await asyncio.sleep(0.05)
        pipe.send_text('中文目标')
        await asyncio.sleep(0.05)
        await run_in_terminal(lambda: renderer.event(event('model.started', {'model': 'demo'})))
        assert prompt.default_buffer.text == '中文目标'
        pipe.send_text('\x1b\r第二行\r')
        assert await asyncio.wait_for(task, 2) == '中文目标\n第二行'
        assert not prompt.app.is_running


async def test_prompt_completion_and_keyboard_interrupt():
    renderer, output = renderer_output()
    with create_pipe_input() as pipe:
        prompt = make_prompt(renderer, lambda: 'demo', lambda: 'repair',
                             input=pipe, output=DummyOutput())
        task = asyncio.create_task(prompt.prompt_async())
        await asyncio.sleep(0.05)
        pipe.send_text('/sta\t')
        await asyncio.sleep(0.1)
        pipe.send_text('\r\r')
        assert await asyncio.wait_for(task, 2) == '/status'
        async def expect_keyboard_interrupt():
            with pytest.raises(KeyboardInterrupt):
                await prompt.prompt_async()

        interrupted = asyncio.create_task(expect_keyboard_interrupt())
        await asyncio.sleep(0.05)
        pipe.send_text('\x03')
        await asyncio.wait_for(interrupted, 2)
        assert not prompt.app.is_running


def test_preview_cli_needs_no_configuration_or_backend(tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('TRACEFIX_')}
    environment['PYTHONPATH'] = str(root / 'backend/packages/agent/src')
    environment['PYTHONUTF8'] = '1'
    result = subprocess.run([sys.executable, '-m', 'tracefix.cli.main', '--preview', '--plain',
        '--projects', 'missing-projects.yaml', '--profile', 'missing-profile.yaml'],
        cwd=tmp_path, env=environment, text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert '演示数据' in result.stdout and '审批尚未提交' in result.stdout
    assert '\x1b' not in result.stdout
    assert not list(tmp_path.iterdir())
