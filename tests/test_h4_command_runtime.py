"""非 GUI 命令目标的冻结、稳定复现与独立验证专项。"""
import copy
import json

import pytest
from pydantic import ValidationError

from tracefix.execution.runner import DockerRunner
from tracefix.model.contracts import ModelResult
from tracefix.runtime.contracts import (FileEdit, FinishRepair, Outcome, PatchProposal, Phase,
                                       RunState, RunStatus, TestSpec, Validation, digest)
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.command_tests import baseline_reproduced, frozen_plan
from tracefix.service.contracts import ExecutionRequest


class Commands:
    environment_digest = 'command-environment'

    def __init__(self, workspace, source, target, *, flaky=False, infrastructure=False):
        self.workspace, self.source, self.target = workspace, source, target
        self.calls, self.actual_digest = [], ''
        self.flaky, self.infrastructure, self.command_only = flaky, infrastructure, False
        self._started = False
        self.force_fail = None

    async def inspect_images(self, *, runtime=False):
        self.calls.append(('inspect', runtime))
        self.actual_digest = self.environment_digest
        return self.actual_digest

    async def start_commands(self, source):
        assert source == self.source
        self.calls.append(('start_commands', source))
        self._started = True
        return {'passed': True}

    async def command(self, target):
        self.calls.append(('command', target))
        if self.infrastructure:
            return {'passed': False, 'exit_code': 127, 'output': 'node: command not found'}
        fixed = 'true' in self.workspace.path('src/value.ts').read_text()
        attempts = sum(call == ('command', target) for call in self.calls)
        failed = self.force_fail == target or target == self.target and not fixed
        if self.flaky and attempts % 2 == 0:
            failed = False
        output = 'TAP version 13\n' + ('not ok 1 - persisted value\n' if failed else 'ok 1 - persisted value\n')
        return {'passed': not failed, 'exit_code': int(failed), 'output': output}

    async def close(self):
        self._started = False


class HybridModel:
    supports_tool_executor = True

    def __init__(self, root):
        self.root = root

    async def generate(self, schema, context, **options):
        assert schema is FinishRepair
        options['on_attempt']('fake-command', {'unprojected_messages': [
            {'role': 'user', 'content': '修复冻结的构建失败'}], 'json': {'messages': []}}, 1)
        pipeline = options['tool_pipeline']
        written = await pipeline.execute('Write', {'file_path': str(self.root / 'src/value.ts'),
            'content': 'export const persisted = true;\n'}, 'write-command')
        assert not written.is_error, written.error
        finished = await pipeline.execute('finish_repair', {'summary': '修复命令目标'}, 'finish-command')
        assert not finished.is_error, finished.error
        return ModelResult(FinishRepair(summary='修复命令目标'), {}, 'fake-command', 'stop')


def configured(tmp_path, target='build', **options):
    engine, state = make_engine(tmp_path)
    spec = TestSpec(goal='修复冻结命令中的持久化缺陷', mode=target)
    state.test_spec_ref = engine.put(state, spec.model_dump())
    state.test_spec_hash = digest(spec)
    state.goal = spec.goal
    engine.runner = Commands(engine.workspace, state.source_manifest, target, **options)
    async def forbidden(*args, **kwargs):
        raise AssertionError('非 GUI 任务不得启动应用或浏览器')
    engine.browser.open = engine.browser.action = forbidden
    engine.runner.start = engine.runner.health = engine.runner.version = engine.runner.rebuild = forbidden
    engine.store.save(state)
    return engine, state


async def verified(tmp_path, target='build'):
    engine, state = configured(tmp_path, target)
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.outcome == Outcome.FIX_VERIFIED, state.model_dump()
    assert state.phase == Phase.REVIEW
    assert engine.verification_passed(state)
    return engine, state


def test_old_gui_and_modify_contracts_keep_exact_serialized_hash(tmp_path):
    engine, state = make_engine(tmp_path)
    raw = engine.get(state, state.test_spec_ref)
    assert 'mode' not in raw
    assert TestSpec.model_validate(raw).model_dump() == raw
    assert digest(TestSpec.model_validate(raw)) == digest(raw)
    old = {'path': 'src/value.ts', 'before_hash': digest(b'old'), 'content': 'new'}
    assert FileEdit(**old).model_dump() == old
    assert FileEdit(**old).model_dump_json() == json.dumps(old, separators=(',', ':'))
    with pytest.raises(ValidationError):
        FileEdit(**old, operation='create')
    assert FileEdit(path='src/new.ts', before_hash=digest(b''), content='new', operation='create').operation == 'create'


