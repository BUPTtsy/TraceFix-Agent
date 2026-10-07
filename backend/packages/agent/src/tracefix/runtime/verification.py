"""R-11 producer protocol: all result/observation/checkpoint bodies carry the
flat verification_binding fields. Frozen TestSpec and replay-plan JSON stay raw.
Validation wrappers require scope_id/run_id and replay_plan_hash=plan_hash.
Result bodies use type=validation_result, kind and a strict bool passed; commands
require a real integer exit_code and string output, HTTP health uses status_code
and url. Optional build.health must also be a successful command/HTTP result.
GUI results add observation_ref/hash, assertions from browser.assertions and an
execution_plan_hash: original=frozen plan, regression=spec.regression_plan,
behavior=the ordered scenario actions (model_dump(mode='json') for each action).
GUI observations use type=gui_observation, snapshot, screenshot_ref/hash, where
screenshot_hash=digest(actual PNG bytes) and observation_hash=digest(observation).
Behavior results carry scenario_id/hash and ordered checkpoint_refs; checkpoints
use type=assertion_checkpoint, kind=behavior, scenario_id/hash, scenario_step,
action_hash and the same GUI fields/execution_plan_hash. The result references
the last checkpoint's observation. Readers must enforce scope/run and storage
integrity; JSON and screenshot-byte readers are separate and both mandatory.
"""
from math import isfinite
from urllib.parse import urlsplit

from tracefix.runtime.contracts import (BrowserAction, CheckPlan, CheckResult, SCHEMA_VERSION,
                                       TestSpec, Validation, digest)

REQUIRED = {'static', 'unit', 'build', 'health', 'original', 'regression'}


def verification_binding(state, replay_plan_hash):
    return {'scope_id': state.scope_id, 'run_id': state.run_id,
            'verifier_version': SCHEMA_VERSION, 'source_manifest': state.source_manifest,
            'patch_hash': state.patch_hash, 'environment_digest': state.environment_digest,
            'test_spec_hash': state.test_spec_hash, 'plan_hash': replay_plan_hash}


def _read(ref, exists, reader):
    if (type(ref) is not str or not ref or ref in {'.', '..'}
            or any(char in ref for char in '/\\:') or exists(ref) is not True):
        raise ValueError('artifact reference is invalid for this run')
    return reader(ref)


def _bound(record, binding, record_type, kind=None):
    return (type(record) is dict and record.get('type') == record_type
            and (kind is None or record.get('kind') == kind)
            and all(record.get(key) == value for key, value in binding.items()))


def _passed(result):
    if type(result) is not dict or type(result.get('passed')) is not bool or result.get('passed') is not True:
        return False
    if any(key in result and result[key] is not True for key in ('success', 'ok', 'healthy')):
        return False
    if 'failed' in result and result['failed'] is not False:
        return False
    if result.get('error') not in (None, ''):
        return False
    if 'status' in result and result['status'] not in {'passed', 'success', 'succeeded', 'healthy'}:
        return False
    if 'exit_code' in result and (type(result['exit_code']) is not int or result['exit_code'] != 0):
        return False
    if 'assertions' in result:
        checks = result['assertions']
        if (type(checks) is not list or not checks
                or any(type(check) is not dict or check.get('passed') is not True for check in checks)):
            return False
    return True


def _command(result):
    return (_passed(result) and type(result.get('exit_code')) is int
            and result['exit_code'] == 0 and type(result.get('output')) is str)


def _health(result):
    if not _passed(result):
        return False
    command = 'exit_code' in result
    response = 'status_code' in result
    if command and not _command(result):
        return False
    if response:
        status = result['status_code']
        url = urlsplit(result.get('url', ''))
        if (type(status) is not int or not 200 <= status < 300
                or url.scheme not in {'http', 'https'} or not url.netloc):
            return False
    return command or response


def _gui(record, checks, binding, exists, read, read_bytes):
    from tracefix.execution.browser import assertions

    if not _passed(record) or not callable(read_bytes):
        return False
    observation = _read(record.get('observation_ref'), exists, read)
    if (not _bound(observation, binding, 'gui_observation')
            or type(observation.get('snapshot')) is not str
            or type(observation.get('screenshot_hash')) is not str):
        return False
    if record.get('observation_hash') != digest(observation):
        return False
    png = _read(observation.get('screenshot_ref'), exists, read_bytes)
    if (type(png) is not bytes or not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) <= 8
            or observation.get('screenshot_hash') != digest(png)):
        return False
    evaluated = assertions(observation['snapshot'], checks)
    actual = record.get('assertions')
    if (type(actual) is not list or any(type(check.get('matches')) is not int for check in actual)
            or actual != evaluated['assertions']):
        return False
    return evaluated['passed'] is True


