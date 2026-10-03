import copy

import pytest
from pydantic import ValidationError

from tracefix.execution.browser import assertions
from tracefix.runtime.contracts import (Assertion, BehaviorScenario, BehaviorStep, BrowserAction,
    Locator, RunState, TestSpec as FrozenSpec, Validation, digest, reduce_state, verification_gate)
from tracefix.runtime.verification import verification_binding
from tracefix.storage.artifacts import Artifacts

KINDS = ['static', 'unit', 'build', 'health', 'original', 'regression', 'behavior']
BINDING = ['scope_id', 'run_id', 'verifier_version', 'source_manifest', 'patch_hash',
           'environment_digest', 'test_spec_hash', 'plan_hash']
PNG = b'\x89PNG\r\n\x1a\nverified-image-bytes'


@pytest.fixture
def bundle():
    check = Assertion(locator=Locator(role='checkbox', name='Task'), condition='checked')
    action = BrowserAction(kind='navigate', value='http://app:3000')
    scenario = BehaviorScenario(id='round-trip', steps=[
        BehaviorStep(action=action, assertions=[check]),
        BehaviorStep(action=BrowserAction(kind='observe'), assertions=[check])])
    spec = FrozenSpec(goal='verify persisted task', assertions=[check],
        regression_plan=[BrowserAction(kind='observe')], regression_assertions=[check],
        behavior_scenarios=[scenario])
    plan = [action.model_dump(mode='json')]
    state = RunState(scope_id='scope', run_id='run', goal=spec.goal, url=action.value,
        source_manifest='source', environment_digest='environment', patch_hash='patch',
        test_spec_hash=digest(spec), test_spec_ref='spec.json', replay_plan_ref='plan.json',
        reproduced=True, source_aligned=True, reproduction_plan_frozen=True,
        evidence_refs=['failure.json'], patch_base_commit='a' * 40)
    artifacts = {'spec.json': spec.model_dump(mode='json'), 'plan.json': plan,
                 'screenshot.png': PNG, 'failure.json': {'passed': False}}
    binding = verification_binding(state, digest(plan))
    observation = {**binding, 'type': 'gui_observation', 'snapshot': '- checkbox "Task" [checked] [ref=e1]',
                   'screenshot_ref': 'screenshot.png', 'screenshot_hash': digest(PNG)}
    artifacts['observation.json'] = observation
    gui = {**assertions(observation['snapshot'], [check]), 'observation_ref': 'observation.json',
           'observation_hash': digest(observation)}
    validations = []
    for kind in KINDS:
        result = {**binding, 'type': 'validation_result', 'kind': kind, 'passed': True}
        scenario_fields = {}
        if kind in {'static', 'unit', 'build', 'health'}:
            result.update(exit_code=0, output='actual command result')
        elif kind in {'original', 'regression'}:
            result.update(gui, execution_plan_hash=digest(plan) if kind == 'original'
                          else digest([item.model_dump(mode='json') for item in spec.regression_plan]))
        else:
            scenario_fields = {'scenario_id': scenario.id, 'scenario_hash': digest(scenario)}
            execution_hash = digest([step.action.model_dump(mode='json') for step in scenario.steps])
            refs = []
            for index, step in enumerate(scenario.steps, 1):
                ref = f'checkpoint-{index}.json'
                artifacts[ref] = {**binding, **gui, **scenario_fields, 'type': 'assertion_checkpoint',
                    'kind': kind, 'scenario_step': index, 'action_hash': digest(step.action),
                    'execution_plan_hash': execution_hash}
                refs.append(ref)
            result.update(scenario_fields, checkpoint_refs=refs, execution_plan_hash=execution_hash,
                          observation_ref=gui['observation_ref'], observation_hash=gui['observation_hash'])
        ref = kind + '.json'
        artifacts[ref] = result
        validations.append(Validation(kind=kind, passed=True, scope_id=state.scope_id,
            run_id=state.run_id, source_manifest=state.source_manifest, patch_hash=state.patch_hash,
            environment_digest=state.environment_digest, test_spec_hash=state.test_spec_hash,
            replay_plan_hash=digest(plan), artifact_ref=ref, **scenario_fields))
    return state, validations, artifacts