@pytest.mark.parametrize('target', ['unit', 'static', 'build'])
def test_non_gui_contract_and_service_round_trip(target):
    spec = TestSpec(goal='冻结命令失败目标', mode=target)
    assert TestSpec.model_validate_json(spec.model_dump_json()).mode == target
    request = ExecutionRequest(spec=spec.model_dump())
    assert request.spec.mode == target
    with pytest.raises(ValidationError):
        ExecutionRequest(spec={**spec.model_dump(), 'regression_plan': [{'kind': 'navigate', 'value': 'http://app:3000'}]})
    with pytest.raises(ValidationError):
        TestSpec(goal='GUI 仍须断言')


@pytest.mark.parametrize('target', ['unit', 'static', 'build'])
async def test_real_failure_baseline_and_independent_verify_without_browser(tmp_path, target):
    engine, state = await verified(tmp_path, target)
    assert state.trial == 3 and state.reproduced
    assert len(state.baseline_validation_refs) == 4
    plan = frozen_plan(engine, state)
    assert plan['commands'][target] == engine.profile.commands[target]
    assert baseline_reproduced(state, plan,
        lambda ref: engine.artifacts.exists(state.scope_id, state.run_id, ref), lambda ref: engine.get(state, ref))
    assert sum(call == ('command', target) for call in engine.runner.calls) >= 6
    assert {engine.get(state, ref)['kind'] for ref in state.validation_refs} == {
        'static', 'unit', 'build', 'original', 'regression'}
    assert not engine.browser.calls
    assert state.budget.browser_actions == 0


async def test_hybrid_command_repair_uses_same_independent_gate(tmp_path):
    engine, state = configured(tmp_path)
    state.repair_mode = 'hybrid'
    engine.model = HybridModel(engine.workspace.root)
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.outcome == Outcome.FIX_VERIFIED, state.model_dump()
    assert state.repair_segment == 1 and engine.verification_passed(state)


async def test_unstable_failure_cannot_enter_diagnose(tmp_path):
    engine, state = configured(tmp_path, flaky=True)
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.outcome == Outcome.INCONCLUSIVE
    assert not state.reproduced and not state.patch_hash


async def test_missing_executable_is_infrastructure_not_bug(tmp_path):
    engine, state = configured(tmp_path, infrastructure=True)
    state.execution_mode = 'batch'
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.outcome == Outcome.INFRA_FAILURE
    assert not state.reproduced and not state.patch_hash


async def test_passing_frozen_target_completes_without_reproduction_or_patch(tmp_path):
    engine, state = configured(tmp_path)
    engine.runner.target = 'none'
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.outcome == Outcome.NO_BUG_FOUND
    assert not state.reproduced and not state.patch_hash and state.trial == 0
    assert [call for call in engine.runner.calls if call[0] == 'command'] == [('command', 'build')]


async def test_timeout_pauses_without_establishing_defect_baseline(tmp_path):
    engine, state = configured(tmp_path)
    async def timeout(target):
        raise TimeoutError('命令已超时')
    engine.runner.command = timeout
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.run_status == RunStatus.PAUSED
    assert not state.reproduced and not state.patch_hash and not state.baseline_validation_refs


async def test_user_cancellation_stops_before_running_frozen_commands(tmp_path):
    engine, state = configured(tmp_path)
    engine.control = 'cancel'
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.run_status == RunStatus.CANCELLED and state.outcome == Outcome.INCONCLUSIVE
    assert not state.reproduced and not engine.runner.calls


async def test_failed_regression_returns_to_diagnose_with_bound_feedback(tmp_path):
    engine, state = configured(tmp_path)
    engine.runner.force_fail = 'unit'
    diagnose = engine.diagnose
    async def stop_after_return(state, route):
        if state.patch_hash:
            return engine.output(state, 'end')
        return await diagnose(state, route)
    engine.diagnose = stop_after_return
    await engine.run(state)
    state = engine.store.load(state.run_id, state.scope_id)
    assert state.phase == Phase.DIAGNOSE and state.outcome is None
    assert engine.current_diagnosis_feedback(state)
    validations = [engine.get(state, ref) for ref in state.validation_refs]
    assert any(value['kind'] == 'regression' and not value['passed'] for value in validations)
    assert not engine.verification_passed(state)


def test_new_file_fingerprint_matches_proposal_and_unmaterialized_overlay(tmp_path):
    engine, state = configured(tmp_path)
    path, content = 'src/new.ts', 'export const added = true;\n'
    patch = PatchProposal(summary='新增合法实现文件', evidence_refs=[state.test_spec_ref], edits=[FileEdit(
        path=path, before_hash=digest(b''), content=content, operation='create')])
    expected = engine.candidate_fingerprint(patch)
    empty_patch = patch.model_copy(update={'edits': [FileEdit(
        path=path, before_hash=digest(b''), content='', operation='create')]})
    assert engine.candidate_fingerprint(empty_patch) != digest({})
    engine.workspace.stage(path, content)
    assert not engine.workspace.path(path).exists()
    assert engine.candidate_fingerprint() == expected
    assert expected != digest({})


