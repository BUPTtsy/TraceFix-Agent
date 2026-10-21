"""Python 控制桥的字段、归属和冻结配置边界回归。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.cli.main import Session
from tracefix.harness_control import ControlIdentity, ControlRequest, HarnessControl, HarnessNotFound, ResumeRequest
from tracefix.agents.claude_sdk_adapter import ExecutionInspection
from tracefix.execution.candidate_export import _snapshot
from tracefix.runtime.contracts import RunState, digest
from tracefix.runtime.harness_bootstrap import restore_harness_runtime
from tracefix.runtime.harness_contracts import HarnessJob, HarnessSession
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, MemoryHarnessStore
from tracefix.storage.store import MemoryStore

from test_harness_store import postgres_harness_pair
from test_harness_recovery import stopped_execution
from test_harness_store import coder


def make_job(authority, *, job_id='job', task_id='task', scope_id='scope', **options):
    return authority.create_job(HarnessJob(job_id=job_id, scope_id=scope_id,
        console_task_id=task_id, repo_id='repo', goal_hash=digest('冻结目标'),
        origin_base_commit='a' * 40, acceptance_refs=['spec'], **options))


@pytest.fixture
def control(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    store = MemoryStore()
    authority = MemoryHarnessStore(artifacts)
    return HarnessControl(store, artifacts, tmp_path, authority=authority)


@pytest.mark.parametrize('field', ['prompt', 'instruction', 'goal', 'unexpected'])
def test_resume_request_rejects_all_extra_fields(field):
    assert ResumeRequest.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        ResumeRequest.model_validate({field: '不应注入执行会话'})


@pytest.mark.parametrize('field', ['task_id', 'scope_id', 'job_id', 'run_id'])
@pytest.mark.parametrize('value', ['../other', 'bad/value', ''])
def test_control_identity_rejects_unsafe_identifiers(field, value):
    fields = {'task_id': 'task', 'scope_id': 'scope', 'job_id': 'job', field: value}
    with pytest.raises(ValidationError):
        ControlIdentity.model_validate(fields)


def test_job_lookup_without_binding_has_not_found_type(control):
    with pytest.raises(HarnessNotFound):
        control.job(ControlIdentity(task_id='task', scope_id='scope'))


def test_job_lookup_rejects_ambiguous_task_binding(control):
    make_job(control.authority)
    make_job(control.authority, job_id='second-job')
    with pytest.raises(HarnessConflict, match='唯一'):
        control.job(ControlIdentity(task_id='task', scope_id='scope'))


def test_explicit_job_checks_task_and_scope(control):
    job = make_job(control.authority)
    assert control.job(ControlIdentity(task_id='task', scope_id='scope', job_id='job')).job_id == job.job_id
    with pytest.raises(PermissionError):
        control.job(ControlIdentity(task_id='other-task', scope_id='scope', job_id='job'))
    with pytest.raises(PermissionError):
        control.job(ControlIdentity(task_id='task', scope_id='other-scope', job_id='job'))


def test_control_state_rejects_session_run_identity_mismatch(control):
    job = make_job(control.authority)
    session = control.authority.create_session(HarnessSession(session_id='init', job_id='job',
        scope_id='scope', kind='initializer', run_id='run-init', input_commit=job.accepted_commit),
        scope_id='scope', expected_revision=job.revision)
    state = RunState(scope_id='scope', run_id=session.run_id, goal='冻结目标',
        url='http://app.test', harness_job_id='other-job', harness_session_id=session.session_id)
    control.store.save(state)
    with pytest.raises(PermissionError, match='身份'):
        control.state(job)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['普通补充文字', '/continue run-sdk 新的指令',
    '/hint 额外建议', '/retarget 新目标', '/constrain 额外条件'])
async def test_sdk_text_and_continue_cannot_persist_guidance(control, text):
    state = RunState(scope_id='scope', run_id='run-sdk', goal='冻结目标',
        url='http://app.test', harness_job_id='job', run_status='PAUSED')
    control.store.save(state)
    submit = Mock(side_effect=AssertionError('SDK guidance 不应写入'))
    session = object.__new__(Session)
    session.run_id, session.scope, session.store = state.run_id, state.scope_id, control.store
    session.engine = SimpleNamespace(submit_guidance=submit, notes=[])
    session.task = None
    with pytest.raises(ValueError, match='不接收补充指令'):
        await session.dispatch(text)
    submit.assert_not_called()
    assert session.engine.notes == []
    assert control.store.load(state.run_id, state.scope_id).continuation_instruction is None
    assert control.store.events == {}


def configured_runtime(control, monkeypatch):
    config = ClaudeSdkConfig(backend='claude_sdk', model_id='claude-sonnet-4-6',
        credential_ref='env:TEST_ANTHROPIC_KEY', worker_image='sha256:' + 'a' * 64,
        egress_allowlist=['https://api.anthropic.com'])
    reference = control.artifacts.put('scope', 'run-init', {'config': config.model_dump(mode='json')})
    job = make_job(control.authority, config_ref=reference, config_digest=config.config_hash)
    session = control.authority.create_session(HarnessSession(session_id='init', job_id=job.job_id,
        scope_id='scope', kind='initializer', run_id='run-init', input_commit=job.accepted_commit),
        scope_id='scope', expected_revision=job.revision)
    state = RunState(scope_id='scope', goal='冻结目标', url='http://app.test',
        harness_job_id=job.job_id, harness_session_id=session.session_id)
    monkeypatch.setenv('TRACEFIX_CODING_BACKEND', 'legacy')
    monkeypatch.setenv('TRACEFIX_CLAUDE_MODEL', 'changed-default')
    engine = SimpleNamespace(store=control.store, artifacts=control.artifacts)
    return engine, state, config


def test_restore_uses_job_frozen_config_after_default_backend_rollback(control, monkeypatch):
    engine, state, config = configured_runtime(control, monkeypatch)
    runtime = restore_harness_runtime(engine, state, store=control.authority)
    assert runtime.config == config
    assert runtime.config.backend == 'claude_sdk'
    assert runtime.config.model_id == 'claude-sonnet-4-6'


def test_restore_rejects_job_config_digest_drift(control, monkeypatch):
    engine, state, _config = configured_runtime(control, monkeypatch)
    control.authority.jobs['job'].config_digest = 'forged'
    with pytest.raises(PermissionError, match='摘要'):
        restore_harness_runtime(engine, state, store=control.authority)


def test_postgres_control_job_lookup_checks_task_and_scope(postgres_harness_pair, tmp_path):
    authority = postgres_harness_pair[0]
    job = make_job(authority)
    control = HarnessControl(authority.store, authority.artifacts, tmp_path, authority=authority)
    assert control.job(ControlIdentity(task_id='task', scope_id='scope')).job_id == job.job_id
    with pytest.raises(PermissionError):
        control.job(ControlIdentity(task_id='other-task', scope_id='scope', job_id=job.job_id))
    with pytest.raises(PermissionError):
        control.job(ControlIdentity(task_id='task', scope_id='other-scope', job_id=job.job_id))


@pytest.mark.parametrize('storage', ['memory', 'postgres'])
def test_origin_run_selects_job_among_task_history(control, request, storage):
    if storage == 'postgres':
        authority = request.getfixturevalue('postgres_harness_pair')[0]
        control = HarnessControl(authority.store, authority.artifacts, control.root, authority=authority)
    for job_id, run_id in [('old-job', 'old-run'), ('new-job', 'new-run')]:
        job = make_job(control.authority, job_id=job_id)
        control.authority.create_session(HarnessSession(session_id=job_id + '-init', job_id=job_id,
            scope_id='scope', kind='initializer', run_id=run_id, input_commit=job.accepted_commit),
            scope_id='scope', expected_revision=job.revision)
    assert control.job(ControlIdentity(task_id='task', scope_id='scope', run_id='new-run')).job_id == 'new-job'
    with pytest.raises(PermissionError, match='Console Run'):
        control.job(ControlIdentity(task_id='task', scope_id='scope', job_id='old-job', run_id='new-run'))
    with pytest.raises(HarnessNotFound):
        control.job(ControlIdentity(task_id='task', scope_id='scope', run_id='missing-run'))


def stopped_control(control, tmp_path, monkeypatch):
    job, session, execution, checkpoint, reference = stopped_execution(control.authority)
    state = RunState(scope_id='scope', run_id=session.run_id, goal='冻结目标', url='http://app.test',
        harness_job_id=job.job_id, harness_session_id=session.session_id)
    control.store.save(state)
    source = tmp_path / 'sdk-source'
    source.mkdir()
    (source / 'partial.py').write_text('partial = True\n', encoding='utf-8')
    snapshot_ref = control.artifacts.put('scope', session.run_id, _snapshot(source).model_dump())
    checkpoint = checkpoint.model_copy(update={'workspace_snapshot_ref': snapshot_ref})
    reference = control.artifacts.put('scope', session.run_id, checkpoint.model_dump())
    control.authority.executions[execution.execution_id].checkpoint_ref = reference
    control.authority.executions[execution.execution_id].workspace_ref = str(tmp_path / 'execution')
    runner = SimpleNamespace(owner_epoch=0, inspect=AsyncMock(return_value=ExecutionInspection(
        execution_id=execution.execution_id, status='STOPPED', active=False)))
    monkeypatch.setattr(control, 'engine', lambda _state: SimpleNamespace(workspace=object()))
    monkeypatch.setattr('tracefix.harness_control.SDKWorkspace.load', lambda *_args: SimpleNamespace(source=source))
    monkeypatch.setattr('tracefix.harness_control.restore_harness_runtime',
        lambda *_args, **_options: SimpleNamespace(runner=lambda _workspace: runner))
    return state, checkpoint, reference, source, runner


def control_request(operation='view'):
    return ControlRequest(operation=operation, fields=ControlIdentity(task_id='console', scope_id='scope'))


@pytest.mark.asyncio
async def test_stopped_view_verifies_disk_before_advertising_resume(control, tmp_path, monkeypatch):
    _state, _checkpoint, _reference, _source, runner = stopped_control(control, tmp_path, monkeypatch)
    view = await control.dispatch(control_request())
    assert view['control_status'] == 'STOPPED' and view['can_resume']
    runner.inspect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['checkpoint', 'snapshot', 'workspace', 'missing_snapshot'])
async def test_view_persists_damaged_recovery_artifacts_as_blocked(control, tmp_path, monkeypatch, damage):
    state, checkpoint, reference, source, runner = stopped_control(control, tmp_path, monkeypatch)
    if damage == 'workspace':
        (source / 'partial.py').write_text('tampered = True\n', encoding='utf-8')
    elif damage == 'missing_snapshot':
        snapshot_path = control.artifacts._path(state.scope_id, state.run_id, checkpoint.workspace_snapshot_ref)
        snapshot_path.rename(snapshot_path.with_suffix('.saved'))
    else:
        artifact_ref = reference if damage == 'checkpoint' else checkpoint.workspace_snapshot_ref
        control.artifacts._path(state.scope_id, state.run_id, artifact_ref).write_text('damaged', encoding='utf-8')
    view = await control.dispatch(control_request())
    assert view['control_status'] == 'BLOCKED' and not view['can_resume']
    blocker = ('RECOVERY_ARTIFACTS_MISSING' if damage == 'missing_snapshot'
        else 'RECOVERY_ARTIFACTS_INCONSISTENT')
    assert view['resume_blockers'] == [blocker]
    blocked = control.authority.get_job('job', scope_id='scope')
    assert blocked.blocker == blocker
    second = await control.dispatch(control_request())
    assert second['revision'] == view['revision']
    with pytest.raises(HarnessConflict, match='不能恢复'):
        await control.dispatch(control_request('resume.check'))
    runner.inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_view_blocks_old_run_unknown_fence_and_preserves_operation(control, tmp_path, monkeypatch):
    state, _checkpoint, _reference, _source, runner = stopped_control(control, tmp_path, monkeypatch)
    operation = dict(scope_id=state.scope_id, run_id='previous-run', status='UNKNOWN', resources=['repo:repo'])
    control.store.operations['old-write'] = operation.copy()
    view = await control.dispatch(control_request())
    assert view['control_status'] == 'BLOCKED'
    assert view['resume_blockers'] == ['EXTERNAL_OPERATION_UNKNOWN']
    assert control.store.operations['old-write'] == operation
    runner.inspect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['STARTED', 'UNKNOWN'])
async def test_stop_persists_intent_before_interrupt_and_keeps_scope_fence(control, monkeypatch, status):
    session = coder(control.authority)
    state = RunState(scope_id='scope', run_id=session.run_id, goal='冻结目标', url='http://app.test',
        harness_job_id='job', harness_session_id=session.session_id)
    control.store.save(state)
    operation = dict(scope_id='scope', run_id='previous-run', status=status, resources=['repo:repo'])
    control.store.operations['uncertain-write'] = operation.copy()
    async def stop(_engine, _state):
        job = control.authority.get_job('job', scope_id='scope')
        assert job.status == 'STOPPING' and job.stop_request_id
    interrupted = AsyncMock(side_effect=stop)
    monkeypatch.setattr(control, 'engine', lambda _state: object())
    monkeypatch.setattr('tracefix.harness_control.restore_harness_runtime',
        lambda *_args, **_options: SimpleNamespace(stop=interrupted))
    view = await control.dispatch(control_request('stop'))
    interrupted.assert_awaited_once()
    assert view['control_status'] == 'BLOCKED'
    assert view['resume_blockers'] == ['EXTERNAL_OPERATION_UNKNOWN']
    assert control.store.operations['uncertain-write'] == operation
    assert control.authority.get_job('job', scope_id='scope').stop_request_id


def test_pending_includes_same_scope_history_and_global_wildcard(control):
    state = RunState(scope_id='scope', run_id='current-run', goal='冻结目标', url='http://app.test')
    control.store.operations = {
        'history': dict(scope_id='scope', run_id='old-run', status='UNKNOWN', resources=['repo:repo']),
        'other': dict(scope_id='other-scope', run_id='other-run', status='UNKNOWN', resources=['repo:other']),
        'global': dict(scope_id='other-scope', run_id='other-run', status='STARTED', resources=['*']),
        'done': dict(scope_id='scope', run_id='old-run', status='DONE', resources=['*'])}
    assert control.pending(state) == ['history', 'global']


def test_postgres_pending_keeps_scope_history_and_wildcard(postgres_harness_pair, tmp_path):
    authority = postgres_harness_pair[0]
    control = HarnessControl(authority.store, authority.artifacts, tmp_path, authority=authority)
    current = RunState(scope_id='scope', run_id='current-run', goal='冻结目标', url='http://app.test')
    previous = current.model_copy(update={'run_id': 'previous-run'})
    other = current.model_copy(update={'scope_id': 'other-scope', 'run_id': 'other-run'})
    for state in [current, previous, other]:
        control.store.save(state)
    control.store.begin(previous, 'history', {'tool': 'external'}, owner='owner', resources=['repo:repo'])
    control.store.mark_unknown(previous, 'history', owner='owner', reason='lost')
    control.store.begin(other, 'unrelated', {'tool': 'external'}, owner='other', resources=['repo:other'])
    control.store.begin(other, 'global', {'tool': 'global'}, owner='global', resources=['global:write'])
    control.store.conn.execute("UPDATE operations SET resources='[\"*\"]'::jsonb WHERE id='global'")
    assert set(control.pending(current)) == {'history', 'global'}