def gate(bundle, **readers):
    state, validations, artifacts = bundle
    callbacks = {'artifact_read': lambda ref: copy.deepcopy(artifacts[ref]),
                 'artifact_read_bytes': lambda ref: artifacts[ref]}
    callbacks.update(readers)
    return verification_gate(state, validations, lambda ref: ref in artifacts, **callbacks)


def test_all_generic_and_behavior_evidence_passes(bundle):
    assert gate(bundle)


def test_six_generic_gates_without_behavior_still_require_full_evidence(bundle):
    state, validations, artifacts = bundle
    artifacts['spec.json']['behavior_scenarios'] = []
    state.test_spec_hash = digest(artifacts['spec.json'])
    validations.pop()
    for index, validation in enumerate(validations):
        validations[index] = validation.model_copy(update={'test_spec_hash': state.test_spec_hash})
    for ref in ['original.json', 'regression.json', 'static.json', 'unit.json', 'build.json', 'health.json', 'observation.json']:
        artifacts[ref]['test_spec_hash'] = state.test_spec_hash
    for kind in ['original', 'regression']:
        artifacts[kind + '.json']['observation_hash'] = digest(artifacts['observation.json'])
    assert gate(bundle)
    artifacts['unit.json']['exit_code'] = 3
    assert not gate(bundle)


@pytest.mark.parametrize('kind', KINDS)
def test_latest_failed_body_overrides_successful_wrapper(bundle, kind):
    validation = next(item for item in bundle[1] if item.kind == kind)
    body = copy.deepcopy(bundle[2][validation.artifact_ref])
    body['passed'] = False
    bundle[2]['late.json'] = body
    bundle[1].append(validation.model_copy(update={'artifact_ref': 'late.json'}))
    assert not gate(bundle)
    bundle[1].append(validation)
    assert gate(bundle)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('payload', [None, {}, [], 'passed', {'passed': True}, {'passed': 'passed'}])
def test_empty_untyped_or_unbound_results_never_pass(bundle, kind, payload):
    bundle[2][kind + '.json'] = payload
    assert not gate(bundle)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('field', BINDING + ['type', 'kind', 'passed'])
@pytest.mark.parametrize('missing', [True, False])
def test_every_kind_requires_exact_body_binding(bundle, kind, field, missing):
    result = bundle[2][kind + '.json']
    if missing:
        result.pop(field)
    else:
        result[field] = False if field == 'passed' else 'wrong'
    assert not gate(bundle)


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('field', ['passed', 'scope_id', 'run_id', 'source_manifest', 'patch_hash',
    'environment_digest', 'test_spec_hash', 'verifier_version', 'replay_plan_hash', 'artifact_ref'])
def test_latest_wrapper_never_falls_back_to_old_success(bundle, kind, field):
    validations = bundle[1]
    previous = next(item for item in validations if item.kind == kind)
    validations.append(previous.model_copy(update={field: False if field == 'passed' else 'wrong'}))
    assert not gate(bundle)
    validations.append(previous)
    assert gate(bundle)


@pytest.mark.parametrize('kind', ['static', 'unit', 'build', 'health'])
@pytest.mark.parametrize('fault', ['missing_exit', 'exit_bool', 'exit_string', 'nonzero',
    'missing_output', 'bad_output', 'success_false', 'failed_true', 'assertions_empty', 'assertion_failed',
    'error', 'failed_status'])
def test_commands_require_real_success_not_labels(bundle, kind, fault):
    result = bundle[2][kind + '.json']
    if fault == 'missing_exit':
        result.pop('exit_code')
    elif fault == 'missing_output':
        result.pop('output')
    else:
        field, value = {'exit_bool': ('exit_code', False), 'exit_string': ('exit_code', '0'),
            'nonzero': ('exit_code', 2), 'bad_output': ('output', None),
            'success_false': ('success', False), 'failed_true': ('failed', True),
            'assertions_empty': ('assertions', []),
            'assertion_failed': ('assertions', [{'passed': False}]),
            'error': ('error', 'command failure'), 'failed_status': ('status', 'failed')}[fault]
        result[field] = value
    assert not gate(bundle)


