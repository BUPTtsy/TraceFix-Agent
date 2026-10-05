"""Aggregate evaluator-only task records with explicit denominators and coverage."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _usage_value(row, names):
    usage = row.get('usage')
    sources = [usage, row] if isinstance(usage, dict) else [row]
    for source in sources:
        for name in names:
            value = source
            for part in name.split('.'):
                value = value.get(part) if isinstance(value, dict) else None
            if _number(value):
                return value
    return None


def _coverage(rows, names):
    values = [_usage_value(row, names) for row in rows]
    known = [value for value in values if value is not None]
    complete = bool(rows) and len(known) == len(rows)
    return {'known_sum': sum(known) if known else None, 'known_runs': len(known),
            'unknown_runs': len(rows) - len(known), 'coverage': ratio(len(known), len(rows)),
            'complete': complete, 'total': sum(known) if complete else None}


def _scored(row):
    if type(row.get('oracle_passed')) is not bool:
        return False
    if row.get('oracle_status') in {'infra', 'missing', 'invalid_binding'}:
        return False
    patch_hash = row.get('patch_hash')
    scored_hash = row.get('oracle_patch_hash')
    return not scored_hash or bool(patch_hash and scored_hash == patch_hash)


def _claim(row):
    return row.get('internal_success') is True or row.get('outcome') == 'FIX_VERIFIED'


def _boolean_metric(rows, field):
    known = [row[field] for row in rows if type(row.get(field)) is bool]
    passed = sum(value is True for value in known)
    return {'passed': passed, 'scorable': len(known), 'attempted': len(rows),
            'rate': ratio(passed, len(rows)), 'scorable_rate': ratio(passed, len(known)),
            'coverage': ratio(len(known), len(rows))}


def _latency(rows):
    values = sorted(row['duration_seconds'] for row in rows
                    if _number(row.get('duration_seconds')))

    def percentile(fraction):
        if not values:
            return None
        offset = (len(values) - 1) * fraction
        lower = math.floor(offset)
        upper = math.ceil(offset)
        return values[lower] + (values[upper] - values[lower]) * (offset - lower)

    return {'known_runs': len(values), 'coverage': ratio(len(values), len(rows)),
            'mean_seconds': sum(values) / len(values) if values else None,
            'p50_seconds': percentile(.5), 'p95_seconds': percentile(.95)}


def aggregate(rows):
    rows = list(rows)
    attempted = [row for row in rows if row.get('attempted', True) is True]
    scored = [row for row in attempted if _scored(row)]
    valid = [row for row in scored if row['oracle_passed'] is True]
    claims = [row for row in attempted if _claim(row)]
    scored_claims = [row for row in scored if _claim(row)]
    false_claims = [row for row in scored_claims if row['oracle_passed'] is False]
    known_bug = [row for row in attempted if row.get('known_bug') is True]
    reproduced = [row for row in known_bug if row.get('reproduced') is True]
    recovery = [row for row in attempted if row.get('recovery_attempted') is True]
    recovered = [row for row in recovery if _scored(row) and row['oracle_passed'] is True
                 and row.get('recovery_effective') is True]
    usage = {
        'input_tokens': _coverage(attempted, ('input_tokens', 'prompt_tokens')),
        'output_tokens': _coverage(attempted, ('output_tokens', 'completion_tokens')),
        'cache_read_tokens': _coverage(attempted, (
            'cache_read_tokens', 'cache_read_input_tokens', 'prompt_cache_hit_tokens',
            'input_tokens_details.cached_tokens', 'prompt_tokens_details.cached_tokens')),
        'cache_write_tokens': _coverage(attempted, ('cache_write_tokens', 'cache_creation_input_tokens')),
        'total_tokens': _coverage(attempted, ('total_tokens', 'tokens')),
        'cost_usd': _coverage(attempted, ('cost_usd',)),
    }
    cost = usage['cost_usd']
    kinds = sorted({row.get('evidence_kind', 'unspecified') for row in attempted})
    return {
        'runs': len(rows), 'attempted': len(attempted), 'not_attempted': len(rows) - len(attempted),
        'scorable': len(scored), 'independently_valid': len(valid),
        'independently_invalid': len(scored) - len(valid),
        'missing_scoring': len(attempted) - len(scored),
        'infra_failures': sum(row.get('infra_failure') is True
                              or row.get('outcome') == 'INFRA_FAILURE'
                              or row.get('oracle_status') == 'infra' for row in attempted),
        'invalid_scoring_binding': sum(type(row.get('oracle_passed')) is bool
                                      and not _scored(row) for row in attempted),
        'success_rate': ratio(len(valid), len(attempted)),
        'scorable_success_rate': ratio(len(valid), len(scored)),
        'scoring_coverage': ratio(len(scored), len(attempted)),
        'internal_successes': len(claims), 'scored_internal_successes': len(scored_claims),
        'false_successes': len(false_claims),
        'false_success': ratio(len(false_claims), len(scored_claims)),
        'internal_success_scoring_coverage': ratio(len(scored_claims), len(claims)),
        'claims_with_missing_oracle': len(claims) - len(scored_claims),
        'known_bug_runs': len(known_bug), 'reproduced_runs': len(reproduced),
        'e2e_fix_all_known': ratio(sum(row in valid for row in known_bug), len(known_bug)),
        'e2e_fix_reproduced': ratio(sum(row in valid for row in reproduced), len(reproduced)),
        'usage': usage,
        'cost_per_effective_fix_usd': ratio(cost['total'], len(valid))
        if cost['total'] is not None else None,
        'latency': _latency(attempted),
        'recovery': {'attempted': len(recovery), 'effective': len(recovered),
                     'rate': ratio(len(recovered), len(recovery))},
        'business': {field: _boolean_metric(attempted, field) for field in (
            'original_passed', 'reverse_passed', 'refresh_passed', 'normal_business_passed')},
        'sibling_leaks': _coverage(attempted, ('sibling_leaks',)),
        'evidence_kinds': kinds,
        'real_effects_evidence': bool(attempted) and kinds == ['real'],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('jsonl', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.jsonl.read_text(encoding='utf-8-sig').splitlines()
            if line.strip()]
    groups = defaultdict(list)
    for row in rows:
        key = (row.get('configuration', 'unspecified'), row.get('evidence_kind', 'unspecified'))
        groups[key].append(row)
    report = {'groups': [
        {'configuration': configuration, 'evidence_kind': kind, 'metrics': aggregate(group)}
        for (configuration, kind), group in sorted(groups.items())],
        'raw_records': rows,
        'limits': 'fixture 与真实结果分组；小样本只报告观察值；评分与派生报告仅供 evaluator。'}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()
