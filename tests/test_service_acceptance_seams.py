import asyncio
from types import SimpleNamespace

import pytest

from tracefix.knowledge.documents import DocumentLibrary
from tracefix.runtime.contracts import Outcome
from tracefix.service.finding import strict_acceptance_adapter, validate_acceptance
from tracefix.service.resources import RunServices
from tracefix.service.run import execute_request, watch_service_stop
from tests.test_finding_acceptance import acceptance_fixture
from tests.test_service_runtime import service_fixture


def test_typed_acceptance_freezes_without_success_refs(acceptance_fixture):
    state, payload, _ = acceptance_fixture
    request = SimpleNamespace(run_id=state.run_id, repo_id=state.scope_id,
        source_finding_ids=['selected'], finding_acceptance=payload,
        repository_snapshot=SimpleNamespace(commit='a' * 40,
            model_dump=lambda **kwargs: {'commit': 'a' * 40}))
    accepted = strict_acceptance_adapter(request, state, {'commit': 'a' * 40})
    assert accepted.frozen and not accepted.reproduction_ref and not accepted.verification_ref
    assert validate_acceptance(accepted, request, state) is accepted
    accepted.reproduction_ref = 'user-success.json'
    with pytest.raises(Exception, match='不能继承'):
        validate_acceptance(accepted, request, state)


@pytest.mark.parametrize('field,value', [
    ('processControlId', 'another-process'), ('taskId', 'another-task'), ('projectId', 'another-repo'),
    ('id', 'another-run'), ('status', 'running'),
])
async def test_stop_watcher_ignores_other_run_task_or_process(field, value):
    request = SimpleNamespace(run_id='run', repo_id='repo', task_id='task')
    record = {'id': 'run', 'projectId': 'repo', 'taskId': 'task', 'processControlId': 'process',
        'status': 'stopping', 'stopRequestedAt': 'requested'}
    record[field] = value
    documents = SimpleNamespace(run=lambda run_id: dict(record))
    target = asyncio.create_task(asyncio.Event().wait())
    watcher = asyncio.create_task(watch_service_stop(documents, request, 'process', target, interval=0.01))
    try:
        await asyncio.sleep(0.04)
        assert not target.done()
        record[field] = {'id': 'run', 'projectId': 'repo', 'taskId': 'task', 'processControlId': 'process',
            'status': 'stopping'}[field]
        await asyncio.wait_for(watcher, 1)
        assert target.cancelled()
    finally:
        watcher.cancel()
        target.cancel()
        await asyncio.gather(watcher, target, return_exceptions=True)


async def test_managed_stop_cancels_initialization_and_closes_resources(service_fixture, monkeypatch):
    fixture = service_fixture
    request = fixture.request()
    documents = DocumentLibrary(fixture.settings.console_db)
    documents.update_run(request.run_id, {'taskId': request.task_id, 'projectId': request.repo_id,
        'status': 'queued', 'processControlId': 'exact-process'}, create=True)
    monkeypatch.setenv('TRACEFIX_CONSOLE_RUN_ID', request.run_id)
    monkeypatch.setenv('TRACEFIX_PROCESS_CONTROL_ID', 'exact-process')
    started, closed = asyncio.Event(), asyncio.Event()

    class WaitingServices(RunServices):
        async def prepare(self):
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            await super().aclose()
            closed.set()

    task = asyncio.create_task(execute_request(request, fixture.settings, session_factory=fixture.factory,
        services_factory=WaitingServices))
    await asyncio.wait_for(started.wait(), 5)
    documents.update_run(request.run_id, {'status': 'stopping', 'stopRequestedAt': 'now'})
    result = await asyncio.wait_for(task, 5)
    assert result['run_status'] == 'CANCELLED' and result['outcome'] == str(Outcome.INCONCLUSIVE)
    assert result['report_ref'] and closed.is_set()
