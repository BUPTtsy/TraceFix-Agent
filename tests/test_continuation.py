import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from test_cli_workspace import session, web
from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import Outcome, RunState, RunStatus, digest
from tracefix.runtime.continuation import continuation_state
from tracefix.runtime.smoke import FakeBrowser, FakeRunner, make_engine
from tracefix.knowledge.documents import ConflictError
from tracefix.rules import Rule, RuleLibrary, RuleResolver


@pytest.fixture
def engine_session(session, tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'engine', bugfree=True)
    rule = Rule(id='ci_source_guard', name='模拟源码不得包含 debugger', status='enabled', pinned=True,
        scope={'level': 'project', 'project_ids': ['b'], 'path_globs': ['src/value.ts']},
        phases=['EXPLORE', 'DIAGNOSE', 'PATCH', 'VERIFY'],
        detection={'type': 'static', 'static': {'regex': r'\bdebugger\b', 'path_globs': ['src/value.ts']}})
    monkeypatch.setattr('tracefix.rules.builtins.builtin_rules', lambda: [rule])
    engine.rule_library = RuleLibrary(session.documents.path)
    engine.rule_resolver = RuleResolver(engine.rule_library)
    registry = Path(session.args.projects)
    registry.write_text(yaml.safe_dump({'projects': [{'id': 'b', 'repo_id': 'ci',
        'root': str(engine.scopes.projects[state.scope_id].root)}]}), encoding='utf-8')
    profile = registry.parent / 'b.yaml'
    profile.write_text(yaml.safe_dump(engine.profile.model_dump(mode='json')), encoding='utf-8')
    session.select_project('b')
    session.args.data = str(tmp_path / 'engine')
    destination = Path(session.args.data) / 'workspaces' / state.run_id
    workspace, source = Workspace.export(session.scopes, session.ctx, engine.source['commit'], destination)
    engine.workspace, engine.source = workspace, source
    engine.browser.workspace = engine.model.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, source)
    engine.runner.source = state.source_manifest
    session.store, session.saver, session.artifacts = engine.store, engine.graph.checkpointer, engine.artifacts
    session.render.artifacts = session.artifacts
    session.begin_console_run(state)
    session.run_id, session.engine = state.run_id, engine
    engine.notify = session.notify
    monkeypatch.setenv('TRACEFIX_API_KEY', 'fake-ci')
    monkeypatch.setattr('tracefix.cli.main.DockerRunner', lambda *args: FakeRunner(state.source_manifest))
    monkeypatch.setattr(FakeRunner, 'browser_command', lambda self: [], raising=False)
    monkeypatch.setattr('tracefix.cli.main.MCPBrowser', lambda *args: FakeBrowser(engine.workspace, bugfree=True))
    monkeypatch.setattr('tracefix.cli.main.Gateway', lambda *args, **kwargs: engine.model)
    return session, engine, state


async def test_two_continuations_keep_identity_trace_budget_and_reports(engine_session):
    session, engine, state = engine_session
    engine.control = 'cancel'
    await session.drive(state)
    original = web(session, 'run', {'id': session.console_run_id})
    original_trace = engine.store.trace(state.run_id, 'b')
    first_report = original['reportRef']
    stopped = engine.store.load(state.run_id, 'b')
    stopped.budget.deadline = time.time() - 10
    stopped.budget.model_calls = stopped.budget.max_model_calls
    stopped.budget.tokens = stopped.budget.max_tokens
    engine.store.save(stopped)
    await session.dispatch('/runs continue ' + state.run_id + ' "重新检查页面"')
    session.engine.control = 'cancel'
    await session.task
    first_continuation = session.state()
    assert first_continuation.run_status == RunStatus.CANCELLED
    assert first_continuation.budget.model_calls == stopped.budget.model_calls
    await session.dispatch('/continue ' + original['id'] + ' "完成验证并记录状态"')
    await session.task
    final = session.state()
    assert final.run_id == state.run_id and final.goal == state.goal
    assert final.continuation_count == 2 and final.abnormal_termination
    assert final.run_status == RunStatus.COMPLETED and final.outcome == Outcome.NO_BUG_FOUND
    assert final.budget.tokens > stopped.budget.tokens
    assert final.budget.model_calls > stopped.budget.model_calls
    assert any(item['rule_id'] == 'ci_source_guard' and item['status'] == 'checked_pass'
               and item['phase'] == 'EXPLORE' for item in final.rule_coverage)
    assert final.revision > stopped.revision
    assert final.parent_run_id is None
    assert len(web(session, 'runs')) == 1
    current = web(session, 'run', {'id': original['id']})
    assert current['startedAt'] == original['startedAt']
    assert current['continuationCount'] == 2 and not current['canContinue']
    assert first_report in {entry['ref'] for entry in current['artifacts']}
    trace = session.store.trace(state.run_id, 'b')
    assert trace[:len(original_trace)] == original_trace
    assert len([event for event in trace if event['type'] == 'run.started']) == 1
    assert len([event for event in trace if event['type'] == 'run.continued']) == 2
    assert [event['seq'] for event in trace] == list(range(1, len(trace) + 1))
    shared = web(session, 'run.trace', {'id': original['id']})
    assert len([event for event in shared if event['type'] == 'run.continuation_requested']) == 2
    assert len([event for event in shared if event['type'] == 'run.continued']) == 2
    await session.dispatch('/runs trace ' + original['id'])
    assert '重新检查页面' in session.output.getvalue()
    with pytest.raises(ConflictError):
        await session.continue_task(original['id'], '已成功的任务不能继续')