def _behavior(result, scenario, binding, exists, read, read_bytes):
    scenario_binding = {**binding, 'scenario_id': scenario.id, 'scenario_hash': digest(scenario)}
    execution_hash = digest([step.action.model_dump(mode='json') for step in scenario.steps])
    expected = [(index, step) for index, step in enumerate(scenario.steps, 1) if step.assertions]
    refs = result.get('checkpoint_refs')
    if (not _bound(result, scenario_binding, 'validation_result', 'behavior')
            or result.get('execution_plan_hash') != execution_hash
            or type(refs) is not list or len(refs) != len(expected)
            or any(type(ref) is not str for ref in refs) or len(set(refs)) != len(refs)):
        return False
    last_checkpoint = None
    for ref, (index, step) in zip(refs, expected):
        checkpoint = _read(ref, exists, read)
        if (not _bound(checkpoint, scenario_binding, 'assertion_checkpoint', 'behavior')
                or type(checkpoint.get('scenario_step')) is not int
                or checkpoint['scenario_step'] != index
                or checkpoint.get('action_hash') != digest(step.action)
                or checkpoint.get('execution_plan_hash') != execution_hash
                or not _gui(checkpoint, step.assertions, binding, exists, read, read_bytes)):
            return False
        last_checkpoint = checkpoint
    return (last_checkpoint is not None
            and result.get('observation_ref') == last_checkpoint['observation_ref']
            and result.get('observation_hash') == last_checkpoint['observation_hash'])


def _check_evidence(ref, state, patch_hash, exists, read, read_bytes, seen, depth=0):
    if depth > 8:
        return False
    if ref in seen:
        return True
    value = _read(ref, exists, read if ref.endswith('.json') else read_bytes)
    seen.add(ref)
    if not ref.endswith('.json'):
        return type(value) is bytes and bool(value)
    if type(value) is not dict:
        return False
    binding = {'scope_id': state.scope_id, 'run_id': state.run_id,
               'source_manifest': state.source_manifest, 'test_spec_hash': state.test_spec_hash,
               'patch_hash': patch_hash}
    if any(key in value and value[key] != expected for key, expected in binding.items()):
        return False
    for key in ('artifact_ref', 'observation_ref', 'screenshot_ref'):
        if value.get(key) and not _check_evidence(
                value[key], state, patch_hash, exists, read, read_bytes, seen, depth + 1):
            return False
    for key in ('evidence_refs', 'checkpoint_refs'):
        refs = value.get(key, [])
        if type(refs) is not list or any(not _check_evidence(
                nested, state, patch_hash, exists, read, read_bytes, seen, depth + 1) for nested in refs):
            return False
    if value.get('screenshot_ref'):
        png = _read(value['screenshot_ref'], exists, read_bytes)
        if (type(png) is not bytes or not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) <= 8
                or value.get('screenshot_hash') != digest(png)):
            return False
    return True


def _check_results(state, plan, refs, stage, patch_hash, exists, read, read_bytes):
    if (type(refs) is not list or len(refs) != len(plan.items)
            or any(type(ref) is not str for ref in refs) or len(set(refs)) != len(refs)):
        return None
    items = {item.id: item for item in plan.items}
    results = {}
    seen = set()
    for ref in refs:
        result = CheckResult.model_validate(_read(ref, exists, read))
        item = items.get(result.id)
        if (item is None or result.id in results or result.stage != stage
                or result.patch_hash != patch_hash or result.source_manifest != state.source_manifest
                or result.check_plan_hash != state.check_plan_hash
                or any(getattr(result, field) != getattr(item, field)
                       for field in ('name', 'criteria', 'severity', 'source', 'detector'))
                or not result.actual.strip() or not isfinite(result.started_at)
                or not isfinite(result.finished_at) or result.finished_at < result.started_at
                or result.status in {'pass', 'fail'} and (result.error or not result.evidence_refs)):
            return None
        if any(not _check_evidence(evidence, state, patch_hash, exists, read, read_bytes, seen)
               for evidence in result.evidence_refs):
            return None
        results[result.id] = result
    return results


def _frozen_check_plan(state, exists, read):
    from tracefix.rules.models import RuleSnapshot

    raw = _read(state.check_plan_ref, exists, read)
    if type(raw) is not dict or digest(raw) != state.check_plan_hash:
        return None
    plan = CheckPlan.model_validate(raw)
    if (plan.run_id != state.run_id or plan.source_manifest != state.source_manifest
            or plan.test_spec_hash != state.test_spec_hash
            or plan.rule_snapshot_hash != state.rule_snapshot_hash):
        return None
    rule_items = {item.id: item.rule_version for item in plan.items if item.source == 'rule'}
    if not any(item.source == 'user_goal' for item in plan.items):
        return None
    if state.rule_snapshot_ref:
        snapshot = RuleSnapshot.model_validate(_read(state.rule_snapshot_ref, exists, read))
        refs = [ref.model_dump(mode='json') for ref in snapshot.refs]
        if (snapshot.run_id != state.run_id or snapshot.hash != state.rule_snapshot_hash
                or len({ref.id for ref in snapshot.refs}) != len(snapshot.refs)
                or rule_items != {ref.id: ref.version for ref in snapshot.refs}
                or refs != state.rule_refs):
            return None
    elif rule_items or state.rule_snapshot_hash or state.rule_refs:
        return None
    return plan


