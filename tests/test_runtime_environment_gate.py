"""S1-07 fake protocol tests, not a live Docker Oracle."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from tracefix.execution.runner import DockerRunner
from tracefix.config import Profile
from tracefix.runtime.contracts import Outcome, Phase, RunStatus, Validation, digest
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.verification import verify_artifacts


async def test_runtime_gate_rejects_inspection_failure_and_propagates_cancel(tmp_path):
    engine, state = make_engine(tmp_path)
    await engine.runner.start(state.source_manifest)

    async def fail(*, runtime=False):
        assert runtime
        raise RuntimeError('container inspect failed')

    engine.runner.inspect_images = fail
    assert not await engine.runtime_verification_passed(state)

    async def cancel(*, runtime=False):
        assert runtime
        raise asyncio.CancelledError()

    engine.runner.inspect_images = cancel
    with pytest.raises(asyncio.CancelledError):
        await engine.runtime_verification_passed(state)


@pytest.mark.parametrize('execution_mode', ['interactive', 'batch'])
async def test_verify_live_gate_fails_once_and_finalizes_unverified_patch(tmp_path, execution_mode):
    engine, state = make_engine(tmp_path)
    state.execution_mode = execution_mode
    engine.runner.inspect_results = ['fake-ci-environment', 'permanent-drift']
    await engine.run(state)
    finished = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(finished, finished.report_ref)
    assert engine.runner.inspect_calls == 2
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['terminal_reason'] == 'runtime_environment_gate_failed'
    assert finished.validation_refs and finished.approval_ref is None
    assert report['patch_verification'] == 'unverified'
    assert engine.artifacts.read(state.scope_id, state.run_id, report['patch_diff_ref'])
    assert engine.runner.closed and engine.browser.closed


async def test_docker_runner_uses_immutable_ids_and_live_container_images(tmp_path):
    engine, state = make_engine(tmp_path)
    runner = DockerRunner(engine.profile, engine.workspace, 'live-images')
    app_id, browser_id = APP_IMAGE, BROWSER_IMAGE
    calls = []
    prebound_browser_command = runner.browser_command()

    async def startup_docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('container', 'inspect'):
            return {'passed': False, 'exit_code': 1, 'output': ''}
        if args[:2] == ('image', 'inspect'):
            return {'passed': True, 'exit_code': 0, 'output': json.dumps([
                {'Id': app_id}, {'Id': browser_id}])}
        return {'passed': True, 'exit_code': 0, 'output': ''}

    runner.docker = startup_docker
    expected_start_digest = await runner.inspect_images()
    assert expected_start_digest == digest({
        'profile': engine.profile.model_dump(), 'image_ids': [app_id, browser_id]})
    assert runner.resolved_image_ids == {'app': app_id, 'browser': browser_id}
    await runner.start(state.source_manifest)
    run_call = next(call for call in calls if call[0] == 'run')
    assert app_id in run_call and engine.profile.image not in run_call
    browser_call = runner.browser_command()
    assert browser_call is prebound_browser_command
    assert browser_id in browser_call and engine.profile.browser_image not in browser_call
    assert 'tracefix.run=live-images' in browser_call

    async def live_docker(*args, **kwargs):
        calls.append(args)
        assert args == ('container', 'inspect', runner.name, runner.name+'-browser')
        return {'passed': True, 'exit_code': 0, 'output': json.dumps(container_records(runner))}

    runner.docker = live_docker
    live_digest = await runner.inspect_images(runtime=True)
    assert live_digest == expected_start_digest
    assert runner.actual_digest == live_digest


async def test_docker_runner_rejects_wrong_live_image_or_run_identity(tmp_path):
    engine, state = make_engine(tmp_path)
    runner = DockerRunner(engine.profile, engine.workspace, 'identity-check')

    async def docker(*args, **kwargs):
        records = container_records(runner)
        records[0]['Config']['Labels']['tracefix.run'] = 'other-run'
        records[1]['State']['Running'] = False
        return {'passed': True, 'exit_code': 0, 'output': json.dumps(records)}

    runner.docker = docker
    with pytest.raises(RuntimeError, match='身份或状态'):
        await runner.inspect_images(runtime=True)


APP_IMAGE = 'sha256:' + 'a' * 64
BROWSER_IMAGE = 'sha256:' + 'b' * 64
OTHER_IMAGE = 'sha256:' + 'c' * 64


def container_records(runner):
    return [{'Id': name+'-id', 'Name': '/'+name, 'Image': image_id,
        'State': {'Running': True}, 'Config': {'Image': 'mutable:tag',
            'Labels': {'tracefix.run': runner.run_id}},
        'HostConfig': {'NetworkMode': runner.network},
        'NetworkSettings': {'Networks': {runner.network: {}}}}
        for name, image_id in ((runner.name, APP_IMAGE), (runner.name+'-browser', BROWSER_IMAGE))]


@pytest.fixture
def docker_runner(tmp_path):
    profile = Profile(project='ci', source_commit='HEAD',
        commands={kind: ['node', kind] for kind in ('start', 'reset', 'static', 'unit', 'build')})
    snapshot = {'fixture': 'host-owned'}
    workspace = SimpleNamespace(root=tmp_path, require_repository_snapshot=lambda: snapshot,
        check_frozen=lambda bound: None, prepare_mountpoints=lambda: None)
    return DockerRunner(profile, workspace, 'live-images')


@pytest.fixture
async def verified_engine(tmp_path):
    engine, state = make_engine(tmp_path)
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.run_status == RunStatus.WAITING_APPROVAL, state.error
    assert engine.verification_passed(state)
    return engine, state


async def test_runtime_gate_requires_strict_new_return_and_offline_checker_is_separate(verified_engine):
    engine, state = verified_engine
    expected = state.environment_digest
    before = engine.runner.inspect_calls
    for invalid in ('different-environment', '', None, True, {}, 1):
        engine.runner.actual_digest = expected
        engine.runner.inspect_results = [invalid]
        assert not await engine.runtime_verification_passed(state)
        assert state.environment_digest == expected
    assert engine.runner.inspect_calls == before + 6
    validations = [Validation(**engine.get(state, ref)) for ref in state.validation_refs]
    assert verify_artifacts(state, validations, lambda ref: engine.bundle_exists(state, ref),
        lambda ref: engine.get(state, ref),
        lambda ref: engine.artifacts.read(state.scope_id, state.run_id, ref))


async def test_runtime_gate_handles_timeout_and_propagates_cancel(verified_engine):
    engine, state = verified_engine
    for error in (RuntimeError('inspect failed'), asyncio.TimeoutError(), ValueError('bad JSON')):
        engine.runner.actual_digest = state.environment_digest
        engine.runner.inspect_results = [error]
        assert not await engine.runtime_verification_passed(state)
    engine.runner.inspect_results = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await engine.runtime_verification_passed(state)


@pytest.mark.parametrize('fault', ['diff', 'frozen', 'artifact'])
async def test_live_sample_cannot_bypass_workspace_or_typed_evidence(verified_engine, monkeypatch, fault):
    engine, state = verified_engine
    if fault == 'diff':
        monkeypatch.setattr(engine.workspace, 'diff', lambda base='HEAD': 'changed patch')
    elif fault == 'frozen':
        def reject(snapshot):
            raise PermissionError('frozen workspace changed')
        monkeypatch.setattr(engine.workspace, 'check_frozen', reject)
    else:
        wrapper = engine.get(state, state.validation_refs[0])
        result = engine.get(state, wrapper['artifact_ref'])
        result['passed'] = False
        wrapper['artifact_ref'] = engine.put(state, result)
        state.validation_refs[0] = engine.put(state, wrapper)
    before = engine.runner.inspect_calls
    assert not await engine.runtime_verification_passed(state)
    assert engine.runner.inspect_calls == before + 1


async def test_review_rejects_inspect_failure_before_consuming_approval(verified_engine, monkeypatch):
    engine, state = verified_engine
    engine.store.decide_approval(state.approval_ref, state, 'approve')
    original_head = engine.workspace.head()
    engine.runner.inspect_results = [RuntimeError('container missing')]
    monkeypatch.setattr(engine.store, 'consume_approval', lambda *args: pytest.fail('must not consume approval'))
    await engine.run(resume='approve')
    finished = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(finished, finished.report_ref)
    assert finished.outcome == Outcome.INFRA_FAILURE
    assert finished.error_details['terminal_reason'] == 'approval_verification_invalid'
    assert engine.workspace.head() == original_head and not finished.local_branch
    assert report['patch_verification'] == 'unverified'
    assert engine.runner.closed and engine.browser.closed


async def test_review_and_finalize_use_new_samples_not_stale_cache(verified_engine):
    engine, state = verified_engine
    calls = []

    async def inspect(*, runtime=False):
        assert runtime and not engine.runner.closed and not engine.browser.closed
        calls.append('live')
        return state.environment_digest

    engine.runner.inspect_images = inspect
    engine.runner.actual_digest = 'stale-cache'
    engine.store.decide_approval(state.approval_ref, state, 'reject')
    await engine.run(resume='reject')
    finished = engine.store.load(state.run_id, state.scope_id)
    assert calls == ['live', 'live'] and finished.outcome == Outcome.FIX_VERIFIED
    assert engine.get(finished, finished.report_ref)['patch_verification'] == 'verified'
    assert engine.runner.closed and engine.browser.closed


async def test_finalize_rejects_new_drift_before_cleanup(verified_engine):
    engine, state = verified_engine
    calls = []

    async def inspect(*, runtime=False):
        assert runtime and not engine.runner.closed and not engine.browser.closed
        calls.append('live')
        return 'new-drift'

    engine.runner.inspect_images = inspect
    state.phase, state.run_status = Phase.FINALIZE, RunStatus.RUNNING
    engine.store.save(state)
    await engine.finalize(state, None)
    finished = engine.store.load(state.run_id, state.scope_id)
    assert calls == ['live'] and finished.outcome == Outcome.INFRA_FAILURE
    assert engine.get(finished, finished.report_ref)['patch_verification'] == 'unverified'
    assert engine.runner.closed and engine.browser.closed


async def test_restored_runner_samples_actual_images_without_resolving_tags(docker_runner):
    runner = docker_runner
    command = runner.browser_command()
    records = container_records(runner)
    calls = []

    async def docker(*args, **kwargs):
        calls.append(args)
        assert args == ('container', 'inspect', runner.name, runner.name+'-browser')
        return {'passed': True, 'exit_code': 0, 'output': json.dumps(records)}

    runner.docker = docker
    expected = digest({'profile': runner.profile.model_dump(), 'image_ids': [APP_IMAGE, BROWSER_IMAGE]})
    assert await runner.inspect_images(runtime=True) == expected
    assert BROWSER_IMAGE in command and len(calls) == 1
    records[0]['Image'] = OTHER_IMAGE
    assert await runner.inspect_images(runtime=True) != expected
    assert runner.resolved_image_ids == {'app': APP_IMAGE, 'browser': BROWSER_IMAGE}


async def test_cleanup_retains_pins_for_continuation_but_live_never_falls_back(docker_runner):
    runner = docker_runner
    runner._bind_image_ids([APP_IMAGE, BROWSER_IMAGE])
    runner._started = True
    runner.actual_digest = 'old-sample'
    calls = []

    async def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ('container', 'inspect'):
            raise RuntimeError('containers already removed')
        return {'passed': True, 'exit_code': 0, 'output': ''}

    runner.docker = docker
    await runner.close()
    assert calls == [
        ('rm', '-f', '-v', runner.name),
        ('rm', '-f', runner.name + '-browser'),
        ('volume', 'rm', runner.dependency_volume),
        ('network', 'rm', runner.network),
    ]
    assert not runner._started and runner.actual_digest == ''
    assert runner.resolved_image_ids == {'app': APP_IMAGE, 'browser': BROWSER_IMAGE}
    expected = digest({'profile': runner.profile.model_dump(), 'image_ids': [APP_IMAGE, BROWSER_IMAGE]})
    assert await runner.inspect_images() == expected
    with pytest.raises(RuntimeError, match='removed'):
        await runner.inspect_images(runtime=True)
    assert not any(call[:2] == ('image', 'inspect') for call in calls)
    await runner.start(digest(runner.workspace.require_repository_snapshot()))
    assert runner._started and APP_IMAGE in next(call for call in calls if call[0] == 'run')


async def test_start_failure_removes_dependency_volume_and_cleanup_is_idempotent(docker_runner):
    runner = docker_runner
    runner._bind_image_ids([APP_IMAGE, BROWSER_IMAGE])
    calls = []

    async def docker(*args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == 'run':
            raise RuntimeError('app start failed')
        return {'passed': True, 'exit_code': 0, 'output': ''}

    runner.docker = docker
    with pytest.raises(RuntimeError, match='app start failed'):
        await runner.start(digest(runner.workspace.require_repository_snapshot()))
    assert (('volume', 'create', '--label', 'tracefix.run=' + runner.run_id,
             runner.dependency_volume), {}) in calls
    calls.clear()
    await runner.close()
    await runner.close()
    expected = [
        (('rm', '-f', '-v', runner.name), {'check': False}),
        (('rm', '-f', runner.name + '-browser'), {'check': False}),
        (('volume', 'rm', runner.dependency_volume), {'check': False}),
        (('network', 'rm', runner.network), {'check': False}),
    ]
    assert calls == expected * 2
    assert not runner._started and runner.actual_digest == ''


@pytest.mark.parametrize('role', [0, 1])
@pytest.mark.parametrize('fault', ['image', 'running', 'label', 'name', 'id', 'network', 'network_mode'])
async def test_runtime_container_identity_and_state_fail_closed(docker_runner, role, fault):
    runner = docker_runner
    records = copy.deepcopy(container_records(runner))
    record = records[role]
    if fault == 'image':
        record['Image'] = 'mutable:tag'
    elif fault == 'running':
        record['State']['Running'] = 1
    elif fault == 'label':
        record['Config']['Labels']['tracefix.run'] = 'other-run'
    elif fault == 'name':
        record['Name'] = '/other-container'
    elif fault == 'id':
        record.pop('Id')
    elif fault == 'network':
        record['NetworkSettings']['Networks'] = {}
    else:
        record['HostConfig']['NetworkMode'] = 'other-network'

    async def docker(*args, **kwargs):
        return {'passed': True, 'exit_code': 0, 'output': json.dumps(records)}

    runner.docker = docker
    with pytest.raises(RuntimeError):
        await runner.inspect_images(runtime=True)
    assert runner.actual_digest == ''


@pytest.mark.parametrize('output', ['not JSON', '{}', '[]', '[{}]'])
async def test_runtime_missing_containers_never_fall_back_to_tags(docker_runner, output):
    runner = docker_runner
    calls = []

    async def docker(*args, **kwargs):
        calls.append(args)
        assert args[:2] == ('container', 'inspect')
        return {'passed': True, 'exit_code': 0, 'output': output}

    runner.docker = docker
    with pytest.raises((RuntimeError, ValueError)):
        await runner.inspect_images(runtime=True)
    assert len(calls) == 1 and runner.resolved_image_ids is None


async def test_untrusted_snapshot_rejects_before_docker_or_profile_access(docker_runner):
    runner = docker_runner
    runner.profile = SimpleNamespace()

    def reject():
        raise PermissionError('invalid snapshot')

    async def docker(*args, **kwargs):
        pytest.fail('untrusted snapshot must not dispatch Docker')

    runner.workspace.require_repository_snapshot = reject
    runner.docker = docker
    with pytest.raises(PermissionError, match='snapshot'):
        await runner.start('wrong')
    with pytest.raises(PermissionError, match='snapshot'):
        await runner.inspect_images()
