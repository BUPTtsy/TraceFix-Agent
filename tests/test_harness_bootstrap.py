from types import SimpleNamespace

import pytest

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.runtime.harness_bootstrap import create_harness_job
from tracefix.runtime.smoke import make_engine
from tracefix.storage.harness import MemoryHarnessStore


def _config():
    return ClaudeSdkConfig(backend='claude_sdk', model_id='claude-sonnet-4-6',
        credential_ref='env:HARNESS_BOOTSTRAP_KEY', worker_image='sha256:' + 'a' * 64,
        egress_allowlist=['https://api.anthropic.com'])


def _create(engine, state, **overrides):
    options = {'console_task_id': 'task', 'local_commit_authorized': True,
               'config': _config(), 'network': 'harness-egress', 'egress_proxy': 'http://proxy:8080'}
    options.update(overrides)
    return create_harness_job(engine, state, **options)


def test_unauthorized_job_rejects_before_attaching_runtime(tmp_path):
    engine, state = make_engine(tmp_path)
    original = state.model_dump(mode='json')
    with pytest.raises(PermissionError, match='已授权本地提交'):
        _create(engine, state, local_commit_authorized=False)
    assert state.model_dump(mode='json') == original
    assert getattr(engine, 'harness', None) is None


def test_explicit_job_attaches_authority_runner_and_sqlite_projection(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_BOOTSTRAP_KEY', 'fake-secret')
    engine, state = make_engine(tmp_path / 'engine')
    engine.store.save(state)
    authority = MemoryHarnessStore(engine.artifacts)
    observed = []

    def factory(workspace, **configuration):
        observed.append(configuration)
        return SimpleNamespace(workspace=workspace)

    created = _create(engine, state, store=authority, runner_factory=factory)
    assert created.harness_job_id
    assert engine.harness.store is authority
    assert engine.harness.root == engine.artifacts.root.parent / 'harness'
    assert (engine.artifacts.root.parent / 'harness-projection.sqlite3').is_file()
    engine.harness.runner(SimpleNamespace())
    assert observed == [{'network': 'harness-egress', 'egress_proxy': 'http://proxy:8080'}]


def test_production_bootstrap_refuses_memory_store(tmp_path, monkeypatch):
    monkeypatch.setenv('HARNESS_BOOTSTRAP_KEY', 'fake-secret')
    engine, state = make_engine(tmp_path / 'engine')
    with pytest.raises(RuntimeError, match='真实 PostgreSQL'):
        _create(engine, state)
    assert not getattr(engine, 'harness', None)


@pytest.mark.parametrize('overrides', [
    {'local_commit_authorized': False}, {'network': ''}, {'egress_proxy': ''},
    {'egress_proxy': 'http://secret:password@proxy:8080'},
])
def test_preflight_rejects_missing_authorization_and_egress(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv('HARNESS_BOOTSTRAP_KEY', 'fake-secret')
    engine, state = make_engine(tmp_path / 'engine')
    with pytest.raises(PermissionError):
        _create(engine, state, store=MemoryHarnessStore(engine.artifacts), **overrides)
    assert not getattr(engine, 'harness', None)


def test_missing_credential_and_frozen_spec_are_rejected_before_job_creation(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'engine')
    authority = MemoryHarnessStore(engine.artifacts)
    monkeypatch.delenv('HARNESS_BOOTSTRAP_KEY', raising=False)
    with pytest.raises(ValueError, match='凭据'):
        _create(engine, state, store=authority)
    monkeypatch.setenv('HARNESS_BOOTSTRAP_KEY', 'fake-secret')
    with pytest.raises(PermissionError, match='摘要'):
        _create(engine, state.model_copy(update={'test_spec_hash': 'forged'}), store=authority)
    assert not authority.jobs


def test_finding_service_preflight_runs_before_job_attachment(tmp_path, monkeypatch):
    from tests.test_harness_finding import finding_case
    engine, state, child, source = finding_case.__wrapped__(tmp_path)
    engine.runtime_services = SimpleNamespace(run_id=state.run_id, finding_acceptance=object())
    authority = MemoryHarnessStore(engine.artifacts)
    monkeypatch.setenv('HARNESS_BOOTSTRAP_KEY', 'fake-secret')
    with pytest.raises(PermissionError, match='专属验收服务'):
        _create(engine, state, store=authority)
    assert not authority.jobs and not getattr(engine, 'harness', None)
