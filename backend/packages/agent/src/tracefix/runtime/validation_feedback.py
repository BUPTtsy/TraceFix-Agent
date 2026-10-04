"""Public development feedback; acceptance remains with verify_artifacts."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from tracefix.runtime.contracts import Contract, Validation, digest
from tracefix.runtime.verification import verification_binding


class ValidationFeedback(Contract):
    type: Literal['validation_feedback'] = 'validation_feedback'
    source: Literal['public_development'] = 'public_development'
    status: Literal['failed', 'reported_pass', 'unavailable', 'stale']
    kind: str
    scenario_id: str | None = None
    validation_ref: str | None = None
    binding: dict[str, str | None]
    actual_refs: list[str] = Field(default_factory=list)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    failure_class: Literal['precondition', 'counterevidence', 'editing', 'recovery'] | None = None
    error_code: str | None = None
    next_phase: Literal['reproduce', 'diagnose', 'edit', 'recover'] | None = None
    instruction: str = ''
    failed_candidate: bool = False
    failure_signature: str | None = None
    failed_assertions: list[dict] = Field(default_factory=list)
    binding_mismatches: list[str] = Field(default_factory=list)
    missing_refs: list[str] = Field(default_factory=list)


_EDIT_CODES = {'ANCHOR_NOT_FOUND', 'ANCHOR_AMBIGUOUS', 'STALE_BASE', 'OVERLAP',
               'NO_CHANGE', 'NO_EFFECTIVE_CHANGE', 'SYNTAX_ERROR', 'TYPE_ERROR',
               'NEW_SYNTAX_ERROR', 'NEW_TYPE_ERROR', 'STALE_CANDIDATE'}
_PRECONDITION_CODES = {'PRECONDITION_FAILED', 'PRECONDITION_MISMATCH', 'WRONG_SEED',
                       'WRONG_ACCOUNT', 'REPRODUCTION_PRECONDITION_FAILED'}
_RECOVERY_CODES = {'WAIT_TIMEOUT', 'WAITING', 'STALE', 'STALE_LOCATOR', 'STALE_PAGE',
                   'CONTEXT_OVERFLOW', 'CONTEXT_PRESSURE', 'UNKNOWN', 'APPLY_UNKNOWN',
                   'ENVIRONMENT_DRIFT', 'SOURCE_DRIFT'}
_ROUTES = {
    'precondition': ('reproduce', '核对冻结场景的账号、数据和前置条件，再重新复现。'),
    'counterevidence': ('diagnose', '保留当前失败候选和反证，重新核对源码假设与业务不变量。'),
    'editing': ('edit', '重读相关片段、版本或诊断，修正局部候选并重新验证实际差异。'),
    'recovery': ('recover', '核对当前观察、环境和已发操作；等待或重观察后交统一恢复控制。'),
}


def _public(record):
    if type(record) is not dict:
        return True
    return (record.get('learnable') is not False and record.get('final_scoring_only') is not True
            and all(record.get(key) not in {'final_scoring_only', 'held_out', 'held-out', 'oracle'}
                    for key in ('source', 'provenance', 'evidence_source')))


def _read(ref, exists, reader):
    if (type(ref) is not str or not ref or ref in {'.', '..'}
            or any(character in ref for character in '/\\:')
            or not callable(exists) or exists(ref) is not True or not callable(reader)):
        raise ValueError('当前 Run 的公开证据引用不可用')
    return reader(ref)


def _binding(state, replay_plan_hash, candidate_hash=None):
    return {**verification_binding(state, replay_plan_hash),
            'candidate_ref': state.patch_ref, 'candidate_hash': candidate_hash}


def _classify(kind, details):
    error_code = details.get('error_code')
    error_code = error_code.upper() if type(error_code) is str else None
    category = details.get('failure_category')
    if error_code in _PRECONDITION_CODES or category == 'precondition':
        failure_class = 'precondition'
    elif error_code in _EDIT_CODES or category in {'editing', 'syntax', 'type'}:
        failure_class = 'editing'
    elif error_code in _RECOVERY_CODES or category in {'waiting', 'stale', 'context', 'recovery'}:
        failure_class = 'recovery'
    elif category == 'counterevidence':
        failure_class = 'counterevidence'
    else:
        failure_class = 'editing' if kind in {'static', 'build', 'edit'} else (
            'recovery' if kind == 'health' else 'counterevidence')
    return failure_class, error_code


def _failed_checks(records):
    checks = []
    for record in records:
        assertions = record.get('assertions', [])
        if type(assertions) is not list:
            continue
        for check in assertions:
            if type(check) is dict and check.get('passed') is False:
                checks.append({key: check[key] for key in ('assertion', 'matches') if key in check})
    unique = {digest(check): check for check in checks}
    return [unique[key] for key in sorted(unique)]


def _failure(feedback, details, records):
    failure_class, error_code = _classify(feedback.kind, details)
    next_phase, instruction = _ROUTES[failure_class]
    checks = _failed_checks(records)
    semantic_binding = {key: value for key, value in feedback.binding.items()
                        if key in {'scope_id', 'source_manifest', 'patch_hash',
                                   'environment_digest', 'test_spec_hash', 'plan_hash'}}
    signals = {key: details[key] if type(details[key]) in {bool, int, float} else str(details[key])[:1000]
               for key in ('exit_code', 'status_code', 'error', 'reason', 'diagnostics', 'output')
               if details.get(key) not in (None, '')}
    signature = digest({'binding': semantic_binding, 'kind': feedback.kind,
        'scenario_id': feedback.scenario_id, 'failure_class': failure_class,
        'error_code': error_code, 'failed_assertions': checks, 'failure_signals': signals})
    return feedback.model_copy(update={'status': 'failed', 'failure_class': failure_class,
        'error_code': error_code, 'next_phase': next_phase, 'instruction': instruction,
        'failed_candidate': bool(feedback.binding.get('candidate_ref')),
        'failed_assertions': checks, 'failure_signature': signature})


def _invalid(feedback, *, mismatches=(), missing=()):
    return feedback.model_copy(update={'status': 'stale' if mismatches else 'unavailable',
        'failure_class': 'recovery', 'next_phase': 'recover',
        'instruction': '公开验证证据缺失或与当前候选绑定不一致；核对原件并重新验证，不能复用 pass。',
        'binding_mismatches': list(mismatches), 'missing_refs': list(missing)})


def build_validation_feedback(state, validation: Validation, *, artifact_exists, artifact_read,
                              replay_plan_hash: str, validation_ref: str | None = None,
                              artifact_read_bytes=None) -> ValidationFeedback:
    feedback = ValidationFeedback(status='unavailable', kind=validation.kind,
        scenario_id=validation.scenario_id, validation_ref=validation_ref,
        binding=_binding(state, replay_plan_hash))
    expected = verification_binding(state, replay_plan_hash)
    mismatches = [key for key, value in expected.items()
                  if not value or getattr(validation, 'replay_plan_hash' if key == 'plan_hash' else key) != value]
    if mismatches:
        return _invalid(feedback, mismatches=mismatches)
    refs, hashes, records = [], {}, []

    def collect(ref, reader=artifact_read):
        value = _read(ref, artifact_exists, reader)
        refs.append(ref)
        hashes[ref] = digest(value)
        return value

    try:
        candidate = collect(state.patch_ref)
        if type(candidate) is not dict:
            return _invalid(feedback, mismatches=['candidate_type'])
        candidate_drift = [key for key, value in expected.items()
                           if key in candidate and candidate.get(key) != value]
        if candidate_drift:
            return _invalid(feedback.model_copy(update={'actual_refs': refs, 'artifact_hashes': hashes}),
                            mismatches=[f'candidate:{key}' for key in candidate_drift])
        feedback.binding['candidate_hash'] = digest(candidate)
        if validation_ref is not None:
            wrapper = collect(validation_ref)
            if wrapper != validation.model_dump(mode='json'):
                return _invalid(feedback, mismatches=['validation_ref'])
        result = collect(validation.artifact_ref)
    except Exception:
        missing = state.patch_ref if not refs else (
            validation_ref if validation_ref is not None and validation_ref not in refs else validation.artifact_ref)
        return _invalid(feedback.model_copy(update={'actual_refs': refs, 'artifact_hashes': hashes}),
                        missing=[missing or 'candidate_ref'])
    feedback.actual_refs, feedback.artifact_hashes = refs, hashes
    if type(result) is not dict:
        return _invalid(feedback, missing=[validation.artifact_ref])
    if not _public(result):
        return _invalid(feedback, mismatches=['non_public_evidence'])
    mismatches = [key for key, value in expected.items() if result.get(key) != value]
    if result.get('type') != 'validation_result' or result.get('kind') != validation.kind:
        mismatches.append('result_type_or_kind')
    for key in ('candidate_ref', 'candidate_hash'):
        if key in result and result[key] != feedback.binding[key]:
            mismatches.append(key)
    if validation.kind == 'behavior' and (result.get('scenario_id') != validation.scenario_id
            or result.get('scenario_hash') != validation.scenario_hash):
        mismatches.append('scenario')
    if mismatches:
        return _invalid(feedback, mismatches=mismatches)
    if type(result.get('passed')) is not bool or result['passed'] != validation.passed:
        return _invalid(feedback, mismatches=['passed'])
    failure_class, _ = _classify(validation.kind, result)
    if validation.kind in {'original', 'regression', 'behavior'}:
        if (validation.passed or failure_class == 'counterevidence') and not result.get('observation_ref'):
            return _invalid(feedback, missing=['observation_ref'])
    if validation.kind in {'static', 'unit', 'build'} and (
            type(result.get('exit_code')) is not int or type(result.get('output')) is not str):
        return _invalid(feedback, missing=['command_evidence'])
    records.append(result)
    checkpoint_refs = result.get('checkpoint_refs', [])
    if type(checkpoint_refs) is not list:
        return _invalid(feedback, missing=['checkpoint_refs'])
    pending = list(checkpoint_refs)
    if result.get('observation_ref'):
        pending.append(result['observation_ref'])
    while pending:
        ref = pending.pop(0)
        if ref in refs:
            continue
        try:
            record = collect(ref)
        except Exception:
            return _invalid(feedback, missing=[str(ref)])
        if type(record) is not dict:
            return _invalid(feedback, missing=[str(ref)])
        if not _public(record):
            return _invalid(feedback, mismatches=['non_public_evidence'])
        drift = [key for key, value in expected.items() if record.get(key) != value]
        if drift:
            return _invalid(feedback, mismatches=[f'{ref}:{key}' for key in drift])
        records.append(record)
        if record.get('observation_ref'):
            if record.get('observation_hash') is None:
                return _invalid(feedback, missing=[f'{ref}:observation_hash'])
            pending.append(record['observation_ref'])
        if record.get('screenshot_ref'):
            screenshot_ref = record['screenshot_ref']
            try:
                screenshot = collect(screenshot_ref, artifact_read_bytes)
            except Exception:
                return _invalid(feedback, missing=[str(screenshot_ref)])
            if (type(screenshot) is not bytes or not screenshot.startswith(b'\x89PNG\r\n\x1a\n')
                    or len(screenshot) <= 8 or digest(screenshot) != record.get('screenshot_hash')):
                return _invalid(feedback, mismatches=[f'{screenshot_ref}:screenshot_hash'])
        elif record.get('type') == 'gui_observation':
            return _invalid(feedback, missing=[f'{ref}:screenshot_ref'])
    for record in records:
        if record.get('observation_ref') and (
                hashes.get(record['observation_ref']) != record.get('observation_hash')):
            return _invalid(feedback, mismatches=[f"{record['observation_ref']}:observation_hash"])
    if validation.passed:
        return feedback.model_copy(update={'status': 'reported_pass'})
    return _failure(feedback, result, records)


def build_error_feedback(state, details: dict, *, error_ref: str, artifact_exists,
                         artifact_read, replay_plan_hash: str = '', kind: str = 'edit') -> ValidationFeedback:
    feedback = ValidationFeedback(status='unavailable', kind=kind,
                                  binding=_binding(state, replay_plan_hash))
    if not _public(details):
        return _invalid(feedback, mismatches=['non_public_evidence'])
    try:
        actual = _read(error_ref, artifact_exists, artifact_read)
        if actual != details:
            return _invalid(feedback, mismatches=['error_ref'])
        feedback.actual_refs = [error_ref]
        feedback.artifact_hashes = {error_ref: digest(actual)}
        if state.patch_ref:
            candidate = _read(state.patch_ref, artifact_exists, artifact_read)
            feedback.binding['candidate_hash'] = digest(candidate)
            feedback.actual_refs.append(state.patch_ref)
            feedback.artifact_hashes[state.patch_ref] = digest(candidate)
    except Exception:
        return _invalid(feedback, missing=[state.patch_ref or error_ref])
    return _failure(feedback, details, [details])


def feedback_is_current(feedback: ValidationFeedback, state, *, replay_plan_hash: str,
                        artifact_exists, artifact_read, artifact_read_bytes=None) -> bool:
    if feedback.status in {'unavailable', 'stale'}:
        return False
    expected = _binding(state, replay_plan_hash, feedback.binding.get('candidate_hash'))
    if feedback.binding != expected:
        return False
    for ref, artifact_hash in feedback.artifact_hashes.items():
        try:
            try:
                value = _read(ref, artifact_exists, artifact_read)
            except Exception:
                value = _read(ref, artifact_exists, artifact_read_bytes)
            if digest(value) != artifact_hash:
                return False
        except Exception:
            return False
    return True
