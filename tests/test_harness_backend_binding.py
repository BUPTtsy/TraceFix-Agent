import ast
import inspect
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig, load_claude_sdk_config
from tracefix.runtime.contracts import digest
from tracefix.runtime.harness import HarnessRuntime
from tracefix.runtime.harness_bootstrap import restore_harness_runtime
from tracefix.runtime import repair_loop

from tests.test_harness_graph import create_harness_engine


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(prefix='hb', dir=Path.cwd().anchor) as directory:
        yield Path(directory)


def test_sdk_execution_source_has_no_legacy_model_or_history_dependencies():
    forbidden = {'model_call', 'segment_history', 'exchange_history', 'checkpoint_segment',
        'compact_context', 'PydanticAIAdapter', 'DeepAgentsReadonlyAdapter', 'ReadOnlyWorker',
        'WorkerScheduler', 'SubAgentRuntime', 'run_segment', 'message_history'}
    for method in (HarnessRuntime.initialize, HarnessRuntime.code, HarnessRuntime._execute):
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        names.update(node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute))
        assert not names & forbidden, (method.__name__, names & forbidden)


async def test_sdk_two_increment_execution_never_calls_legacy_model_worker_or_compaction(tmp_path, monkeypatch):
    from tracefix.agents.deepagents_adapter import DeepAgentsReadonlyAdapter
    from tracefix.agents.pydantic_ai_adapter import PydanticAIAdapter
    from tracefix.runtime.worker import ReadOnlyWorker
    from tracefix.workers.scheduler import WorkerScheduler

    engine, state, harness, executor = await create_harness_engine(tmp_path)
    original_execute = harness._execute
    observed_roles = []

    def forbidden(*args, **kwargs):
        pytest.fail('SDK execution 调用了 legacy 模型、worker 或历史压缩入口')

    async def isolated_execute(current, active, role, *args, **kwargs):
        with monkeypatch.context() as isolation:
            isolation.setattr(current, 'model_call', forbidden)
            isolation.setattr(current, 'compact_context', forbidden)
            isolation.setattr(PydanticAIAdapter, 'generate', forbidden)
            isolation.setattr(DeepAgentsReadonlyAdapter, 'run', forbidden)
            isolation.setattr(ReadOnlyWorker, 'run', forbidden)
            isolation.setattr(WorkerScheduler, 'submit', forbidden)
            for name in ('segment_history', 'exchange_history', 'checkpoint_segment', 'run_segment'):
                isolation.setattr(repair_loop, name, forbidden)
            output = await original_execute(current, active, role, *args, **kwargs)
            observed_roles.append(role)
            return output

    monkeypatch.setattr(harness, '_execute', isolated_execute)
    output = await engine.run(state)
    job = harness.job(state)
    assert job.status == 'COMPLETED', output['data'].get('error')
    assert observed_roles == ['initializer', 'coder', 'coder']
    assert len(executor.requests) == 3
    assert all(request.config.backend == 'claude_sdk' for request in executor.requests)
    assert all(not hasattr(request, 'message_history') for request in executor.requests)
    sessions = harness.store.list_sessions(job.job_id, scope_id=state.scope_id)
    events = [event for session in sessions for event in engine.store.trace(session.run_id, state.scope_id)]
    assert sum(event['type'] == 'sdk.execution.started' for event in events) == 3


async def test_default_backend_rollback_does_not_change_existing_job_config_ref(tmp_path, monkeypatch):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    before = harness.job(state)
    frozen = engine.get(state, before.config_ref)
    assert frozen['kind'] == 'harness_configuration'
    assert digest(ClaudeSdkConfig.model_validate(frozen['config'])) == before.config_digest
    monkeypatch.setenv('TRACEFIX_CODING_BACKEND', 'legacy')
    monkeypatch.setenv('TRACEFIX_CLAUDE_MODEL', 'changed-deployment-model')
    assert load_claude_sdk_config().backend == 'legacy'
    restored = restore_harness_runtime(engine, state, store=harness.store)
    after = harness.job(state)
    assert after.backend == 'claude_sdk'
    assert (after.config_ref, after.config_digest) == (before.config_ref, before.config_digest)
    assert restored.config == ClaudeSdkConfig.model_validate(frozen['config'])
    assert not executor.requests


async def test_restore_rejects_changed_frozen_configuration(tmp_path, monkeypatch):
    engine, state, harness, _ = await create_harness_engine(tmp_path)
    frozen = engine.get(state, harness.job(state).config_ref)
    frozen['config']['model_id'] = 'tampered-model'
    monkeypatch.setattr(engine.artifacts, 'json', lambda *args: frozen)
    with pytest.raises(PermissionError, match='摘要'):
        restore_harness_runtime(engine, state, store=harness.store)


async def test_restore_requires_initializer_session_and_config_ref(tmp_path, monkeypatch):
    engine, state, harness, _ = await create_harness_engine(tmp_path)
    monkeypatch.setattr(harness.store, 'list_sessions', lambda *args, **kwargs: [])
    with pytest.raises(PermissionError, match='初始化 Session'):
        restore_harness_runtime(engine, state, store=harness.store)


async def test_changed_runtime_configuration_cannot_attach_to_existing_job(tmp_path):
    engine, state, harness, _ = await create_harness_engine(tmp_path)
    changed = harness.config.model_copy(update={'model_id': 'changed-deployment-model'})
    other = HarnessRuntime(harness.store, harness.runner, changed, harness.root)
    with pytest.raises(PermissionError, match='配置'):
        other.attach(engine, state, console_task_id='console-task', local_commit_authorized=True)