@pytest.mark.parametrize('corruption', ['argv', 'exit', 'baseline', 'regression', 'binding', 'target'])
async def test_command_gate_rejects_forged_or_stale_evidence(tmp_path, corruption):
    engine, state = await verified(tmp_path)
    if corruption == 'target':
        engine.profile.commands['build'] = ['node', 'different-approved-command']
        assert not await engine.runtime_verification_passed(state)
        return
    if corruption == 'baseline':
        state.baseline_validation_refs = state.baseline_validation_refs[:1]
    else:
        kind = 'regression' if corruption == 'regression' else 'original'
        index = next(index for index, ref in enumerate(state.validation_refs) if engine.get(state, ref)['kind'] == kind)
        validation = Validation(**engine.get(state, state.validation_refs[index]))
        result = copy.deepcopy(engine.get(state, validation.artifact_ref))
        if corruption == 'argv':
            result['executed_argv'] = ['node', 'pretend-passed']
        elif corruption == 'exit':
            result['exit_code'] = 1
        elif corruption == 'regression':
            result['command_results'][0]['passed'] = False
            result['command_results'][0]['exit_code'] = 1
        else:
            result['plan_hash'] = 'wrong-plan'
        validation.artifact_ref = engine.put(state, result)
        state.validation_refs[index] = engine.put(state, validation.model_dump())
    assert not engine.verification_passed(state)


async def test_command_only_runner_never_requires_browser_image_or_start(tmp_path):
    engine, state = make_engine(tmp_path)
    runner = DockerRunner(engine.profile, engine.workspace, state.run_id)
    runner.command_only = True
    image = 'sha256:' + '1' * 64
    calls = []
    async def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == 'exec':
            assert kwargs['output_limit'] is None
            assert args == ('exec', runner.name, *engine.profile.commands['unit'])
        if args[:2] == ('image', 'inspect'):
            assert args == ('image', 'inspect', engine.profile.image)
            return {'passed': True, 'output': json.dumps([{'Id': image}])}
        if args[:2] == ('container', 'inspect'):
            assert args == ('container', 'inspect', runner.name)
            record = {'Id': 'container-id', 'Image': image, 'Name': '/' + runner.name,
                'Config': {'Labels': {'tracefix.run': state.run_id}}, 'State': {'Running': True},
                'HostConfig': {'NetworkMode': runner.network},
                'NetworkSettings': {'Networks': {runner.network: {}}}}
            return {'passed': True, 'output': json.dumps([record])}
        return {'passed': True, 'output': '', 'exit_code': 0}
    runner.docker = docker
    expected = await runner.inspect_images()
    assert await runner.start_commands(state.source_manifest) == {'passed': True}
    assert await runner.inspect_images(runtime=True) == expected
    app = next(args for args in calls if args[:2] == ('run', '-d'))
    assert '--entrypoint' in app and 'setInterval(()=>{}, 1000)' in app
    assert not any(args[0] == 'exec' for args in calls)
    assert engine.profile.browser_image not in repr(calls)
    assert (await runner.command('unit'))['exit_code'] == 0


async def test_gui_environment_digest_keeps_legacy_shape(tmp_path):
    engine, state = make_engine(tmp_path)
    runner = DockerRunner(engine.profile, engine.workspace, state.run_id)
    ids = ['sha256:' + '1' * 64, 'sha256:' + '2' * 64]
    assert runner._environment_digest(ids) == digest({'profile': engine.profile.model_dump(), 'image_ids': ids})


def test_docker_resources_use_one_trusted_group_prefix(monkeypatch):
    monkeypatch.delenv('TRACEFIX_DOCKER_RESOURCE_PREFIX', raising=False)
    assert DockerRunner(object(), object(), 'example').name == 'tf-example'
    monkeypatch.setenv('TRACEFIX_DOCKER_RESOURCE_PREFIX', 'tf-hybrid_seed-2_nonce')
    runner = DockerRunner(object(), object(), 'same-run-id')
    assert runner.name == 'tf-hybrid_seed-2_nonce'
    assert runner.network == runner.name + '-net'
    assert runner.dependency_volume == runner.name + '-node-modules'
    for prefix in ['', 'tf-xy', 'other-group', 'tf-' + 'x' * 101, 'tf-valid;docker', 'tf-a/b']:
        monkeypatch.setenv('TRACEFIX_DOCKER_RESOURCE_PREFIX', prefix)
        with pytest.raises(ValueError, match='Docker'):
            DockerRunner(object(), object(), 'same-run-id')
