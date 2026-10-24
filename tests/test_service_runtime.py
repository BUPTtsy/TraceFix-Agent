import asyncio
import io
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from langgraph.checkpoint.memory import InMemorySaver

from tracefix.service.session import Session
from tracefix.connectors import GitHubMCPError
from tracefix.execution.workspace import commit_workspace, git
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.repository_registry import RepositoryRegistry, config_digest
from tracefix.rules import Rule, RuleLibrary
from tracefix.runtime.contracts import Outcome, RunState, digest
from tracefix.runtime.smoke import FakeBrowser, FakeModel, FakeRunner
from tracefix.service.config import ServiceSettings, validate_snapshot
from tracefix.service.contracts import ServiceRequest
from tracefix.service.finding import VerifiedFindingAcceptance, strict_acceptance_adapter
from tracefix.runtime.finding_acceptance import AcceptanceSpec
from tracefix.service.resources import RunServices
from tracefix.service.run import execute_request
from tracefix.storage.store import MemoryStore
from tests.test_github_mcp import mcp_mock


def test_service_terminal_uses_utf8_with_windows_output_encoding(service_fixture, monkeypatch):
    from tracefix.service import run
    from tracefix.service.results import failure_result

    fixture = service_fixture
    request = fixture.request()
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding='cp1252')
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(request.model_dump_json().encode('utf-8'))))
    monkeypatch.setattr(ServiceSettings, 'from_environment', lambda: fixture.settings)

    async def failed(*args):
        return {**failure_result(run_id=request.run_id), 'message': '运行基础设施不可用'}

    monkeypatch.setattr(run, 'run_with_signals', failed)
    assert run.main(['--request-stdin']) == 1
    stream.flush()
    encoded = output.getvalue()
    assert len(encoded.splitlines()) == 1
    assert json.loads(encoded.decode('utf-8'))['message'] == '运行基础设施不可用'


