import math

import pytest

from evals.metrics import aggregate


def test_independent_success_does_not_require_internal_claim_and_infra_stays_in_denominator():
    rows = [
        {'oracle_passed': True, 'outcome': 'INCONCLUSIVE'},
        {'oracle_passed': False, 'outcome': 'FIX_VERIFIED'},
        {'outcome': 'FIX_VERIFIED'},
        {'outcome': 'INFRA_FAILURE'},
        {'attempted': False},
    ]
    result = aggregate(rows)
    assert result['attempted'] == 4
    assert result['scorable'] == 2
    assert result['independently_valid'] == 1
    assert result['success_rate'] == .25
    assert result['scorable_success_rate'] == .5
    assert result['scoring_coverage'] == .5
    assert result['infra_failures'] == 1
    assert result['false_success'] == 1
    assert result['internal_success_scoring_coverage'] == .5
    assert result['claims_with_missing_oracle'] == 1


def test_wrong_patch_binding_and_string_boolean_cannot_supply_a_score():
    result = aggregate([
        {'oracle_passed': True, 'patch_hash': 'current', 'oracle_patch_hash': 'other'},
        {'oracle_passed': 'true'},
        {'oracle_passed': True, 'oracle_status': 'infra'},
    ])
    assert result['independently_valid'] == 0
    assert result['scorable'] == 0
    assert result['success_rate'] == 0
    assert result['false_success'] is None


def test_partial_usage_preserves_known_values_and_reports_unknown():
    result = aggregate([
        {'oracle_passed': True, 'usage': {'prompt_tokens': 10, 'completion_tokens': 3,
                                         'prompt_tokens_details': {'cached_tokens': 4},
                                         'cost_usd': .02}},
        {'usage': {'input_tokens': 20, 'output_tokens': 0}},
        {'usage': {'input_tokens': float('nan'), 'cost_usd': float('inf')}},
    ])
    usage = result['usage']
    assert usage['input_tokens']['known_sum'] == 30
    assert usage['input_tokens']['coverage'] == pytest.approx(2 / 3)
    assert usage['input_tokens']['total'] is None
    assert usage['output_tokens']['known_sum'] == 3
    assert usage['cache_read_tokens']['known_sum'] == 4
    assert usage['cache_write_tokens']['known_sum'] is None
    assert usage['cost_usd']['known_sum'] == .02
    assert usage['cost_usd']['total'] is None
    assert result['cost_per_effective_fix_usd'] is None
    assert not math.isnan(usage['input_tokens']['known_sum'])


def test_zero_denominator_and_no_effective_fix_are_na():
    empty = aggregate([])
    assert empty['success_rate'] is None
    assert empty['scoring_coverage'] is None
    assert empty['false_success'] is None
    assert empty['usage']['input_tokens']['known_sum'] is None
    assert empty['cost_per_effective_fix_usd'] is None
    failed = aggregate([{'oracle_passed': False, 'cost_usd': .2}])
    assert failed['usage']['cost_usd']['total'] == .2
    assert failed['cost_per_effective_fix_usd'] is None


def test_fixture_is_never_labelled_real_and_recovery_requires_independent_validity():
    result = aggregate([
        {'evidence_kind': 'fixture', 'oracle_passed': False,
         'recovery_attempted': True, 'recovery_effective': True},
        {'evidence_kind': 'fixture', 'oracle_passed': True,
         'recovery_attempted': True, 'recovery_effective': True},
    ])
    assert result['real_effects_evidence'] is False
    assert result['recovery'] == {'attempted': 2, 'effective': 1, 'rate': .5}