async def test_failed_launch_reuses_console_task_and_records_each_request(session):
    session.goal = '修复初始化错误'
    with pytest.raises(ValueError, match='TRACEFIX_DATABASE_URL'):
        await session.create()
    original = web(session, 'runs')[0]
    for attempt in range(1, 3):
        with pytest.raises(ValueError, match='TRACEFIX_DATABASE_URL'):
            await session.continue_task(original['id'], '重试连接')
        records = web(session, 'runs')
        assert len(records) == 1 and records[0]['id'] == original['id']
        assert records[0]['continuationCount'] == attempt
        assert records[0]['status'] == 'failed'
    with pytest.raises(ValueError, match='TRACEFIX_DATABASE_URL'):
        await session.create()
    assert len(web(session, 'runs')) == 2


@pytest.mark.parametrize('status,outcome,allowed', [
    ('failed', 'INFRA_FAILURE', True), ('cancelled', 'INCONCLUSIVE', True),
    ('completed', 'INCONCLUSIVE', True), ('completed', 'NO_BUG_FOUND', False),
    ('completed', 'FIX_VERIFIED', False), ('paused', None, False), ('running', None, False)])
def test_shared_continuation_gate_and_duplicate_requests(session, status, outcome, allowed):
    record = session.documents.update_run('task', {'projectId': 'alpha', 'goal': '目标',
        'mode': 'repair', 'status': status, 'outcome': outcome}, create=True)
    assert web(session, 'run', {'id': record['id']})['canContinue'] is allowed
    if allowed:
        continued = web(session, 'run.continue', {'id': record['id'], 'instruction': '继续检查'})
        assert continued['id'] == record['id'] and continued['continuationCount'] == 1
        with pytest.raises(ConflictError):
            web(session, 'run.continue', {'id': record['id'], 'instruction': '重复点击'})
    else:
        with pytest.raises(ConflictError):
            web(session, 'run.continue', {'id': record['id'], 'instruction': '继续检查'})


async def test_continuation_revalidates_existing_patch(engine_session):
    session, engine, state = engine_session
    engine.browser.bugfree = False
    await session.drive(state)
    pending = session.state()
    assert pending.run_status == RunStatus.WAITING_APPROVAL
    patch = engine.workspace.diff()
    engine.workspace.branch('tracefix/' + pending.run_id)
    pending.run_status = RunStatus.FAILED
    pending.outcome = Outcome.INFRA_FAILURE
    engine.store.save(pending)
    session.publish_console()
    previous_trace = engine.store.trace(state.run_id, state.scope_id)
    await session.continue_task(session.console_run_id, '重试环境并重新验证补丁')
    await session.task
    resumed = session.state()
    assert resumed.run_status == RunStatus.WAITING_APPROVAL
    assert resumed.outcome == Outcome.FIX_VERIFIED
    assert resumed.patch_hash == pending.patch_hash and engine.workspace.diff(pending.patch_base_commit) == patch
    assert resumed.test_spec_ref == pending.test_spec_ref
    assert resumed.approval_ref != pending.approval_ref
    assert resumed.budget.model_calls > pending.budget.model_calls
    assert len(resumed.validation_refs) == 6
    browser_validations = [reference for reference in resumed.validation_refs
        if engine.get(resumed, reference)['kind'] in {'original', 'regression'}]
    assert set(browser_validations).isdisjoint(pending.validation_refs)
    assert set(resumed.check_result_refs).isdisjoint(pending.check_result_refs)
    assert resumed.initial_check_result_refs == pending.initial_check_result_refs
    assert any(item['rule_id'] == 'ci_source_guard' and item['status'] == 'checked_pass'
               and item['phase'] == 'VERIFY' for item in resumed.rule_coverage)
    trace = engine.store.trace(state.run_id, state.scope_id)
    verification_operations = [event['payload']['operation_id'] for event in trace[len(previous_trace):]
        if event['type'] == 'tool.started' and ':verify.' in event['payload']['operation_id']]
    assert {operation.split(':')[-2] for operation in verification_operations} == {
        'verify.static', 'verify.unit', 'verify.build', 'verify.health'}
    assert set(verification_operations).isdisjoint({
        event['payload']['operation_id'] for event in previous_trace if event['type'] == 'tool.started'})
    completed = {event['payload']['operation_id'] for event in trace[len(previous_trace):]
                 if event['type'] == 'tool.completed'}
    assert set(verification_operations) <= completed