@pytest.fixture
def service_fixture(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    (root / 'src').mkdir(parents=True)
    (root / 'src/value.ts').write_text('export const persisted = false;\n', encoding='utf-8')
    git(root, 'init', '-q', '--initial-branch=main')
    git(root, 'add', 'src/value.ts')
    commit_workspace(root, '测试基线')
    git(root, 'remote', 'add', 'origin', 'https://github.com/owner/app.git')
    settings = ServiceSettings(tmp_path / 'data', tmp_path / 'console.sqlite3')
    rule_library = RuleLibrary(settings.console_db)
    for rule in rule_library.rules():
        rule_library.save_rule(rule.model_copy(update={'status': 'disabled'}), rule_id=rule.id)
    registry = RepositoryRegistry(tmp_path / 'profiles/projects.yaml',
                                  authorized_roots=[root], data_root=settings.data_root)
    registry.register({'repo_id': 'alpha', 'root': str(root), 'owner': 'owner', 'repo': 'app',
        'default_ref': 'main', 'allowed_files': ['src/**'], 'allowed_urls': ['http://app:3000'],
        'credential_ref': 'deployment/alpha',
        'profile': {'commands': {key: ['node', 'ci'] for key in ('start', 'reset', 'static', 'unit', 'build')}}})
    store, sessions, runners, browsers = MemoryStore(), [], [], []

    class FixtureSession(Session):
        def __init__(self, *args):
            super().__init__(*args)
            sessions.append(self)

        async def ensure_runtime(self):
            self.store, self.saver = store, InMemorySaver()

        def bind(self, *args):
            super().bind(*args)
            self.engine.subagent_enabled = False

    def runner(profile, workspace, run_id):
        resource = FakeRunner(digest(workspace.repository_snapshot))
        resource.workspace = workspace
        resource.run_id = run_id
        resource.browser_command = lambda: []
        runners.append(resource)
        return resource

    def browser(*args):
        resource = FakeBrowser(runners[-1].workspace, bugfree=not runners[-1].run_id.startswith('repair'))
        browsers.append(resource)
        return resource

    monkeypatch.setenv('TRACEFIX_API_KEY', 'fake-service-model-key')
    # repair 新 Run 必须冻结 SDK 配置；凭据只以 env 引用进入冻结配置。
    monkeypatch.setenv('TRACEFIX_CLAUDE_MODEL', 'claude-sonnet-4-6')
    monkeypatch.setenv('TRACEFIX_CLAUDE_API_KEY', 'fake-sdk-model-key')
    monkeypatch.setenv('TRACEFIX_SDK_WORKER_IMAGE', 'sha256:' + 'a' * 64)
    for name in ('TRACEFIX_CLAUDE_BASE_URL', 'TRACEFIX_CLAUDE_CREDENTIAL_REF', 'TRACEFIX_SDK_EGRESS_ALLOWLIST'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    monkeypatch.delenv('TRACEFIX_SESSION_REMOTE', raising=False)
    monkeypatch.setattr('tracefix.service.session.DockerRunner', runner)
    monkeypatch.setattr('tracefix.service.session.MCPBrowser', browser)
    monkeypatch.setattr('tracefix.service.session.Gateway', lambda: FakeModel(runners[-1].workspace))

    def request(run_id='run_alpha', **fields):
        snapshot = registry.freeze('alpha', run_id)
        payload = {'schema_version': 1, 'task_id': 'task_alpha', 'run_id': run_id, 'repo_id': 'alpha',
            'mode': 'test', 'goal': '检查页面操作结果', 'repository_snapshot': snapshot,
            'execution': {'mode': 'batch', 'spec': {'goal': '检查页面操作结果',
                'assertions': [{'locator': {'role': 'heading', 'name': 'Task board'}}],
                'regression_assertions': [{'locator': {'role': 'heading', 'name': 'Task board'}}]}}}
        payload.update(fields)
        return ServiceRequest.model_validate(payload)

    return SimpleNamespace(settings=settings, registry=registry, request=request, factory=FixtureSession,
        store=store, sessions=sessions, runners=runners, browsers=browsers, root=root)


async def test_service_uses_session_factory_and_managed_run_identity(service_fixture):
    fixture = service_fixture
    result = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory)
    assert result['terminal'] and result['run_id'] == 'run_alpha'
    assert result['publication'] is None
    assert result['summary'] == result['report']['result_summary']
    assert result['run_status'] == 'COMPLETED', result
    state = fixture.store.load('run_alpha', 'alpha')
    assert state.execution_mode == 'batch' and state.job_id == 'task_alpha'
    assert fixture.sessions[-1].engine.runtime_services.run_id == state.run_id
    record = DocumentLibrary(fixture.settings.console_db).run('run_alpha')
    assert record['agentRunId'] == state.run_id and record['projectId'] == 'alpha' and record['managed']
    assert fixture.runners[0].workspace.root != fixture.root
    assert fixture.runners[0].closed and fixture.browsers[0].closed
    original = deepcopy(record)
    repeated = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory)
    assert repeated['error']['code'] == 'run_identity_conflict'
    assert DocumentLibrary(fixture.settings.console_db).run('run_alpha') == original


def test_rehashed_forged_configuration_cannot_replace_deployment_freeze(service_fixture):
    fixture = service_fixture
    payload = fixture.request().model_dump(mode='json')
    snapshot = payload['repository_snapshot']
    config = snapshot['repository_config']
    config['profile']['commands']['start'] = ['malicious-command']
    config['config_digest'] = config_digest(config)
    config['snapshot_digest'] = digest({key: value for key, value in config.items() if key != 'snapshot_digest'})
    snapshot.update(config_digest=config['config_digest'], snapshot=deepcopy(config))
    request = ServiceRequest.model_validate(payload)
    with pytest.raises(Exception, match='部署侧冻结'):
        validate_snapshot(request, fixture.settings)


async def test_runtime_failure_finalizes_before_store_closes(service_fixture, monkeypatch):
    fixture = service_fixture

    async def fail(self, *args):
        raise RuntimeError('fake-service-model-key must never leak')

    monkeypatch.setattr(fixture.factory, 'drive', fail)
    result = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory)
    assert result['run_status'] == 'FAILED' and result['report_ref']
    assert 'fake-service-model-key' not in json.dumps(result)
    assert fixture.runners[0].closed and fixture.browsers[0].closed


