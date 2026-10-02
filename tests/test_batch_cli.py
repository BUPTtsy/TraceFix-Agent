import asyncio
import json
from types import SimpleNamespace

import pytest

from tracefix.cli import main as cli
from tracefix.runtime.contracts import Outcome, RunState, RunStatus
from tracefix.storage.store import MemoryStore


@pytest.mark.parametrize('status,outcome,expected_code', [
    (RunStatus.COMPLETED, Outcome.FIX_VERIFIED, 0),
    (RunStatus.FAILED, Outcome.REPAIR_EXHAUSTED, 1),
    (RunStatus.ABNORMAL, Outcome.LOOP_DETECTED, 1),
])
async def test_batch_cli_returns_consumable_json_and_terminal_exit_code(
        monkeypatch, capsys, status, outcome, expected_code):
    state = RunState(scope_id='demo', goal='验证并修复状态持久化', url='http://app:3000',
                     execution_mode='batch', run_status=status, outcome=outcome,
                     report_ref='report.json')
    report = {'patch_diff_ref': 'candidate.diff', 'patch_available': True,
              'patch_verification': 'verified' if expected_code == 0 else 'unverified',
              'result_summary': '已生成最终结果'}

    class CompletedSession:
        def __init__(self, args, store, saver):
            self.task = None
            self.run_id = state.run_id
            self.artifacts = SimpleNamespace(json=lambda *args: report)

        async def create(self):
            self.task = asyncio.create_task(asyncio.sleep(0))

        def state(self):
            return state

    monkeypatch.setattr(cli, 'Session', CompletedSession)
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    exit_code = await cli.application(SimpleNamespace(run=True, continue_run=None, command=None))
    result_line = next(line for line in capsys.readouterr().out.splitlines()
                       if line.startswith('BATCH_RESULT: '))
    result = json.loads(result_line.removeprefix('BATCH_RESULT: '))
    assert exit_code == expected_code
    assert result['run_status'] == status
    assert result['outcome'] == outcome
    assert result['report_ref'] == 'report.json'
    assert result['patch_diff_ref'] == 'candidate.diff'
    assert result['patch_verification'] == report['patch_verification']
    assert result['result_summary'] == '已生成最终结果'


async def test_batch_drive_does_not_display_pause_for_terminal_error():
    state = RunState(scope_id='demo', goal='验证并修复状态持久化', url='http://app:3000',
                     execution_mode='batch', run_status=RunStatus.FAILED,
                     outcome=Outcome.INFRA_FAILURE, report_ref='report.json')
    displayed = []

    async def fail_after_final_report(*args):
        raise RuntimeError('最终日志暂时不可用')

    session = cli.Session.__new__(cli.Session)
    session.engine = SimpleNamespace(run=fail_after_final_report)
    session.state = lambda: state
    session.publish_console = lambda **kwargs: None
    session.render = SimpleNamespace(status=lambda current: displayed.append(current.run_status))
    await session.drive(state)
    assert displayed == [RunStatus.FAILED]


@pytest.mark.parametrize('noninteractive,requested_mode,record_mode,previous_mode,expected_mode', [
    (True, None, 'interactive', 'interactive', 'batch'),
    (True, 'batch', 'interactive', 'interactive', 'batch'),
    (True, 'interactive', 'batch', 'batch', 'interactive'),
    (True, None, None, 'interactive', 'batch'),
    (False, None, 'interactive', 'interactive', 'interactive'),
    (False, None, 'batch', 'batch', 'batch'),
    (False, None, None, 'batch', 'batch'),
    (False, 'batch', 'interactive', 'interactive', 'batch'),
    (False, 'interactive', 'batch', 'batch', 'interactive'),
])
async def test_continuation_uses_requested_or_entrypoint_execution_mode(
        cli_session, monkeypatch, noninteractive, requested_mode, record_mode,
        previous_mode, expected_mode):
    monkeypatch.delenv('TRACEFIX_CONTINUATION_ID', raising=False)
    cli_session.args.execution_mode = requested_mode
    cli_session.args.continue_run = 'previous_task' if noninteractive else None
    continued_session = cli.Session(cli_session.args, MemoryStore(), None)
    previous = RunState(scope_id=continued_session.scope, goal='继续验证状态持久化',
                        url='http://app:3000', mode='repair', execution_mode=previous_mode,
                        run_status=RunStatus.FAILED, outcome=Outcome.INFRA_FAILURE)
    continued_session.store.save(previous)
    fields = {'projectId': previous.scope_id, 'goal': previous.goal,
              'mode': previous.mode, 'status': 'failed', 'outcome': str(previous.outcome),
              'agentRunId': previous.run_id}
    if record_mode is not None:
        fields['executionMode'] = record_mode
    record = continued_session.documents.update_run('previous_task', fields, create=True)
    launched = []

    async def capture_agent(state=None, reuse_workspace=False):
        launched.append((state, reuse_workspace))

    monkeypatch.setattr(continued_session, 'create_agent', capture_agent)
    await continued_session.continue_task(record['id'], '检查环境后继续原任务')

    assert len(launched) == 1
    continued, reuse_workspace = launched[0]
    assert continued.execution_mode == continued_session.execution_mode == expected_mode
    assert continued.run_id == previous.run_id
    assert continued.continuation_count == 1 and reuse_workspace
    assert continued.run_status == RunStatus.RUNNING
    assert continued_session.store.load(previous.run_id, previous.scope_id).execution_mode == previous_mode