@pytest.mark.parametrize('has_initial_failures', [False, True])
def test_continuation_retains_patch_plan_only_with_reproduction_evidence(has_initial_failures):
    previous = RunState(scope_id='b', goal='重新验证候选补丁', url='http://app:3000',
        run_status=RunStatus.FAILED, patch_hash='patch', replay_plan_ref='plan.json',
        exploration_plan_ref='exploration.json', reproduction_plan_frozen=True,
        initial_check_result_refs=['initial-failure.json'] if has_initial_failures else [],
        validation_refs=['validation.json'], check_result_refs=['check.json'],
        check_suite_stage='verify', check_suite_completed=True,
        check_suite_patch_hash='patch', overall_status='PASSED', check_summary={'passed': 1},
        rule_coverage=[{'rule_id': 'ci_source_guard', 'status': 'checked_pass'}])
    continued = continuation_state(previous, '重跑所有验证', [{}])
    assert not previous.reproduced and not continued.reproduced
    assert continued.replay_plan_ref == ('plan.json' if has_initial_failures else None)
    assert continued.exploration_plan_ref == ('exploration.json' if has_initial_failures else None)
    assert continued.reproduction_plan_frozen is has_initial_failures
    assert continued.initial_check_result_refs == previous.initial_check_result_refs
    assert continued.validation_refs == continued.check_result_refs == continued.rule_coverage == []
    assert continued.check_suite_stage is None and not continued.check_suite_completed
    assert continued.check_suite_patch_hash is None and continued.overall_status is None
    assert continued.check_summary == {}


async def test_continuation_guided_model_pass_keeps_inconclusive(engine_session):
    session, engine, state = engine_session
    rule = engine.rule_library.save_rule(Rule(id='ci_guided', name='检查页面是否清晰',
        status='enabled', pinned=True, scope={'level': 'project', 'project_ids': ['b']},
        phases=['EXPLORE', 'DIAGNOSE', 'PATCH', 'VERIFY'],
        detection={'type': 'guided', 'guided': {'prompt': '确认页面内容清晰易懂'}}))
    engine.control = 'cancel'
    await session.drive(state)
    await session.continue_task(session.console_run_id, '继续检查页面')
    await session.task
    final = session.state()
    assert final.run_status == RunStatus.COMPLETED and final.outcome == Outcome.INCONCLUSIVE
    coverage = next(item for item in final.rule_coverage
                    if item['rule_id'] == rule.id and item['phase'] == 'EXPLORE')
    assert coverage['status'] == 'not_completed'
    assert 'guided' in coverage['reason'] and coverage['evidence_refs']
    result = next(session.engine.get(final, reference) for reference in final.check_result_refs
                  if session.engine.get(final, reference)['id'] == rule.id)
    assert result['status'] == 'inconclusive'
    assert web(session, 'run', {'id': session.console_run_id})['canContinue']


async def test_crashed_process_can_continue_and_missing_workspace_keeps_history(engine_session):
    session, engine, state = engine_session
    engine.store.save(state)
    session.publish_console()
    web(session, 'run.ended', {'id': session.console_run_id, 'exitCode': 1})
    before_rejection = web(session, 'run', {'id': session.console_run_id})
    before_trace = web(session, 'run.trace', {'id': session.console_run_id})
    session.run_id = None
    saved_workspace = engine.workspace.root.with_name('saved-workspace')
    engine.workspace.root.rename(saved_workspace)
    with pytest.raises(PermissionError, match='工作区根目录或 .git 入口不可信'):
        await session.continue_task(session.console_run_id, '继续崩溃任务')
    record = web(session, 'run', {'id': session.console_run_id})
    assert record['status'] == before_rejection['status'] == 'failed'
    assert record['continuationCount'] == 0
    assert record.get('continuationMarkers', []) == before_rejection.get('continuationMarkers', [])
    assert record['agentRunId'] == before_rejection['agentRunId'] == state.run_id
    assert record.get('logs', []) == before_rejection.get('logs', [])
    assert web(session, 'run.trace', {'id': session.console_run_id}) == before_trace
    saved_workspace.rename(engine.workspace.root)
    await session.continue_task(session.console_run_id, '工作区恢复后继续')
    await session.task
    assert session.state().run_id == state.run_id
    assert session.state().continuation_count == 1


