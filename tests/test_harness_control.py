"""Python 控制桥的字段、归属和冻结配置边界回归。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.cli.main import Session
from tracefix.harness_control import ControlIdentity, HarnessControl, HarnessNotFound, ResumeRequest
from tracefix.runtime.contracts import RunState, digest
from tracefix.runtime.harness_bootstrap import restore_harness_runtime
from tracefix.runtime.harness_contracts import HarnessJob, HarnessSession
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, MemoryHarnessStore
from tracefix.storage.store import MemoryStore

from test_harness_store import postgres_harness_pair


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


@pytest.mark.parametrize('field', ['task_id', 'scope_id', 'job_id'])
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