@pytest.mark.parametrize('status,passed', [(200, True), (204, True), (299, True), (302, False),
    (500, False), ('200', False), (True, False), (None, False)])
def test_http_health_requires_actual_status(bundle, status, passed):
    result = bundle[2]['health.json']
    result.pop('exit_code')
    result.pop('output')
    result.update(status_code=status, url='http://app:3000/health')
    assert gate(bundle) is passed
    result['url'] = 'not-http'
    assert not gate(bundle)


@pytest.mark.parametrize('health', [None, {}, {'passed': True}, {'passed': True, 'exit_code': 1, 'output': ''},
    {'passed': True, 'status_code': 500, 'url': 'http://app:3000/health'}])
def test_build_nested_health_cannot_contradict_success(bundle, health):
    bundle[2]['build.json']['health'] = health
    assert not gate(bundle)


@pytest.mark.parametrize('kind', ['original', 'regression', 'checkpoint-1', 'checkpoint-2'])
@pytest.mark.parametrize('fault', ['empty_assertions', 'wrong_assertion', 'failed_assertion',
    'string_assertion', 'missing_observation', 'wrong_observation_hash', 'wrong_plan', 'wrong_matches'])
def test_gui_recomputes_assertions_and_hashes(bundle, kind, fault):
    result = bundle[2][kind + '.json']
    if fault == 'empty_assertions':
        result['assertions'] = []
    elif fault == 'wrong_assertion':
        result['assertions'][0]['assertion']['condition'] = 'unchecked'
    elif fault in {'failed_assertion', 'string_assertion'}:
        result['assertions'][0]['passed'] = False if fault == 'failed_assertion' else 'passed'
    elif fault == 'wrong_matches':
        result['assertions'][0]['matches'] = True
    else:
        field = {'missing_observation': 'observation_ref', 'wrong_observation_hash': 'observation_hash',
                 'wrong_plan': 'execution_plan_hash'}[fault]
        result[field] = 'invalid'
    assert not gate(bundle)


@pytest.mark.parametrize('fault', ['missing_png', 'wrong_png', 'empty_png', 'bad_hash', 'missing_hash',
    'cross_scope', 'cross_run', 'false_snapshot', 'nonstring_snapshot', 'duplicate_controls'])
def test_gui_observation_screenshot_closure(bundle, fault):
    artifacts = bundle[2]
    observation = artifacts['observation.json']
    if fault in {'missing_png', 'wrong_png', 'empty_png'}:
        if fault == 'missing_png':
            artifacts.pop('screenshot.png')
        else:
            artifacts['screenshot.png'] = b'bad-image' if fault == 'wrong_png' else b''
    elif fault == 'missing_hash':
        observation.pop('screenshot_hash')
    elif fault == 'bad_hash':
        observation['screenshot_hash'] = 'wrong'
    elif fault in {'cross_scope', 'cross_run'}:
        observation['scope_id' if fault == 'cross_scope' else 'run_id'] = 'other'
    else:
        observation['snapshot'] = {'false_snapshot': '- checkbox "Task" [ref=e1]',
            'nonstring_snapshot': None,
            'duplicate_controls': observation['snapshot'] + '\n' + observation['snapshot']}[fault]
        for ref in ['original.json', 'regression.json', 'behavior.json', 'checkpoint-1.json', 'checkpoint-2.json']:
            artifacts[ref]['observation_hash'] = digest(observation)
    assert not gate(bundle)


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'reverse', 'wrong_scenario', 'wrong_step',
    'wrong_action', 'unbound_checkpoint', 'bad_final_observation', 'late_scenario_failure'])