async def test_service_preserves_unknown_browser_receipt_without_replay(service_fixture, monkeypatch):
    from tracefix.execution.browser import MCPActionUnknown, MCPConnectionError
    fixture = service_fixture
    attempts = []

    async def lose_receipt(browser, action):
        attempts.append(action.kind)
        raise MCPActionUnknown('service-unknown-action', action.kind, MCPConnectionError('lost receipt'))

    monkeypatch.setattr(FakeBrowser, 'action', lose_receipt)
    request = fixture.request()
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['run_status'] == 'FAILED' and result['error']['status'] == 'UNKNOWN_OPERATION'
    assert len(attempts) == 1
    assert any(operation['status'] == 'UNKNOWN' for operation in fixture.store.operations.values())
    repeated = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert repeated['error']['code'] == 'run_identity_conflict' and len(attempts) == 1


async def test_partial_factory_initialization_closes_browser_and_runner(service_fixture, monkeypatch):
    fixture = service_fixture

    def fail():
        raise RuntimeError('secret startup details')

    monkeypatch.setattr('tracefix.service.session.Gateway', fail)
    result = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory)
    assert result['run_status'] == 'FAILED' and result['report_ref']
    assert fixture.runners[0].closed and fixture.browsers[0].closed
    assert 'secret startup details' not in json.dumps(result)


async def test_required_github_failure_cannot_be_silently_skipped(service_fixture):
    fixture = service_fixture
    closed = []

    class FailingServices(RunServices):
        async def prepare(self):
            raise GitHubMCPError('permission_denied', 'do not include remote details')

        async def aclose(self):
            closed.append(True)
            await super().aclose()

    result = await execute_request(fixture.request(), fixture.settings,
        session_factory=fixture.factory, services_factory=FailingServices)
    assert result['run_status'] == 'FAILED' and result['error']['code'] == 'permission_denied'
    assert closed and not fixture.runners


async def test_cancelled_context_initialization_outputs_terminal_and_closes(service_fixture):
    fixture = service_fixture
    started, closed = asyncio.Event(), asyncio.Event()

    class WaitingServices(RunServices):
        async def prepare(self):
            started.set()
            await asyncio.Future()

        async def aclose(self):
            await super().aclose()
            closed.set()

    task = asyncio.create_task(execute_request(fixture.request(), fixture.settings,
        session_factory=fixture.factory, services_factory=WaitingServices))
    await started.wait()
    task.cancel()
    result = await task
    assert closed.is_set() and result['run_status'] == 'CANCELLED'
    assert result['outcome'] == 'INCONCLUSIVE' and result['report_ref']


async def create_source(fixture):
    library = RuleLibrary(fixture.settings.console_db)
    library.save_rule(Rule(id='feedback', name='保存状态必须持久化', status='enabled',
        scope={'project_ids': ['alpha']}, phases=['EXPLORE', 'DIAGNOSE', 'PATCH', 'VERIFY'],
        detection={'type': 'static', 'static': {'regex': 'persisted = false'}},
        fix_guidance='将真实保存结果持久化'))
    result = await execute_request(fixture.request('source_run', additional_rule_ids=['feedback']),
                                 fixture.settings, session_factory=fixture.factory)
    assert result['report_ref'], result
    finding = next(item for item in library.findings(run_id='source_run') if item.rule_id == 'feedback')
    return library, finding


def acceptance_payload(fixture, request, finding, *, allow_revision_rebind=False):
    source = fixture.store.load(finding.run_id, 'alpha')
    expected = finding.expected or finding.details.get('expected')
    actual = finding.actual or finding.details.get('actual')
    heading = {'locator': {'role': 'heading', 'name': 'Task board'}, 'condition': 'visible'}
    checkbox = {'locator': {'role': 'checkbox', 'name': 'Complete task'}, 'condition': 'checked'}
    navigate = {'action': {'kind': 'navigate', 'value': source.url}, 'assertions': [heading]}
    observe = {'action': {'kind': 'observe'}, 'assertions': [heading]}
    spec = AcceptanceSpec(goal=request.goal, assertions=[checkbox], regression_plan=[navigate['action']],
        regression_assertions=[heading], behavior_scenarios=[
            {'id': 'original', 'steps': [navigate, {'action': {'kind': 'observe'}, 'assertions': [checkbox]}]},
            {'id': 'normal', 'steps': [navigate, observe]},
            {'id': 'reverse', 'steps': [navigate, observe]},
            {'id': 'refresh', 'steps': [navigate, navigate]}])
    return {'schema_version': 1, 'run_id': request.run_id, 'repo_id': 'alpha',
        'commit': request.repository_snapshot.commit, 'source_run_id': source.run_id,
        'source_scope_id': 'alpha', 'source_repo_id': 'owner/app',
        'source_commit': finding.source_base_commit, 'source_manifest': source.source_manifest,
        'source_rule_snapshot_hash': source.rule_snapshot_hash, 'allow_revision_rebind': allow_revision_rebind,
        'findings': [{'finding_id': finding.id, 'fingerprint': finding.fingerprint,
            'expected': expected, 'actual': actual, 'rule_id': finding.rule_id, 'rule_version': finding.rule_version,
            'evidence_refs': finding.evidence_refs, 'original_scenario_id': 'original', 'guards': [
                {'kind': 'normal', 'scenario_id': 'normal'}, {'kind': 'reverse', 'scenario_id': 'reverse'},
                {'kind': 'refresh', 'scenario_id': 'refresh'}]}],
        'spec': spec.model_dump(mode='json'), 'spec_hash': digest(spec)}