async def test_continuation_instruction_reaches_model(engine_session, monkeypatch):
    session, engine, state = engine_session
    contexts = []
    generate = engine.model.generate
    async def capture(schema, context, **kwargs):
        contexts.append(context)
        return await generate(schema, context, **kwargs)
    monkeypatch.setattr(engine.model, 'generate', capture)
    engine.control = 'cancel'
    await session.drive(state)
    await session.continue_task(session.console_run_id, '检查刷新后是否保留状态')
    await session.task
    assert contexts and all(context['user_continuation'] == '检查刷新后是否保留状态' for context in contexts)
    assert all(context['continuation_count'] == 1 for context in contexts)


async def test_web_continues_failed_launch_without_creating_task(session, monkeypatch):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, 'PORT': str(port), 'TRACEFIX_PROJECTS': session.args.projects,
        'TRACEFIX_CONSOLE_DB': session.args.console_db, 'TRACEFIX_DATA': session.args.data,
        'TRACEFIX_API_KEY': '', 'TRACEFIX_DATABASE_URL': '',
        'BUGBOARD_DATA': str(Path(session.args.data) / 'bugboard.json')}
    process = subprocess.Popen(['node', 'backend/apps/console-api/server/index.mjs'], cwd=Path(__file__).resolve().parents[1],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for variable in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
            monkeypatch.setenv(variable, 'http://127.0.0.1:1')
        monkeypatch.setenv('NO_PROXY', '')
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False) as client:
            for attempt in range(100):
                try:
                    if (await client.get('/health')).is_success:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.1)
            response = await client.post('/api/agent/start', json={'goal': '运行测试', 'mode': 'test', 'projectId': 'alpha'})
            assert response.status_code == 202, response.text
            original = response.json()
            async def wait_finished():
                for attempt in range(100):
                    status = (await client.get('/api/agent/status')).json()
                    if status['status'] == 'failed' and not status['running']:
                        return status
                    await asyncio.sleep(0.1)
                pytest.fail('Agent 进程没有结束')
            await wait_finished()
            instruction = '重试 "quoted command"\n保留 C:\\workspace 路径'
            continued = await client.post('/api/runs/' + original['id'] + '/continue', json={'instruction': instruction})
            assert continued.status_code == 202, continued.text
            result = await wait_finished()
            assert result['id'] == original['id'] and result['continuationCount'] == 1
            assert result['continuationMarkers'][0]['instruction'] == instruction
            records = (await client.get('/api/runs')).json()
            assert len(records) == 1
            trace = (await client.get('/api/runs/' + original['id'] + '/trace')).json()
            assert trace[0]['type'] == 'run.continuation_requested'
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize('paused', [False, True])
async def test_run_input_retarget_requires_confirmation_without_changing_spec(engine_session, monkeypatch, paused):
    session, engine, state = engine_session
    instruction = '改为检查另一条任务的删除按钮'
    original_spec = state.test_spec_hash
    contexts = []
    generate = engine.model.generate

    async def capture(schema, context, **kwargs):
        contexts.append(context)
        return await generate(schema, context, **kwargs)

    monkeypatch.setattr(engine.model, 'generate', capture)
    if paused:
        engine.control = 'pause'
        await session.drive(state)
        assert session.state().run_status == RunStatus.PAUSED
    else:
        engine.store.save(state)
        monkeypatch.setattr(session, 'active', lambda: True)
    await session.dispatch(instruction)
    if paused:
        assert session.task is None or session.task.done()
    else:
        monkeypatch.setattr(session, 'active', lambda: False)
        await session.drive(state)
    trace = engine.store.trace(state.run_id, state.scope_id)
    prompts = [event for event in trace if event['type'] == 'guidance.queued']
    assert len(prompts) == 1
    assert prompts[0]['payload']['level'] == 'retarget'
    assert prompts[0]['payload']['confirmed_at'] is None
    assert all(instruction not in str(context) for context in contexts)
    assert not any(event['type'] == 'run.retargeted' for event in trace)
    assert session.state().test_spec_hash == original_spec
    output = session.output.getvalue()
    assert '改目标 L3' in output and '/retarget confirm' in output
    if paused:
        assert session.state().run_status == RunStatus.PAUSED