def test_behavior_checkpoints_remain_strict(bundle, fault):
    artifacts = bundle[2]
    result = artifacts['behavior.json']
    if fault in {'missing', 'duplicate', 'reverse'}:
        result['checkpoint_refs'] = {'missing': ['checkpoint-2.json'],
            'duplicate': ['checkpoint-1.json'] * 2,
            'reverse': ['checkpoint-2.json', 'checkpoint-1.json']}[fault]
    elif fault == 'bad_final_observation':
        result['observation_ref'] = 'missing'
    elif fault == 'late_scenario_failure':
        bundle[1].append(bundle[1][-1].model_copy(update={'scenario_hash': 'other'}))
    else:
        checkpoint = artifacts['checkpoint-1.json']
        field = {'wrong_scenario': 'scenario_hash', 'wrong_step': 'scenario_step',
                 'wrong_action': 'action_hash', 'unbound_checkpoint': 'environment_digest'}[fault]
        checkpoint[field] = 'wrong'
    assert not gate(bundle)


@pytest.mark.parametrize('exception', [OSError, PermissionError, ValueError, KeyError, TypeError, RuntimeError, AttributeError])
@pytest.mark.parametrize('target', ['spec.json', 'plan.json', 'unit.json', 'observation.json', 'failure.json', 'screenshot.png'])
def test_every_reader_failure_is_closed(bundle, exception, target):
    def read(ref):
        if ref == target:
            raise exception('unreadable')
        return copy.deepcopy(bundle[2][ref])
    assert not gate(bundle, artifact_read=read, artifact_read_bytes=read)


def test_read_apis_are_mandatory_and_existence_errors_are_closed(bundle):
    for reader in ['artifact_read', 'artifact_read_bytes']:
        assert not gate(bundle, **{reader: None})
    def exists(ref):
        raise PermissionError(ref)
    assert not verification_gate(bundle[0], bundle[1], exists,
        artifact_read=lambda ref: bundle[2][ref], artifact_read_bytes=lambda ref: bundle[2][ref])


@pytest.mark.parametrize('fault', ['spec_drift', 'plan_drift', 'empty_plan', 'unfrozen', 'unbound_action', 'unauthorized'])
def test_frozen_spec_and_replay_plan_are_validated(bundle, fault):
    if fault == 'spec_drift':
        bundle[2]['spec.json']['goal'] = 'changed goal'
    elif fault == 'plan_drift':
        bundle[2]['plan.json'][0]['value'] = 'http://other'
    elif fault == 'empty_plan':
        bundle[2]['plan.json'] = []
    elif fault == 'unfrozen':
        bundle[0].reproduction_plan_frozen = False
    else:
        bundle[2]['plan.json'][0]['observation_id' if fault == 'unbound_action' else 'kind'] = 'unknown'
    assert not gate(bundle)
    for field, value in [('test_spec_ref', 'new'), ('replay_plan_ref', 'new'), ('patch_base_commit', 'b' * 40)]:
        with pytest.raises(ValueError):
            reduce_state(bundle[0], bundle[0].revision, **{field: value})


@pytest.mark.parametrize('value', ['passed', 'true', 1, None, [], {}])
def test_validation_wrapper_passed_is_strict(bundle, value):
    with pytest.raises(ValidationError):
        Validation.model_validate({**bundle[1][0].model_dump(), 'passed': value})


@pytest.mark.parametrize('fault', ['scope', 'run', 'damage', 'path'])
def test_real_artifact_api_blocks_foreign_and_corrupt_references(bundle, tmp_path, fault):
    state, validations, values = bundle
    artifacts = Artifacts(tmp_path / 'artifacts')
    old_ref = validations[1].artifact_ref
    scope, run = ('other', state.run_id) if fault == 'scope' else (state.scope_id, 'other') if fault == 'run' else (state.scope_id, state.run_id)
    foreign = artifacts.put(scope, run, values[old_ref])
    if fault == 'damage':
        (artifacts.root / scope / run / foreign).write_bytes(b'corrupt')
    validations[1] = validations[1].model_copy(update={'artifact_ref': '../foreign.json' if fault == 'path' else foreign})
    def read(ref):
        return copy.deepcopy(values[ref]) if ref in values else artifacts.json(state.scope_id, state.run_id, ref)
    def exists(ref):
        return ref in values or artifacts.exists(state.scope_id, state.run_id, ref)
    assert not verification_gate(state, validations, exists, artifact_read=read,
                                artifact_read_bytes=lambda ref: values[ref])