def stop_before_sdk_coding(fixture, monkeypatch):
    """服务层只验证 SDK 编码前的来源重导出与绑定；编码本身需要真实 worker 与 PostgreSQL 权威存储。"""
    entered = []

    async def drive(self, state):
        entered.append(state.model_copy(deep=True))

    monkeypatch.setattr(fixture.factory, 'drive', drive)
    return entered


async def test_finding_repair_reexports_source_and_does_not_inherit_success(service_fixture, monkeypatch):
    fixture = service_fixture
    library, finding = await create_source(fixture)
    source = fixture.store.load('source_run', 'alpha')
    source.outcome, source.reproduced = Outcome.FIX_VERIFIED, True
    fixture.store.save(source)
    request = fixture.request('repair_run', mode='repair', source_run_id='source_run',
                              source_finding_ids=[finding.id])
    request.execution.spec.assertions = [request.execution.spec.assertions[0].model_copy(update={
        'locator': request.execution.spec.assertions[0].locator.model_copy(update={
            'role': 'checkbox', 'name': 'Complete task'}), 'condition': 'checked'})]
    entered = stop_before_sdk_coding(fixture, monkeypatch)
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    # 编码被截停，服务以未到终态收尾，不能把来源 Run 的成功当作本 Run 结论。
    assert result['error']['code'] == 'nonterminal_run' and result['outcome'] == 'INFRA_FAILURE'
    assert result['report_ref'], result
    assert [item.run_id for item in entered] == ['repair_run']
    handoff = entered[0]
    assert handoff.repair_source.source_run_id == 'source_run'
    assert fixture.runners[-1].workspace.root != fixture.runners[0].workspace.root
    assert handoff.repair_source.target_base_commit == request.repository_snapshot.commit
    assert not handoff.repair_source.revision_rebound
    # repair 只走 SDK：新 Run 冻结 SDK 配置，且不继承来源 Run 的复现与修复结论。
    assert handoff.coding_backend == 'claude_sdk' and handoff.coding_config_ref
    assert not handoff.reproduced and handoff.outcome is None and not handoff.patch_hash
    assert library.finding(finding.id).status != 'fixed'
    assert handoff.repair_source.findings[0]['fix_guidance'] == '将真实保存结果持久化'
    assert fixture.runners[-1].closed


def completed_sdk_job(session, state):
    """把当前 Run 投影为已通过最终累计验收的 SDK Job 子 Run，交付前的服务流程与真实 Harness 一致。"""
    from tracefix.runtime.contracts import Phase, RunStatus
    from tracefix.runtime.harness_contracts import HarnessJob
    from tracefix.storage.harness import MemoryHarnessStore
    authority = MemoryHarnessStore(session.artifacts)
    job = authority.create_job(HarnessJob(job_id='sdk-job', scope_id=state.scope_id, repo_id='owner/app',
        console_task_id='task_alpha', goal_hash=digest(state.goal), origin_base_commit='a' * 40,
        acceptance_refs=['spec']))
    authority.jobs[job.job_id].status = 'COMPLETED'
    session.engine.harness = SimpleNamespace(store=authority, job=lambda current: authority.get_job(
        current.harness_job_id, scope_id=current.scope_id))
    child = state.model_copy(deep=True, update={'run_id': 'sdk-current', 'harness_job_id': job.job_id,
        'harness_session_id': 'sdk-session', 'harness_stage': 'handoff', 'run_status': RunStatus.COMPLETED,
        'phase': Phase.FINALIZE, 'outcome': Outcome.FIX_VERIFIED})
    child.report_ref = session.artifacts.put(child.scope_id, child.run_id, {'run_id': child.run_id,
        'scope_id': child.scope_id, 'run_status': 'COMPLETED', 'outcome': 'FIX_VERIFIED',
        'patch_available': False, 'patch_verification': 'none'})
    session.store.save(child)
    session.state = lambda: session.store.load(child.run_id, child.scope_id)
    return authority


