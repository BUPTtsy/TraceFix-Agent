import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tracefix.cli.main import Session
from tracefix.connectors import GitHubMCPError
from tracefix.execution.workspace import commit_workspace, git
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.repository_registry import RepositoryRegistry, config_digest
from tracefix.rules import Rule, RuleLibrary
from tracefix.runtime.contracts import Outcome, digest
from tracefix.runtime.smoke import FakeBrowser, FakeModel, FakeRunner
from tracefix.service.config import ServiceSettings, validate_snapshot
from tracefix.service.contracts import ServiceRequest
from tracefix.service.finding import VerifiedFindingAcceptance
from tracefix.service.resources import RunServices
from tracefix.service.run import execute_request
from tracefix.storage.store import MemoryStore


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
        resource.browser_command = lambda: []
        runners.append(resource)
        return resource

    def browser(*args):
        resource = FakeBrowser(runners[-1].workspace, bugfree=True)
        browsers.append(resource)
        return resource

    monkeypatch.setenv('TRACEFIX_API_KEY', 'fake-service-model-key')
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    monkeypatch.delenv('TRACEFIX_SESSION_REMOTE', raising=False)
    monkeypatch.setattr('tracefix.cli.main.DockerRunner', runner)
    monkeypatch.setattr('tracefix.cli.main.MCPBrowser', browser)
    monkeypatch.setattr('tracefix.cli.main.Gateway', lambda: FakeModel(runners[-1].workspace))

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


async def test_partial_factory_initialization_closes_browser_and_runner(service_fixture, monkeypatch):
    fixture = service_fixture

    def fail():
        raise RuntimeError('secret startup details')

    monkeypatch.setattr('tracefix.cli.main.Gateway', fail)
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


async def test_finding_repair_reexports_source_and_does_not_inherit_success(service_fixture):
    fixture = service_fixture
    library, finding = await create_source(fixture)
    source = fixture.store.load('source_run', 'alpha')
    source.outcome, source.reproduced = Outcome.FIX_VERIFIED, True
    fixture.store.save(source)
    request = fixture.request('repair_run', mode='repair', source_run_id='source_run',
                              source_finding_ids=[finding.id])
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['report_ref'], result
    state = fixture.store.load('repair_run', 'alpha')
    assert state.repair_source.source_run_id == 'source_run'
    assert fixture.runners[-1].workspace.root != fixture.runners[0].workspace.root
    assert state.repair_source.target_base_commit == request.repository_snapshot.commit
    assert not state.repair_source.revision_rebound
    assert state.repair_source.findings[0]['fix_guidance'] == '将真实保存结果持久化'
    assert fixture.runners[-1].closed


async def test_unverified_json_acceptance_and_implicit_revision_change_rejected(service_fixture):
    fixture = service_fixture
    _, finding = await create_source(fixture)
    request = fixture.request('repair_untrusted', mode='repair', source_run_id='source_run',
        source_finding_ids=[finding.id], finding_acceptance={'verified': True})
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['error']['code'] == 'finding_acceptance_unavailable'
    (fixture.root / 'src/value.ts').write_text('export const persisted = false;\nexport const revision = 2;\n')
    git(fixture.root, 'add', 'src/value.ts')
    commit_workspace(fixture.root, '显式新版本')
    request = fixture.request('repair_new_revision', mode='repair', source_run_id='source_run',
                              source_finding_ids=[finding.id])
    result = await execute_request(request, fixture.settings, session_factory=fixture.factory)
    assert result['error']['code'] == 'revision_rebind_required'
    assert len(fixture.runners) == 1


@pytest.mark.parametrize('payload', [b'{broken', b'{"token":"secret"}', b'[]', b'a' * 2_097_153],
                         ids=['malformed', 'secret', 'array', 'oversized'])
def test_request_stdin_needs_no_tty_and_emits_one_machine_terminal(payload):
    result = subprocess.run([sys.executable, '-m', 'tracefix.service.run', '--request-stdin'],
                            input=payload, capture_output=True, timeout=30)
    assert result.returncode == 1 and not result.stderr
    assert len(result.stdout.splitlines()) == 1
    terminal = json.loads(result.stdout)
    assert terminal['terminal'] and terminal['error']['code'] == 'invalid_request'
