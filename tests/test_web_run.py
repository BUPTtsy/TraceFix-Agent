import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tracefix.runtime.contracts import RunStatus
from tracefix.service import web_run


@pytest.mark.parametrize('payload', [
    {'project': 'demo'},
    {'project': 'demo', 'goal': '检查任务', 'mode': 'interactive'},
    {'project': 'demo', 'goal': '检查任务', 'plain': True},
    {'project': 'demo', 'continue_run': 'run_1'},
    {'project': 'demo', 'continue_run': 'run_1', 'instruction': '重试', 'parent_run': 'run_0'},
])
def test_web_worker_rejects_invalid_requests(payload):
    with pytest.raises(ValidationError):
        web_run.WebRunRequest.model_validate(payload)


def test_web_worker_arguments_preserve_execution_inputs(monkeypatch):
    monkeypatch.setenv('TRACEFIX_PROJECTS', 'default-projects.yaml')
    request = web_run.WebRunRequest(project='demo', goal='检查刷新后的状态', mode='repair',
        projects='projects.yaml', profile='profile.yaml', data='data', console_db='console.sqlite3',
        spec='frozen-spec.json', parent_run='source-run', rule=['enabled-rule'])
    arguments = request.arguments()
    for name, expected in request.model_dump().items():
        assert getattr(arguments, name) == expected
    assert arguments.execution_mode == 'batch'


@pytest.fixture
def worker_session(monkeypatch):
    monkeypatch.delenv('TRACEFIX_PROCESS_CONTROL_ID', raising=False)
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    monkeypatch.setattr(web_run.signal, 'signal', lambda *args: None)

    class FakeSession:
        def __init__(self, arguments, *_):
            self.args = arguments
            self.engine, self.run_id = object(), 'agent-run'
            self.task = None
            self.calls = []
            self.closed = False
            self.current = SimpleNamespace(run_id=self.run_id, run_status=RunStatus.COMPLETED,
                outcome=None, report_ref='report.json')

        async def create(self):
            self.runtime_stack.callback(self.close)
            self.calls.append(('create',))
            self.task = asyncio.create_task(asyncio.sleep(0))

        async def continue_task(self, run_id, instruction):
            await self.create()
            self.calls[-1] = ('continue', run_id, instruction)

        async def stop(self):
            self.calls.append(('stop',))

        def state(self):
            return self.current

        def close(self):
            self.closed = True

    sessions = []

    def construct(*args):
        session = FakeSession(*args)
        sessions.append(session)
        return session

    monkeypatch.setattr(web_run, 'Session', construct)
    return FakeSession, sessions


@pytest.mark.parametrize('continue_run', [False, True])
async def test_web_worker_runs_or_continues_and_closes_resources(worker_session, capsys, continue_run):
    _, sessions = worker_session
    values = {'project': 'demo', 'goal': '检查任务'}
    if continue_run:
        values.update(continue_run='previous-run', instruction='重新检查')
    assert await web_run.application(web_run.WebRunRequest(**values)) == 0
    session = sessions[0]
    assert session.calls == ([('continue', 'previous-run', '重新检查')] if continue_run else [('create',)])
    assert session.closed and session.task.done()
    result = json.loads(capsys.readouterr().out)
    assert result['run_id'] == 'agent-run' and result['report_ref'] == 'report.json'


@pytest.mark.parametrize('status,exit_code', [
    (RunStatus.COMPLETED, 0), (RunStatus.WAITING_APPROVAL, 0), (RunStatus.FAILED, 1),
])
async def test_web_worker_exit_code_reflects_final_run_state(worker_session, monkeypatch, status, exit_code):
    session_type, sessions = worker_session
    create = session_type.create

    async def create_with_status(self):
        await create(self)
        self.current.run_status = status

    monkeypatch.setattr(session_type, 'create', create_with_status)
    assert await web_run.application(web_run.WebRunRequest(project='demo', goal='检查任务')) == exit_code
    assert sessions[0].closed


async def test_web_worker_cancellation_stops_run_and_closes_resources(worker_session, monkeypatch):
    session_type, sessions = worker_session
    create = session_type.create

    async def interrupted_create(self):
        await create(self)
        raise asyncio.CancelledError

    monkeypatch.setattr(session_type, 'create', interrupted_create)
    assert await web_run.application(web_run.WebRunRequest(project='demo', goal='检查任务')) == 130
    assert sessions[0].calls[-1] == ('stop',)
    assert sessions[0].closed and sessions[0].task.done()


async def test_web_worker_stop_request_requires_run_project_and_process_identity():
    matching = {'id': 'console-run', 'projectId': 'demo', 'processControlId': 'process-1',
                'stopRequestedAt': '2026-10-24T12:00:00Z'}
    records = [{**matching, 'id': 'other-run'}, {**matching, 'projectId': 'other-project'},
               {**matching, 'processControlId': 'old-process'}, matching]
    visited, stopped = [], []

    def run(run_id):
        visited.append(run_id)
        return records.pop(0)

    async def stop():
        stopped.append(True)

    session = SimpleNamespace(documents=SimpleNamespace(run=run), scope='demo', engine=object(),
                              run_id='agent-run', stop=stop)
    await asyncio.wait_for(web_run.watch_stop_requests(session, 'console-run', 'process-1', interval=0), 2)
    assert visited == ['console-run'] * 4 and stopped == [True]


@pytest.mark.parametrize('level', ['hint', 'retarget'])
async def test_paused_worker_resumes_queued_guidance_and_confirmed_retarget(monkeypatch, level):
    monkeypatch.setenv('TRACEFIX_CONSOLE_RUN_ID', 'console-run')
    current = SimpleNamespace(run_status=RunStatus.PAUSED, guidance=[])
    entry = SimpleNamespace(id='guidance-1', level=level, status='queued', confirmed_at=None)
    checks, resumes = [], []

    def guidance_status(state):
        checks.append(state)
        if len(checks) > 1:
            entry.confirmed_at = 1.0
        return [entry]

    async def drive(*, resume):
        resumes.append(resume)
        current.run_status = RunStatus.COMPLETED

    session = SimpleNamespace(task=asyncio.create_task(asyncio.sleep(0)), state=lambda: current,
        engine=SimpleNamespace(guidance_status=guidance_status), drive=drive)
    await asyncio.wait_for(web_run.wait_for_run(session, interval=0), 2)
    assert resumes == [('retarget' if level == 'retarget' else 'resume')]
    assert len(checks) == (2 if level == 'retarget' else 1)


@pytest.mark.parametrize('raw', [b'{invalid', b'x' * (web_run.MAX_REQUEST_BYTES + 1)],
                         ids=['malformed-json', 'oversized-json'])
def test_web_worker_main_reports_malformed_or_oversized_request(monkeypatch, raw):
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(web_run.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(web_run.sys, 'stdout', output)
    monkeypatch.setattr(web_run.sys, 'stderr', errors)
    assert web_run.main() == 2
    assert output.getvalue() == ''
    assert json.loads(errors.getvalue())['error']