@pytest.mark.parametrize('failure', ['none', 'delivery', 'cleanup', 'store_close'])
async def test_sdk_delivery_runs_after_service_cleanup_and_before_store_close(service_fixture, monkeypatch, failure):
    from tracefix.service import run
    from tracefix.service.delivery import DeliveryError
    fixture = service_fixture
    events, calls, authorities = [], [], []
    original_runtime = fixture.factory.ensure_runtime

    async def runtime(self):
        await original_runtime(self)
        if getattr(self, 'store_close_registered', False):
            return
        # ensure_runtime 在一次 Run 中会被多次调用，存储关闭回调只登记一次。
        self.store_close_registered = True

        def close_store():
            events.append('store_close')
            if failure == 'store_close':
                raise RuntimeError('close failed')

        self.runtime_stack.callback(close_store)

    async def drive(self, state):
        authorities.append(completed_sdk_job(self, state))

    class Services(RunServices):
        async def aclose(self):
            await super().aclose()
            events.append('services_close')
            if failure == 'cleanup':
                self.cleanup_errors.append({'code': 'resource_close_failed'})

    publication = {'status': 'local', 'branch': 'codex/tracefix-repair_delivery', 'commit': 'd' * 40, 'error': None}
    record = {'approval_id': 'dlv_' + 'a' * 32, 'status': 'approved', 'job_id': 'sdk-job',
        'run_id': 'repair_delivery', 'scope_id': 'alpha', 'accepted_commit': 'b' * 40, 'patch_hash': 'c' * 64,
        'diff_ref': 'diff', 'publish_branch': False, 'require_approval': False,
        'requested_at': '2026-10-24T00:00:00+00:00', 'decision': 'approve', 'decided_by': 'auto',
        'decided_at': '2026-10-24T00:00:00+00:00', 'publication': publication}

    def deliver(**options):
        events.append('delivery')
        calls.append(options)
        if failure == 'delivery':
            raise DeliveryError('PATCH_HASH_MISMATCH')
        return record

    monkeypatch.setattr(fixture.factory, 'ensure_runtime', runtime)
    monkeypatch.setattr(fixture.factory, 'drive', drive)
    monkeypatch.setattr(run, 'open_delivery', deliver)
    request = fixture.request('repair_delivery', mode='repair')
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory, services_factory=Services)
    if failure == 'cleanup':
        # 资源清理失败时不交付，SDK Job 保持可恢复而不是终态。
        assert events == ['services_close', 'store_close'] and not calls
        assert result['terminal'] is False and 'CLEANUP_FAILED' in result['resume_blockers']
        return
    assert events == ['services_close', 'delivery', 'store_close']
    # 交付使用 Harness 权威存储、冻结仓库配置与部署凭据解析，而不是请求中的任意字段。
    options = calls[0]
    assert options['authority'] is authorities[0] and options['artifacts'] is fixture.sessions[-1].artifacts
    assert (options['data_root'], options['scope_id'], options['job_id'], options['run_id']) == (
        fixture.settings.data_root, 'alpha', 'sdk-job', 'repair_delivery')
    assert options['repository_config'] == request.repository_snapshot.repository_config
    assert options['resolve_credential'] == fixture.settings.resolve_credential
    assert result['run_status'] == 'COMPLETED' and result['terminal'] is True
    console = DocumentLibrary(fixture.settings.console_db).run('repair_delivery')
    assert console['status'] == 'completed' and console['publication'] == result['publication']
    if failure == 'delivery':
        assert result['publication'] == {'status': 'failed', 'branch': None, 'commit': None,
                                         'error': {'code': 'PATCH_HASH_MISMATCH'}}
        assert result['approval'] is None
        return
    assert result['publication'] == publication and result['approval'] is None
    assert (result['branch'], result['commit']) == (publication['branch'], publication['commit'])
    if failure == 'store_close':
        assert result['cleanup_errors'] == [{'code': 'runtime_close_failed'}]
    else:
        assert 'cleanup_errors' not in result