def initial_check_failure(state, exists, read, read_bytes):
    try:
        if (not state.check_plan_ref or not callable(exists) or not callable(read)
                or not callable(read_bytes)):
            return False
        plan = _frozen_check_plan(state, exists, read)
        if plan is None:
            return False
        results = _check_results(state, plan, state.initial_check_result_refs, 'explore', None,
                                 exists, read, read_bytes)
        return results is not None and any(result.status == 'fail' for result in results.values())
    except Exception:
        return False


def _check_suite_verified(state, exists, read, read_bytes):
    plan = _frozen_check_plan(state, exists, read)
    if plan is None:
        return False
    if (state.check_suite_completed is not True or state.check_suite_stage != 'verify'
            or state.check_suite_patch_hash != state.patch_hash):
        return False
    final = _check_results(state, plan, state.check_result_refs, 'verify', state.patch_hash,
                           exists, read, read_bytes)
    return (initial_check_failure(state, exists, read, read_bytes)
            and final is not None and all(result.status in {'pass', 'fail'}
                and (result.severity != 'blocker' or result.status == 'pass') for result in final.values()))


def verify_artifacts(state, validations, exists, read, read_bytes):
    try:
        if (not callable(exists) or not callable(read) or not callable(read_bytes)
                or not state.source_aligned or not state.patch_hash
                or not state.reproduction_plan_frozen or not state.test_spec_ref or not state.replay_plan_ref):
            return False
        if state.check_plan_ref:
            if not _check_suite_verified(state, exists, read, read_bytes):
                return False
        elif not state.reproduced:
            return False
        raw_spec = _read(state.test_spec_ref, exists, read)
        if type(raw_spec) is not dict or digest(raw_spec) != state.test_spec_hash:
            return False
        spec = TestSpec.model_validate(raw_spec)
        plan = _read(state.replay_plan_ref, exists, read)
        if type(plan) is not list or not plan:
            return False
        for raw_action in plan:
            if type(raw_action) is not dict:
                return False
            action = BrowserAction.model_validate(raw_action)
            if (action.kind not in spec.authorized_actions or action.kind == 'finish'
                    or action.observation_id is not None or action.element_ref is not None):
                return False
        plan_hash = digest(plan)
        binding = verification_binding(state, plan_hash)
        if any(type(value) is not str or not value for value in binding.values()):
            return False
        latest = {}
        for raw_validation in validations:
            validation = Validation.model_validate(raw_validation.model_dump())
            key = (validation.kind, validation.scenario_id)
            if key not in ({(kind, None) for kind in REQUIRED}
                           | {('behavior', scenario.id) for scenario in spec.behavior_scenarios}):
                return False
            latest[key] = validation
        scenarios = {scenario.id: scenario for scenario in spec.behavior_scenarios}
        expected_keys = {(kind, None) for kind in REQUIRED} | {('behavior', scenario_id) for scenario_id in scenarios}
        if set(latest) != expected_keys:
            return False
        for key in expected_keys:
            validation = latest[key]
            if (validation.type != 'validation' or validation.passed is not True
                    or validation.scope_id != state.scope_id or validation.run_id != state.run_id
                    or validation.replay_plan_hash != plan_hash
                    or any(getattr(validation, field) != value for field, value in binding.items()
                           if field not in {'scope_id', 'run_id', 'plan_hash'})):
                return False
            result = _read(validation.artifact_ref, exists, read)
            if not _bound(result, binding, 'validation_result', validation.kind) or not _passed(result):
                return False
            kind = validation.kind
            if kind == 'behavior':
                scenario = scenarios[validation.scenario_id]
                if validation.scenario_hash != digest(scenario) or not _behavior(
                        result, scenario, binding, exists, read, read_bytes):
                    return False
            elif validation.scenario_id is not None or validation.scenario_hash is not None:
                return False
            elif kind in {'original', 'regression'}:
                checks = spec.assertions if kind == 'original' else spec.regression_assertions
                execution_hash = plan_hash if kind == 'original' else digest(
                    [action.model_dump(mode='json') for action in spec.regression_plan])
                if result.get('execution_plan_hash') != execution_hash or not _gui(
                        result, checks, binding, exists, read, read_bytes):
                    return False
            elif kind == 'health':
                if not _health(result):
                    return False
            elif not _command(result) or ('health' in result and not _health(result['health'])):
                return False
        for ref in state.evidence_refs:
            _read(ref, exists, read)
        return True
    except Exception:
        return False
