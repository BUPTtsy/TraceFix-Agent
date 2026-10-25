import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracefix.agents.claude_sdk_adapter import ExecutionResult
from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.execution.repository import run_git
from tracefix.runtime.contracts import Phase, RunState, digest
from tracefix.runtime.harness import HarnessRuntime, environment_key, validate_plan
from tracefix.runtime.harness_contracts import HarnessJob, HarnessWorkItem, TaskPlan
from tracefix.runtime.smoke import FakeBrowser, FakeRunner, make_engine
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import MemoryHarnessStore


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp('hg')


def test_environment_key_changes_when_dependency_lock_changes(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    config = type('Config', (), {'config_hash': 'sdk-config'})()
    first = environment_key(engine, state, config)
    engine.source['files']['requirements.lock'] = digest('first')
    second = environment_key(engine, state, config)
    assert second != first
    engine.source['files']['requirements.lock'] = digest('second')
    assert environment_key(engine, state, config) != second


def test_validate_plan_preserves_acceptance_and_writable_scope(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    job = HarnessJob(job_id='job', scope_id=state.scope_id, repo_id='ci', console_task_id='task',
        goal_hash=digest(state.goal), origin_base_commit='a' * 40, acceptance_refs=['spec'],
        local_commit_authorized=True)
    item = HarnessWorkItem(harness_task_id='item', job_id=job.job_id, scope_id=job.scope_id,
        title='修复', acceptance_refs=['spec'], allowed_files=['src/**'], writable_files=['src/**'])
    plan = TaskPlan(scope_id=job.scope_id, job_id=job.job_id, session_id='session', run_id='run',
        plan_version=1, acceptance_refs=['spec'], work_items=[item])
    assert validate_plan(plan, job, engine.workspace).work_items[0].harness_task_id == 'item'
    with pytest.raises((PermissionError, ValueError)):
        validate_plan(plan.model_copy(update={'acceptance_refs': ['forged']}), job, engine.workspace)
    with pytest.raises(PermissionError):
        validate_plan(plan.model_copy(update={'work_items': [item.model_copy(update={'writable_files': ['.env']})]}),
            job, engine.workspace)


def test_harness_route_does_not_fall_back_to_legacy_repair(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')

    class Harness:
        def _check(self, _engine, _state):
            return None

    engine.harness = Harness()
    state = state.model_copy(update={'harness_job_id': 'job', 'harness_stage': 'coding', 'phase': Phase.DIAGNOSE})
    assert engine.route(state) == 'harness_code'
    state = state.model_copy(update={'phase': Phase.REVIEW})
    assert engine.route(state) == 'harness_review'


def test_environment_key_binds_frozen_sdk_configuration(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    first = environment_key(engine, state, type('Config', (), {'config_hash': 'sdk-a'})())
    second = environment_key(engine, state, type('Config', (), {'config_hash': 'sdk-b'})())
    assert first != second


async def test_final_verification_restores_terminal_state_without_losing_usage_or_audit(tmp_path, monkeypatch):
    from tracefix.storage.store import MemoryStore

    original = RunState(scope_id='scope', goal='最终验收', url='http://app.test', phase=Phase.FINALIZE,
        run_status='COMPLETED', environment_digest='environment', source_manifest='source',
        revision=4, model_exchange_refs=['before-request'], reasoning_refs=['before-reasoning'])
    original.budget = original.budget.model_copy(update={'model_calls': 2, 'tokens': 30})
    store = MemoryStore()
    store.save(original)
    spec = SimpleNamespace(mode='gui', model_dump=lambda **options: {'goal': original.goal})
    engine = SimpleNamespace(store=store, current_run=original.run_id,
        scopes=SimpleNamespace(projects={'scope': SimpleNamespace(root=tmp_path)}),
        workspace=SimpleNamespace(root=tmp_path, head=lambda: 'candidate'), spec=lambda state: spec,
        runner=SimpleNamespace(inspect_images=AsyncMock(return_value='environment'),
            start=AsyncMock(return_value={'passed': True}), close=AsyncMock()),
        browser=SimpleNamespace(open=AsyncMock(), close=AsyncMock()),
        runtime_verification_passed=AsyncMock(return_value=True))

    async def verify(state, node):
        latest = state.model_copy(deep=True, update={'phase': Phase.REVIEW,
            'revision': state.revision + 1,
            'budget': state.budget.model_copy(update={'model_calls': 3, 'tokens': 50}),
            'model_exchange_refs': state.model_exchange_refs + ['final-request', 'final-response'],
            'reasoning_refs': state.reasoning_refs + ['final-reasoning']})
        store.save(latest)
        return {'data': latest.model_dump(mode='json')}

    engine.verify = verify
    runtime = object.__new__(HarnessRuntime)
    host = {'frozen_spec': spec.model_dump()}
    monkeypatch.setattr(runtime, '_read', lambda state: host)
    monkeypatch.setattr(runtime, '_write', lambda state, value: None)
    monkeypatch.setattr(runtime, 'verification_record', lambda *args, **kwargs: SimpleNamespace(input_commit=None))
    monkeypatch.setattr('tracefix.runtime.harness.run_git', lambda *args, **kwargs: b'target')
    monkeypatch.setattr('tracefix.runtime.harness.tracked_files', lambda *args: {'source.py': 'hash'})
    job = SimpleNamespace(accepted_commit='candidate', origin_base_commit='origin', acceptance_refs=['acceptance'])
    await runtime._final_verification(engine, original, job, 'diff-ref', 'patch-hash')
    restored = store.load(original.run_id, original.scope_id)
    assert restored.run_status.value == 'COMPLETED' and restored.phase == Phase.FINALIZE
    assert restored.revision > original.revision
    assert restored.budget.model_calls == 3 and restored.budget.tokens == 50
    assert restored.model_exchange_refs == ['before-request', 'final-request', 'final-response']
    assert restored.reasoning_refs == ['before-reasoning', 'final-reasoning']
    assert engine.current_run == original.run_id


class IncrementExecutor:
    def __init__(self):
        self.requests = []
        self.host_commands = []

    async def start(self, request, emit):
        self.requests.append(request)
        payload = json.loads(request.prompt)
        if request.role == 'initializer':
            output = {'commands': [payload['approved_commands']['unit']], 'work_items': [
                {'id': 'increment-one', 'title': '修复状态', 'depends_on': [],
                 'acceptance_refs': payload['acceptance_refs'], 'allowed_files': ['src/value.ts'],
                 'writable_files': ['src/value.ts']},
                {'id': 'increment-two', 'title': '添加回归保护', 'depends_on': ['increment-one'],
                 'acceptance_refs': payload['acceptance_refs'], 'allowed_files': ['src/value.ts'],
                 'writable_files': ['src/value.ts']}]}
        else:
            source = request.workspace / 'src/value.ts'
            if payload['task']['harness_task_id'] == 'increment-one':
                source.write_text('export const persisted = true;\n', encoding='utf-8')
            else:
                assert source.read_text(encoding='utf-8') == 'export const persisted = true;\n'
                source.write_text('export const persisted = true;\nexport const guarded = true;\n', encoding='utf-8')
            output = {'summary': 'SDK 候选', 'unresolved': []}
        return ExecutionResult(execution_id=request.execution_id, status='SUCCEEDED',
            sdk_session_id='sdk-' + request.session_id, summary='SDK 候选', structured_output=output)


async def create_harness_engine(tmp_path, *, final_verifier=None, fail_validation_once=False,
                                configure=None, runner_factory=None, engine_state=None):
    engine, state = engine_state or make_engine(tmp_path / 'engine')
    store = MemoryHarnessStore(engine.artifacts)
    executor = IncrementExecutor()
    injected_failure = False
    bound_commit = None

    async def rebind(current, workspace, source):
        nonlocal injected_failure, bound_commit
        source_identity = digest(source)
        if bound_commit != source_identity:
            current.model.decision_calls = 0
            bound_commit = source_identity
        current.workspace, current.source = workspace, source
        current.runner = (runner_factory(workspace, digest(source)) if runner_factory
                          else FakeRunner(digest(source)))
        original_command = current.runner.command

        async def command(kind):
            nonlocal injected_failure
            active = current.store.load(current.current_run, current.context.active_scope)
            executor.host_commands.append({'kind': kind, 'stage': active.harness_stage})
            if (fail_validation_once and not injected_failure and kind == 'unit'
                    and 'true' in workspace.read('src/value.ts')):
                injected_failure = True
                return {'passed': False, 'exit_code': 7, 'output': '宿主实际单测失败：需要重试当前候选'}
            return await original_command(kind)

        current.runner.command = command
        current.browser = FakeBrowser(workspace)
        current.model.workspace = workspace

    config = ClaudeSdkConfig(backend='claude_sdk', model_id='claude-sonnet-4-6',
        credential_ref='env:TEST_ANTHROPIC_KEY', worker_image='sha256:' + 'a' * 64,
        egress_allowlist=['https://api.anthropic.com'])
    harness = HarnessRuntime(store, executor, config, tmp_path / 'h', rebind=rebind,
        final_verifier=final_verifier)
    if configure is not None:
        configure(engine, state)
    if runner_factory is not None:
        engine.runner = runner_factory(engine.workspace, state.source_manifest)
    engine.store.save(state)
    state = harness.attach(engine, state, console_task_id='console-task', local_commit_authorized=True)
    return engine, state, harness, executor


async def test_real_langgraph_runs_two_dependent_increments_with_independent_final_verifier(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    output = await engine.run(state)
    job = harness.store.get_job(state.harness_job_id, scope_id=state.scope_id)
    assert job.status == 'COMPLETED', output['data'].get('error')
    items = harness.store.list_work_items(job.job_id, scope_id=state.scope_id)
    assert [item.status for item in items] == ['VERIFIED', 'VERIFIED']
    assert [item.role for item in executor.requests] == ['initializer', 'coder', 'coder']
    sessions = harness.store.list_sessions(job.job_id, scope_id=state.scope_id)
    first, second = [session for session in sessions if session.kind == 'coder']
    assert second.input_commit == first.candidate_commit
    assert first.run_id != second.run_id
    assert first.input_commit == job.origin_base_commit
    assert job.accepted_commit == second.candidate_commit
    second_state = engine.store.load(second.run_id, state.scope_id)
    assert second_state.run_status.value == 'COMPLETED'
    assert second_state.reproduced is False
    assert second_state.initial_check_result_refs
    assert all(engine.get(second_state, ref)['status'] == 'pass'
               for ref in second_state.initial_check_result_refs)
    assert engine.workspace.read('src/value.ts') == 'export const persisted = true;\nexport const guarded = true;\n'
    assert not engine.workspace.diff()
    registered = engine.scopes.projects[state.scope_id].root
    with pytest.raises(RuntimeError):
        run_git(registered, 'cat-file', '-e', first.candidate_commit)
    lineage = harness._read(state)['lineage']
    receipt = engine.artifacts.json(state.scope_id, first.run_id, lineage[0]['ref'])
    assert run_git(Path(receipt['candidate_repository']), 'rev-parse', 'HEAD').decode().strip() == first.candidate_commit
    assert job.final_target_commit == run_git(registered, 'rev-parse', 'HEAD').decode().strip()
    for kind in ('static', 'unit', 'build'):
        assert sum(call == {'kind': kind, 'stage': 'final_acceptance'} for call in executor.host_commands) == 1
    final_state = RunState.model_validate(output['data'])
    await harness.final(engine, final_state)
    assert harness.job(state).final_verification_ref == job.final_verification_ref
    run_git(registered, 'commit', '--allow-empty', '-m', '目标分支验收后移动', identity=('CI', 'ci@example.invalid'))
    target = run_git(registered, 'rev-parse', 'HEAD').decode().strip()
    await harness.final(engine, final_state)
    refreshed = harness.job(state)
    assert refreshed.status == 'COMPLETED' and refreshed.quality == 'MERGE_READY'
    assert refreshed.final_target_commit == target
    assert refreshed.final_verification_ref != job.final_verification_ref
    assert len(executor.requests) == 3
    assert engine.store.load(second.run_id, state.scope_id).run_status.value == 'COMPLETED'
    for kind in ('static', 'unit', 'build'):
        assert sum(call == {'kind': kind, 'stage': 'final_acceptance'} for call in executor.host_commands) == 2


async def test_sdk_candidate_export_accepts_runs_with_rule_snapshot_refs(tmp_path, monkeypatch):
    # 真实 Run 的 rule_refs 是规则快照引用对象，不能原样作为 PatchProposal 的规则 ID 列表。
    original = HarnessRuntime.code
    exported = []

    async def code(self, engine, state):
        # 只在编码节点内注入快照引用对象；节点返回后恢复，避免 fake engine 缺少规则快照触发后续门禁。
        refs = state.rule_refs
        output = await original(self, engine, engine.changed(state,
            rule_refs=[{'id': 'builtin_api_no_5xx', 'version': 1}]))
        current = RunState.model_validate(output['data'])
        exported.append(engine.get(current, current.patch_ref)['rule_refs'])
        return {**output, 'data': engine.changed(current, rule_refs=refs).model_dump(mode='json')}

    monkeypatch.setattr(HarnessRuntime, 'code', code)
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    output = await engine.run(state)
    job = harness.job(state)
    assert job.status == 'COMPLETED', output['data'].get('error')
    assert exported == [[], []]


async def test_initializer_schema_requires_file_scope_matching_plan_validation(tmp_path):
    import jsonschema

    engine, state, harness, executor = await create_harness_engine(tmp_path)
    await engine.run(state)
    request = executor.requests[0]
    assert request.role == 'initializer'
    item = {'id': 'verify-only', 'title': '只做验证', 'depends_on': [], 'acceptance_refs': [],
            'allowed_files': [], 'writable_files': []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'commands': [], 'work_items': [item]}, request.output_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'commands': [], 'work_items': []}, request.output_schema)
    assert '不要单列只做验证的增量' in json.loads(request.prompt)['instruction']


async def test_completed_logical_invocation_reentry_reuses_result_and_frozen_input(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    payload = {'approved_commands': engine.profile.commands, 'acceptance_refs': [state.test_spec_ref]}
    first_state, first_execution, first_result = await harness._execute(engine, state, 'initializer',
        json.dumps(payload))
    second_state, second_execution, second_result = await harness._execute(engine, first_state,
        'initializer', 'changed transient runtime view')
    assert first_execution.execution_id == second_execution.execution_id
    assert first_execution.input_hash == second_execution.input_hash
    assert first_result == second_result
    assert len(executor.requests) == 1


async def test_final_verifier_failure_keeps_two_accepted_increments(tmp_path):
    async def fail_final(engine, state, job, diff_ref, patch_hash):
        raise RuntimeError('累计验收注入失败')

    engine, state, harness, executor = await create_harness_engine(tmp_path, final_verifier=fail_final)
    output = await engine.run(state)
    job = harness.job(state)
    assert job.status == 'BLOCKED' and job.blocker == 'FINAL_ACCEPTANCE_FAILED', output['data'].get('error')
    items = harness.store.list_work_items(job.job_id, scope_id=state.scope_id)
    assert len(items) == 2 and all(item.status == 'VERIFIED' for item in items)
    assert job.accepted_commit != job.origin_base_commit
    assert len(executor.requests) == 3


async def test_failed_candidate_receipts_reach_new_invocation_in_same_session(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path, fail_validation_once=True)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    coders = [request for request in executor.requests if request.role == 'coder']
    assert len(coders) == 3
    first, retry = coders[:2]
    assert retry.session_id == first.session_id
    assert retry.execution_id != first.execution_id and retry.input_hash != first.input_hash
    assert retry.sdk_session_id == 'sdk-' + first.session_id
    feedback = json.loads(retry.prompt)['feedback']
    assert feedback['execution_id'] == first.execution_id
    assert any(command['exit_code'] == 7 for command in feedback['verification']['commands'])
    assert any(observation.get('exit_code') == 7 and '宿主实际单测失败' in observation.get('output', '')
               for observation in feedback['observations'])
    sessions = harness.store.list_sessions(state.harness_job_id, scope_id=state.scope_id)
    first_session = next(session for session in sessions if session.session_id == first.session_id)
    assert first_session.invocation_no == 2


@pytest.mark.parametrize('failure', ['handoff', 'accept_transaction', 'host_state'])
async def test_increment_accept_retries_without_duplicate_commit_or_lost_lineage(tmp_path, monkeypatch, failure):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    original_accept = harness.accept
    injected = []

    async def retry_accept(current, candidate):
        if injected:
            return await original_accept(current, candidate)
        commit = current.workspace.head()
        before = harness.job(candidate).accepted_commit
        if failure == 'handoff':
            original_put = current.put

            def fail_put(active, value, ext='json', *, name='数据记录'):
                if name == '增量会话交接':
                    raise OSError('交接持久化注入失败')
                return original_put(active, value, ext, name=name)

            monkeypatch.setattr(current, 'put', fail_put)
            with pytest.raises(OSError, match='注入失败'):
                await original_accept(current, candidate)
            monkeypatch.setattr(current, 'put', original_put)
        elif failure == 'accept_transaction':
            original_transaction = harness.store.accept_increment

            def fail_transaction(*args, **kwargs):
                raise RuntimeError('接纳事务注入失败')

            monkeypatch.setattr(harness.store, 'accept_increment', fail_transaction)
            with pytest.raises(RuntimeError, match='注入失败'):
                await original_accept(current, candidate)
            monkeypatch.setattr(harness.store, 'accept_increment', original_transaction)
        else:
            original_write = harness._write

            def fail_host_write(active, value):
                if len(value['lineage']) == 1 and 'pending_accept' not in value:
                    raise OSError('接纳后宿主记录持久化注入失败')
                return original_write(active, value)

            monkeypatch.setattr(harness, '_write', fail_host_write)
            with pytest.raises(OSError, match='注入失败'):
                await original_accept(current, candidate)
            monkeypatch.setattr(harness, '_write', original_write)
        assert harness.job(candidate).accepted_commit == (commit if failure == 'host_state' else before)
        assert current.workspace.head() == commit
        injected.append(commit)
        result = await original_accept(current, candidate)
        assert harness.job(candidate).accepted_commit == commit
        assert current.workspace.head() == commit
        return result

    monkeypatch.setattr(harness, 'accept', retry_accept)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    host = harness._read(state)
    assert len(host['lineage']) == 2
    assert len({entry['commit'] for entry in host['lineage']}) == 2
    assert injected[0] == host['lineage'][0]['commit']