async def test_unverified_json_acceptance_and_implicit_revision_change_rejected(service_fixture):
    fixture = service_fixture
    _, finding = await create_source(fixture)
    with pytest.raises(ValidationError):
        fixture.request('repair_untrusted', mode='repair', source_run_id='source_run',
            source_finding_ids=[finding.id], finding_acceptance={'verified': True})
    (fixture.root / 'src/value.ts').write_text('export const persisted = false;\nexport const revision = 2;\n')
    git(fixture.root, 'add', 'src/value.ts')
    commit_workspace(fixture.root, '显式新版本')
    request = fixture.request('repair_new_revision', mode='repair', source_run_id='source_run',
                              source_finding_ids=[finding.id])
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['error']['code'] == 'revision_rebind_required'
    assert len(fixture.runners) == 1


async def test_explicit_revision_change_requires_typed_bound_acceptance(service_fixture, monkeypatch):
    fixture = service_fixture
    _, finding = await create_source(fixture)
    stop_before_sdk_coding(fixture, monkeypatch)
    (fixture.root / 'src/value.ts').write_text('export const persisted = false;\nexport const revision = 2;\n')
    git(fixture.root, 'add', 'src/value.ts')
    commit_workspace(fixture.root, '重新冻结验收版本')
    request = fixture.request('repair_rebound', mode='repair', source_run_id='source_run',
        source_finding_ids=[finding.id])
    payload = acceptance_payload(fixture, request, finding, allow_revision_rebind=True)
    request = ServiceRequest.model_validate({**request.model_dump(mode='json'), 'finding_acceptance': payload,
        'execution': {'executionMode': 'batch', 'spec': payload['spec']}})

    def accepted(request, state, snapshot):
        return strict_acceptance_adapter(request, state, snapshot)

    result = await execute_request(request, fixture.settings, session_factory=fixture.factory,
                                   acceptance_adapter=accepted)
    state = fixture.store.load(request.run_id, request.repo_id)
    assert state.repair_source.revision_rebound and state.repair_source.target_base_commit == request.repository_snapshot.commit
    assert result['report_ref'] and len(fixture.runners) == 2
    assert fixture.sessions[-1].engine.runtime_services.finding_acceptance.commit == request.repository_snapshot.commit


@pytest.mark.parametrize('damage', ['wrong_source', 'cross_repo', 'corrupt_hash', 'ambiguous_identity'])
async def test_service_rejects_source_before_new_workspace(service_fixture, damage):
    fixture = service_fixture
    library, finding = await create_source(fixture)
    if damage in {'wrong_source', 'cross_repo'}:
        finding = library.save_finding(finding.model_copy(update={'id': 'other_finding',
            'run_id' if damage == 'wrong_source' else 'repo_id': 'another'}))
    elif damage == 'corrupt_hash':
        session = fixture.sessions[-1]
        state = fixture.store.load('source_run', 'alpha')
        session.artifacts._path('alpha', 'source_run', state.observation_ref).write_bytes(b'corrupted')
    else:
        DocumentLibrary(fixture.settings.console_db).update_run('duplicate', {
            'projectId': 'alpha', 'agentRunId': 'source_run'}, create=True)
    request = fixture.request('repair_rejected', mode='repair', source_run_id='source_run',
                              source_finding_ids=[finding.id])
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['error']['code'] == 'finding_source_invalid' and len(fixture.runners) == 1


async def test_no_engine_context_consumption_is_reported_as_integration_failure(service_fixture):
    fixture = service_fixture

    class CachedServices(RunServices):
        async def prepare(self):
            self._prepared = True

        async def reference_documents(self, state):
            return []

    result = await execute_request(fixture.request(github_reads=[{'source_kind': 'commit'}]),
        fixture.settings, session_factory=fixture.factory, services_factory=CachedServices)
    assert result['run_status'] == 'FAILED' and result['error']['code'] == 'runtime_services_not_integrated'


