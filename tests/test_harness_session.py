from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tracefix.cli.main import Session
from tracefix.runtime.contracts import Outcome, RunState, digest
from tracefix.runtime.engine import Engine
from tracefix.runtime.smoke import make_engine
from tracefix.storage.harness import HarnessConflict
from tracefix.storage.store import MemoryStore


@pytest.mark.parametrize('same_job', [True, False])
def test_session_tracks_current_increment_without_changing_console_identity(same_job):
    store = MemoryStore()
    original = RunState(run_id='original', scope_id='scope', goal='goal', url='http://app.test',
        harness_job_id='job')
    child = original.model_copy(update={'run_id': 'child',
        'harness_job_id': 'job' if same_job else 'other-job'})
    store.save(original)
    store.save(child)
    session = object.__new__(Session)
    session.run_id, session.scope, session.store = original.run_id, original.scope_id, store
    session.engine = SimpleNamespace(current_run=child.run_id)
    if same_job:
        assert session.state().run_id == child.run_id
        assert session.run_id == original.run_id
    else:
        with pytest.raises(PermissionError, match='原 SDK Job'):
            session.state()


async def test_finalize_rejects_stop_before_reading_or_committing_workspace():
    check = Mock(side_effect=HarnessConflict('停止意图已落盘'))
    engine = SimpleNamespace(harness=SimpleNamespace(_check=check))
    state = RunState(scope_id='scope', goal='goal', url='http://app.test', harness_job_id='job')
    with pytest.raises(HarnessConflict, match='停止意图'):
        await Engine.finalize(engine, state, None)
    check.assert_called_once_with(engine, state)


async def test_sdk_preparation_rejects_guidance_before_job_is_attached():
    store = MemoryStore()
    state = RunState(run_id='preparing', scope_id='scope', goal='goal', url='http://app.test',
        coding_backend='claude_sdk')
    store.save(state)
    session = object.__new__(Session)
    session.run_id, session.scope, session.store = state.run_id, state.scope_id, store
    session.engine = SimpleNamespace(current_run=state.run_id)
    with pytest.raises(ValueError, match='不接收补充指令'):
        await session.dispatch('不得注入正在冻结的任务')
    assert not store.load(state.run_id, state.scope_id).guidance


async def test_stop_during_final_verification_prevents_local_commit(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path)
    state.harness_job_id = 'job'
    state.outcome = Outcome.FIX_VERIFIED
    state.execution_mode = 'batch'
    patch = 'verified candidate'
    state.patch_hash = digest(patch.encode())
    stopped = False

    def check(*args):
        if stopped:
            raise HarnessConflict('验收期间收到停止意图')

    async def verify(current):
        nonlocal stopped
        stopped = True
        return True

    engine.harness = SimpleNamespace(_check=check)
    monkeypatch.setattr(engine.workspace, 'diff', lambda *args: patch)
    branch = Mock(side_effect=AssertionError('停止后不得创建提交'))
    monkeypatch.setattr(engine.workspace, 'branch', branch)
    monkeypatch.setattr(engine, 'require_repair_rule_coverage', lambda *args: None)
    monkeypatch.setattr(engine, 'rules_fully_checked', lambda *args: True)
    monkeypatch.setattr(engine, 'source_acceptance_ready', lambda *args: True)
    monkeypatch.setattr(engine, 'source_rule_trials_passed', lambda *args: True)
    monkeypatch.setattr(engine, 'runtime_verification_passed', verify)
    with pytest.raises(HarnessConflict, match='验收期间'):
        await engine.finalize(state, None)
    branch.assert_not_called()
