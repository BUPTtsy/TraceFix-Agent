"""修复模式成对门禁；开发观测与回退结果不得冒充正式结论。"""
from collections import defaultdict

from evals.metrics import _scored, aggregate

PAIR_FIELDS = ('case_id', 'family', 'source_revision', 'source_hash', 'environment_digest',
               'seed', 'spec_hash', 'protocol_hash', 'model', 'effort', 'tools_hash', 'skill_hash',
               'initial_experience_hash', 'recovery_rules_hash')
FORMAL_CATEGORIES = ('B', 'M', 'N', 'V', 'CLEAN', 'G')


def evaluate_mode_gate(rows, *, minimum_cases=5, minimum_repeats=3,
                       expected_categories=FORMAL_CATEGORIES):
    expected_categories = tuple(expected_categories)
    if (not expected_categories or any(not isinstance(category, str) or not category
                                       for category in expected_categories)
            or len(set(expected_categories)) != len(expected_categories)):
        raise ValueError('expected_categories 必须为非空且不重复的类别列表')
    rows = list(rows)
    categories = defaultdict(list)
    excluded = defaultdict(int)
    seen = set()
    for row in rows:
        arm = row.get('arm')
        if arm not in {'pipeline', 'hybrid'}:
            excluded['other_arm'] += 1
            continue
        if row.get('repair_mode_actual') != arm:
            excluded['mode_mismatch'] += 1
            continue
        if (row.get('held_out') is not True or row.get('evidence_kind') != 'real'
                or row.get('trace_summary', {}).get('trace_proven') is not True
                or row.get('trace_summary', {}).get('oracle_isolation_proven') is not True
                or row.get('agent_mode') != 'multi' or row.get('cross_run_memory') is not False
                or row.get('real_model') is False or row.get('source') == 'scripted'):
            excluded['unproven_evidence'] += 1
            continue
        if row.get('track') == 'capability' or str(row.get('category', '')).upper() in {'U', 'C'}:
            excluded['capability_track'] += 1
            continue
        if (not _scored(row) or row.get('infra_failure') or not row.get('patch_hash')
                or row.get('oracle_patch_hash') != row['patch_hash']):
            excluded['unscored'] += 1
            continue
        binding = row.get('binding') or {}
        if (any(binding.get(field) is None or binding.get(field) == '' for field in PAIR_FIELDS)
                or binding.get('case_id') != row.get('case_id')
                or binding.get('seed') != row.get('repeat', row.get('seed'))):
            excluded['invalid_fixed_inputs'] += 1
            continue
        key = (row.get('case_id'), row.get('repeat', row.get('seed')), arm)
        if key in seen:
            raise ValueError('同一用例、重复序号和模式有重复结果')
        seen.add(key)
        categories[row.get('category', row.get('family', 'unspecified'))].append(row)
    results = []
    for category, entries in sorted(categories.items()):
        pairs = defaultdict(dict)
        for row in entries:
            pairs[(row.get('case_id'), row.get('repeat', row.get('seed')))][row['arm']] = row
        complete = [pair for pair in pairs.values() if set(pair) == {'pipeline', 'hybrid'}
                    and all(pair['pipeline']['binding'][field] == pair['hybrid']['binding'][field]
                            for field in PAIR_FIELDS)]
        modes = {arm: aggregate(pair[arm] for pair in complete) for arm in ('pipeline', 'hybrid')}
        cases = {pair['pipeline']['case_id'] for pair in complete}
        control_failures = [row.get('run_id') or row.get('case_id') for pair in complete for row in pair.values()
                            if (row.get('known_bug') is False or str(category).upper() in {'CLEAN', 'G'})
                            and (row.get('has_patch') or row.get('patch_files', 0) > 0
                                 or row.get('patch_lines', 0) > 0)]
        pipeline, hybrid = modes['pipeline'], modes['hybrid']
        claim_coverage = all(metric['internal_successes'] == metric['scored_internal_successes']
                             for metric in modes.values())
        false_ok = (hybrid['false_successes'] == 0 if not pipeline['scored_internal_successes']
                    else hybrid['false_successes'] * pipeline['scored_internal_successes']
                        <= pipeline['false_successes'] * hybrid['scored_internal_successes'])
        passed = (hybrid['independently_valid'] >= pipeline['independently_valid']
                  and false_ok and claim_coverage and not control_failures)
        repeats = {case: sum(pair['pipeline']['case_id'] == case for pair in complete) for case in cases}
        status = ('insufficient_samples' if len(cases) < minimum_cases or len(complete) < len(pairs)
                  or any(count < minimum_repeats for count in repeats.values())
                  else 'pass' if passed else 'fail')
        results.append({'category': category, 'status': status, 'observed_pass': passed,
                        'paired_runs': len(complete), 'unpaired_runs': len(pairs) - len(complete),
                        'distinct_cases': len(cases), 'repeats_per_case': repeats, 'metrics': modes,
                        'control_patch_runs': control_failures})
    missing_categories = [category for category in expected_categories if category not in categories]
    status = ('pending' if missing_categories or excluded or not results
              or any(item['status'] == 'insufficient_samples' for item in results)
              else 'fail' if any(item['status'] == 'fail' for item in results) else 'pass')
    return {'status': status, 'categories': results, 'excluded': dict(excluded),
            'expected_categories': list(expected_categories), 'missing_categories': missing_categories,
            'attempted_rows': len(rows), 'minimum_cases': minimum_cases, 'minimum_repeats': minimum_repeats,
            'decision': '仅报告门禁结果；项目回退与全局默认值变更由产品确认'}