async def test_missing_database_still_publishes_managed_terminal(service_fixture):
    fixture = service_fixture
    result = await execute_request(fixture.request(), fixture.settings)
    assert result['error']['code'] == 'configuration_missing' and result['terminal']
    record = DocumentLibrary(fixture.settings.console_db).run('run_alpha')
    assert record['managed'] and record['status'] == 'failed' and record['pid'] is None


async def test_cross_scope_internal_run_is_never_overwritten(service_fixture):
    fixture = service_fixture
    original = RunState(run_id='run_alpha', scope_id='another_scope', goal='保护已有任务状态', url='http://app:3000')
    fixture.store.save(original)
    result = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory)
    assert result['error']['code'] == 'run_identity_conflict'
    assert fixture.store.load('run_alpha', 'another_scope') == original


async def test_cleanup_failure_updates_authoritative_terminal(service_fixture):
    fixture = service_fixture

    class FailedCloseServices(RunServices):
        async def aclose(self):
            await super().aclose()
            self.cleanup_errors.append({'code': 'resource_close_failed'})

    result = await execute_request(fixture.request(), fixture.settings, session_factory=fixture.factory,
                                   services_factory=FailedCloseServices)
    assert result['run_status'] == 'FAILED' and result['error']['code'] == 'cleanup_failed'
    assert fixture.store.load('run_alpha', 'alpha').outcome == Outcome.INFRA_FAILURE
    assert result['report']['outcome'] == 'INFRA_FAILURE'


async def test_c_context_seam_combines_cached_reads_with_engine_calls(service_fixture, mcp_mock, monkeypatch):
    from tracefix.runtime.engine import Engine
    fixture = service_fixture
    settings = replace(fixture.settings, credential_env={'deployment/alpha': 'BETA_GITHUB_SECRET'})
    monkeypatch.setenv('BETA_GITHUB_SECRET', 'only-in-deployment')
    original = Engine.model_call
    consumed = []

    async def integrate(engine, state, schema, context, *args, **kwargs):
        documents = await engine.runtime_services.reference_documents(state)
        context = {**context, 'reference_documents': [*context.get('reference_documents', []), *documents]}
        consumed.extend(documents)
        return await original(engine, state, schema, context, *args, **kwargs)

    monkeypatch.setattr(Engine, 'model_call', integrate)
    result = await execute_request(fixture.request(github_reads=[{'source_kind': 'file', 'path': 'src/value.ts'}]),
                                   settings, session_factory=fixture.factory)
    assert result['run_status'] == 'COMPLETED' and consumed
    assert all(item['untrusted'] and item['provenance']['run_id'] == 'run_alpha' for item in consumed)
    assert sum(name == 'get_file_contents' for name, _ in mcp_mock.sessions[0].calls) == 1
    assert mcp_mock.sessions[0].closed


def test_valid_stdin_request_emits_terminal_on_configuration_failure(service_fixture):
    fixture = service_fixture
    request = fixture.request()
    environment = {**os.environ, 'TRACEFIX_SERVICE_DATA_ROOT': str(fixture.settings.data_root),
                   'TRACEFIX_SERVICE_CONSOLE_DB': str(fixture.settings.console_db)}
    environment.pop('TRACEFIX_SERVICE_DATABASE_URL', None)
    process = subprocess.run([sys.executable, '-m', 'tracefix.service.run', '--request-stdin'],
        input=request.model_dump_json().encode('utf-8'), capture_output=True, env=environment, timeout=30)
    assert process.returncode == 1 and not process.stderr and len(process.stdout.splitlines()) == 1
    result = json.loads(process.stdout)
    assert result['task_id'] == request.task_id and result['run_id'] == request.run_id
    assert result['error']['code'] == 'configuration_missing'


@pytest.mark.parametrize('payload', [b'{broken', b'{"token":"secret"}', b'[]', b'a' * 2_097_153],
                         ids=['malformed', 'secret', 'array', 'oversized'])
def test_request_stdin_needs_no_tty_and_emits_one_machine_terminal(payload):
    result = subprocess.run([sys.executable, '-m', 'tracefix.service.run', '--request-stdin'],
                            input=payload, capture_output=True, timeout=30)
    assert result.returncode == 1 and not result.stderr
    assert len(result.stdout.splitlines()) == 1
    terminal = json.loads(result.stdout)
    assert terminal['terminal'] and terminal['error']['code'] == 'invalid_request'
