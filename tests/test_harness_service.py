"""服务边界的 SDK 子 Run、暂停清理和恢复回归。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus, digest
from tracefix.runtime.harness_contracts import HarnessJob
from tracefix.service import run
from tracefix.service.resources import RunServices
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import MemoryHarnessStore
from tracefix.storage.store import MemoryStore

from test_service_runtime import service_fixture


def authority_job(artifacts, *, status='STOPPED', scope='alpha', task='task_alpha'):
    authority = MemoryHarnessStore(artifacts)
    job = authority.create_job(HarnessJob(job_id='sdk-job', scope_id=scope, repo_id='owner/app',
        console_task_id=task, goal_hash=digest('检查页面操作结果'), origin_base_commit='a' * 40,
        acceptance_refs=['spec']))
    authority.jobs[job.job_id].status = status
    runtime = SimpleNamespace(store=authority, job=lambda state: authority.get_job(
        state.harness_job_id, scope_id=state.scope_id))
    return authority, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_failed', [False, True])
async def test_sdk_pause_returns_after_cleanup_and_never_terminalizes(service_fixture, monkeypatch, cleanup_failed):
    fixture = service_fixture
    terminalize = AsyncMock(side_effect=AssertionError('SDK 不能 terminal_failure'))
    publish = Mock(side_effect=AssertionError('SDK 不能 publication'))
    monkeypatch.setattr(run, 'terminal_failure', terminalize)
    monkeypatch.setattr(run, 'prepare_publication', publish)

    class Services(RunServices):
        async def aclose(self):
            await super().aclose()
            if cleanup_failed:
                self.cleanup_errors.append({'code': 'controlled_close_failure'})

    async def drive(session, state):
        authority, runtime = authority_job(session.artifacts)
        session.engine.harness = runtime
        child = state.model_copy(deep=True, update={'run_id': 'sdk-current',
            'harness_job_id': 'sdk-job', 'harness_session_id': 'sdk-session',
            'harness_stage': 'coding', 'run_status': RunStatus.PAUSED, 'phase': Phase.DIAGNOSE})
        session.store.save(child)
        session.state = lambda: session.store.load(child.run_id, child.scope_id)

    monkeypatch.setattr(fixture.factory, 'drive', drive)
    result = await run.execute_request(fixture.request(), fixture.settings,
        session_factory=fixture.factory, services_factory=Services)
    assert result['run_id'] == 'run_alpha' and result['current_run_id'] == 'sdk-current'
    assert result['terminal'] is False and result['outcome'] is None
    assert result['control_status'] == ('BLOCKED' if cleanup_failed else 'STOPPED')
    if cleanup_failed:
        assert result['resume_blockers'] == ['CLEANUP_FAILED']
    terminalize.assert_not_called()
    publish.assert_not_called()
    assert fixture.store.load('sdk-current', 'alpha').run_status == 'PAUSED'


@pytest.mark.asyncio
async def test_sdk_cancellation_requests_stop_without_cancelled_terminal(service_fixture, monkeypatch):
    fixture = service_fixture
    started = asyncio.Event()
    monkeypatch.setattr(run, 'terminal_failure', AsyncMock(side_effect=AssertionError('terminal failure')))

    async def drive(session, state):
        authority, runtime = authority_job(session.artifacts, status='RUNNING')
        session.engine.harness = runtime
        state.harness_job_id, state.harness_session_id = 'sdk-job', 'sdk-session'
        session.store.save(state)

        async def stop():
            job = runtime.job(state)
            authority.request_stop(job.job_id, job.revision, scope_id=state.scope_id)

        session.stop = stop
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(fixture.factory, 'drive', drive)
    task = asyncio.create_task(run.execute_request(fixture.request(), fixture.settings,
        session_factory=fixture.factory))
    await started.wait()
    task.cancel()
    result = await task
    assert result['control_status'] == 'STOPPING'
    assert result['terminal'] is False and result['run_status'] != 'CANCELLED'
    assert fixture.store.load('run_alpha', 'alpha').run_status != 'CANCELLED'


def test_sdk_terminal_result_preserves_console_identity_and_child_report(tmp_path):
    artifacts, store = Artifacts(tmp_path / 'artifacts'), MemoryStore()
    state = RunState(run_id='sdk-child', scope_id='alpha', goal='冻结目标', url='http://app.test',
        run_status='COMPLETED', phase=Phase.FINALIZE, outcome=Outcome.NO_BUG_FOUND,
        harness_job_id='sdk-job', harness_session_id='session')
    state.report_ref = artifacts.put('alpha', state.run_id, {'run_id': state.run_id,
        'scope_id': state.scope_id, 'run_status': 'COMPLETED', 'outcome': str(state.outcome),
        'patch_available': False, 'patch_verification': 'none'})
    request = SimpleNamespace(run_id='original-run', task_id='task', repo_id='alpha')
    session = SimpleNamespace(store=store, artifacts=artifacts, engine=SimpleNamespace(workspace=None))
    result = run.sdk_result(request, session, state, SimpleNamespace(status='COMPLETED', job_id='sdk-job'))
    assert result['run_id'] == 'original-run' and result['current_run_id'] == 'sdk-child'
    assert result['report']['run_id'] == 'sdk-child'
    assert result['report_ref'] == state.report_ref and result['terminal']


@pytest.mark.asyncio
async def test_restore_nonfinding_services_uses_current_run_and_session(tmp_path):
    request = SimpleNamespace(run_id='original', repo_id='alpha', github_reads=[])
    state = SimpleNamespace(run_id='current', repair_source=None)
    session = SimpleNamespace(engine=None)
    services = await run.restore_run_services(request, SimpleNamespace(), session, state)
    assert services.run_id == 'current' and services.session is session
    assert await services.reference_documents(SimpleNamespace(run_id='current', scope_id='alpha')) == []
    await services.aclose()


@pytest.mark.asyncio
async def test_resume_rejects_original_run_identity_mismatch(service_fixture, monkeypatch):
    from tracefix.harness_control import HarnessControl
    fixture = service_fixture
    request = fixture.request()
    artifacts = Artifacts(fixture.settings.data_root / 'artifacts')
    authority, _runtime = authority_job(artifacts)
    state = RunState(run_id='current', scope_id='alpha', job_id=request.task_id,
        goal=request.goal, url='http://app:3000', mode=request.mode, harness_job_id='sdk-job')
    authority.list_sessions = lambda *args, **options: [SimpleNamespace(kind='initializer', run_id='other-origin')]
    monkeypatch.setattr(run, 'Session', fixture.factory)
    monkeypatch.setattr('tracefix.storage.harness.PostgresHarnessStore', lambda *args: authority)
    monkeypatch.setattr(HarnessControl, 'state', lambda self, job: state)
    restore = AsyncMock(side_effect=AssertionError('身份不匹配不得开始恢复'))
    monkeypatch.setattr(fixture.factory, 'restore_harness', restore)
    result = await run.execute_resume_request(request, fixture.settings)
    assert result['error']['code'] in {'run_identity_conflict', 'harness_resume_conflict'}
    restore.assert_not_called()


@pytest.mark.asyncio
async def test_resume_cancellation_stays_recoverable_and_rebuilds_services(service_fixture, monkeypatch):
    from tracefix.harness_control import HarnessControl
    fixture = service_fixture
    request = fixture.request()
    artifacts = Artifacts(fixture.settings.data_root / 'artifacts')
    authority, runtime = authority_job(artifacts, status='RUNNING')
    state = RunState(run_id='current', scope_id='alpha', job_id=request.task_id,
        goal=request.goal, url='http://app:3000', mode=request.mode, harness_job_id='sdk-job')
    authority.list_sessions = lambda *args, **options: [SimpleNamespace(kind='initializer', run_id=request.run_id)]
    monkeypatch.setattr(run, 'Session', fixture.factory)
    monkeypatch.setattr('tracefix.storage.harness.PostgresHarnessStore', lambda *args: authority)
    monkeypatch.setattr(HarnessControl, 'state', lambda self, job: state)
    prepared = []

    async def restore(session, current):
        assert session.runtime_services.run_id == current.run_id
        prepared.append(session.runtime_services._prepared)
        session.state = lambda: current
        session.engine = SimpleNamespace(harness=runtime, rule_library=object(),
            browser=SimpleNamespace(close=AsyncMock()), runner=SimpleNamespace(close=AsyncMock()))

        async def interrupted():
            raise asyncio.CancelledError()

        async def stop():
            job = runtime.job(current)
            authority.request_stop(job.job_id, job.revision, scope_id=current.scope_id)

        session.stop = stop
        session.task = asyncio.create_task(interrupted())

    monkeypatch.setattr(fixture.factory, 'restore_harness', restore)
    result = await run.execute_resume_request(request, fixture.settings)
    assert prepared == [True]
    assert result['terminal'] is False and result['control_status'] == 'STOPPING'
    assert result['run_id'] == request.run_id and result['current_run_id'] == state.run_id
    session = fixture.sessions[-1]
    assert session.engine.rule_library.services is session.runtime_services
    assert session.runtime_services._close_task.done()


@pytest.mark.asyncio
async def test_restore_preparation_failure_closes_created_services():
    request = SimpleNamespace(run_id='original', repo_id='alpha', github_reads=[])
    state = SimpleNamespace(run_id='current', repair_source=None)
    session = SimpleNamespace(engine=None)
    services = RunServices(request, SimpleNamespace())
    services.prepare = AsyncMock(side_effect=RuntimeError('controlled_prepare_failure'))
    services.aclose = AsyncMock()
    with pytest.raises(RuntimeError, match='controlled_prepare_failure'):
        await run.restore_run_services(request, SimpleNamespace(), session, state,
            services_factory=lambda *args: services)
    services.aclose.assert_awaited_once()
