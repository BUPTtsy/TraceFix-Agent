import dataclasses
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.config import AblationGroup, EvaluationConfig
from evals.mode_gate import evaluate_mode_gate
from evals.run_matrix import run_matrix
from evals.runner import FourCellRunner


def row(case='case', arm='hybrid', **updates):
    result = dict(case_id=case, repeat=0, arm=arm, category='B', repair_mode_actual=arm,
                held_out=True, evidence_kind='real', oracle_passed=True, oracle_status='passed',
                trace_summary={'oracle_isolation_proven': True, 'trace_proven': True},
                agent_mode='multi', cross_run_memory=False, track='compare',
                patch_hash='a', oracle_patch_hash='a', internal_success=True, patch_files=1)
    result.update(updates)
    result['binding'] = {field: 'fixed' for field in ('family', 'source_revision', 'source_hash',
        'environment_digest', 'spec_hash', 'protocol_hash', 'model', 'effort', 'tools_hash',
        'skill_hash', 'initial_experience_hash', 'recovery_rules_hash')}
    result['binding'].update(case_id=case, seed=result['repeat'])
    return result


def paired_rows(category='B'):
    control = category in {'CLEAN', 'G'}
    return [row(f'{category}-{i}', arm, repeat=repeat, category=category,
                known_bug=not control, patch_files=0 if control else 1)
            for i in range(5) for repeat in range(3)
            for arm in ('pipeline', 'hybrid')]


def test_formal_gate_requires_all_categories_and_preserves_partial_observations():
    rows = paired_rows()
    report = evaluate_mode_gate(rows)
    assert report['status'] == 'pending'
    assert report['expected_categories'] == ['B', 'M', 'N', 'V', 'CLEAN', 'G']
    assert report['missing_categories'] == ['M', 'N', 'V', 'CLEAN', 'G']
    assert len(report['categories']) == 1
    assert report['categories'][0]['category'] == 'B'
    assert report['categories'][0]['status'] == 'pass'
    local = evaluate_mode_gate(rows, expected_categories=('B',))
    assert local['status'] == 'pass'
    assert local['expected_categories'] == ['B']
    assert local['missing_categories'] == []


def test_formal_gate_passes_with_complete_category_coverage():
    categories = ('B', 'M', 'N', 'V', 'CLEAN', 'G')
    rows = [item for category in categories for item in paired_rows(category)]
    report = evaluate_mode_gate(rows)
    assert report['status'] == 'pass'
    assert report['missing_categories'] == []
    assert {item['category'] for item in report['categories']} == set(categories)
    assert all(item['status'] == 'pass' for item in report['categories'])


def test_mode_gate_requires_paired_heldout_isolated_evidence():
    rows = paired_rows()
    assert evaluate_mode_gate(rows, expected_categories=('B',))['status'] == 'pass'
    for field, value in [('held_out', False), ('evidence_kind', 'dev'), ('repair_mode_actual', 'pipeline'),
                         ('oracle_patch_hash', 'tampered'), ('oracle_patch_hash', None),
                         ('real_model', False), ('source', 'scripted')]:
        changed = [dict(item) for item in rows]
        changed[1][field] = value
        assert evaluate_mode_gate(changed, expected_categories=('B',))['status'] == 'pending'
    assert evaluate_mode_gate(rows[:2], expected_categories=('B',))['status'] == 'pending'
    assert evaluate_mode_gate([item for item in rows if item['repeat'] == 0],
                              expected_categories=('B',))['status'] == 'pending'
    rows[1]['trace_summary']['trace_proven'] = False
    assert evaluate_mode_gate(rows, expected_categories=('B',))['status'] == 'pending'


def test_gate_detects_false_success_and_control_mutation():
    rows = paired_rows()
    rows[1]['oracle_passed'] = False
    assert evaluate_mode_gate(rows, expected_categories=('B',))['status'] == 'fail'
    rows[1]['oracle_passed'] = True
    rows[1]['known_bug'] = False
    report = evaluate_mode_gate(rows, expected_categories=('B',))
    assert report['status'] == 'fail' and report['categories'][0]['control_patch_runs']
    with pytest.raises(ValueError, match='重复'):
        evaluate_mode_gate(rows + [rows[0]], expected_categories=('B',))


@pytest.mark.parametrize('field,value', [('track', 'capability'), ('category', 'U'), ('category', 'C')])
def test_capability_cases_never_enter_mode_gate(field, value):
    rows = paired_rows()
    for item in rows:
        item[field] = value
    report = evaluate_mode_gate(rows, expected_categories=('B',))
    assert report['status'] == 'pending' and not report['categories']
    assert report['excluded']['capability_track'] == len(rows)


@pytest.mark.parametrize('field', ['source_revision', 'model', 'spec_hash', 'environment_digest', 'tools_hash'])
def test_pairs_require_identical_fixed_inputs(field):
    rows = paired_rows()
    rows[1]['binding'][field] = 'different'
    assert evaluate_mode_gate(rows, expected_categories=('B',))['status'] == 'pending'


def test_control_gate_detects_deletion_and_nonempty_patch_even_without_file_count():
    rows = paired_rows()
    rows[1].update(known_bug=False, patch_files=0, patch_lines=1, has_patch=True)
    assert evaluate_mode_gate(rows, expected_categories=('B',))['status'] == 'fail'


@pytest.fixture
def run_deleted_candidate(tmp_path, monkeypatch):
    """Exercise candidate parsing with simulated runtime/Oracle evidence only."""
    source_root = tmp_path / 'input-source'
    source_root.mkdir()
    skills = tmp_path / 'skills'
    skills.mkdir()
    inputs = {}
    for name in ('profile', 'spec', 'public_protocol', 'initial_experience'):
        path = tmp_path / (name + '.json')
        path.write_text('{}', encoding='utf-8')
        inputs[name] = path
    config = EvaluationConfig(case_id='deletion', family='original-family',
        source_root=source_root, source_revision='a' * 40, skills_root=skills,
        goal='Repair the reported behavior', model='fixture-model', seed=0,
        tools_hash='fixture-tools', recovery_rules_hash='fixture-recovery', **inputs)
    patch = (b'diff --git a/src/obsolete.py b/src/obsolete.py\n'
             b'deleted file mode 100644\n'
             b'--- a/src/obsolete.py\n'
             b'+++ /dev/null\n'
             b'@@ -1 +0,0 @@\n'
             b'-obsolete = True\n')

    def export_source(_source, revision, destination):
        # Keep Git setup outside this candidate-parsing regression.
        (destination / 'src').mkdir(parents=True)
        (destination / 'src/obsolete.py').write_text('obsolete = True\n', encoding='utf-8')
        return revision

    monkeypatch.setattr('evals.runner._copy_source', export_source)

    def adapter(*, source, group, **_kwargs):
        (source / 'src/obsolete.py').unlink()
        (source / 'candidate.patch').write_bytes(patch)
        return {'status': 'completed', 'outcome': 'FIX_VERIFIED', 'internal_success': True,
                'repair_mode': group.arm, 'candidate_patch': 'candidate.patch',
                'materialized_patch_hash': hashlib.sha256(patch).hexdigest(),
                'trace_origin': 'runtime_audit', 'gui_real': True, 'trace': [
                    {'event': 'model_call', 'model_agent_id': 'parent', 'response_audited': True},
                    {'event': 'model_call', 'model_agent_id': 'child', 'response_audited': True},
                    {'event': 'delegation'}]}

    class SimulatedOracle:
        def score(self, binding, candidate, *, verify_candidate):
            assert candidate.read_bytes() == patch
            assert verify_candidate() == binding['patch_hash']
            return {'status': 'passed', 'oracle_passed': True,
                    'candidate_patch_hash': binding['patch_hash'],
                    'boundary': {'real_isolation': True},
                    'boundary_after': {'real_isolation': True}}

    def run(*, adapter_metadata=None, **updates):
        configured = dataclasses.replace(config, **updates)

        def configured_adapter(**kwargs):
            result = adapter(**kwargs)
            result.update(adapter_metadata or {})
            return result

        runner = FourCellRunner(configured, tmp_path / 'runs',
                                adapter=configured_adapter, oracle=SimulatedOracle())
        rows = [runner.run_group(AblationGroup('multi', False, arm))
                for arm in ('pipeline', 'hybrid')]
        return configured, rows

    return run


def test_run_group_counts_deleted_candidate_and_rejects_control_patch(run_deleted_candidate):
    config, rows = run_deleted_candidate(category='CLEAN', known_bug=False)
    for result in rows:
        assert result['patch_files'] == 1
        assert result['patch_lines'] == 1
        assert result['has_patch'] is True
        assert result['patch_hash'] == result['oracle_patch_hash'] == result['materialized_patch_hash']
        assert not result['infra_failure']
        assert result['category'] == config.category
        assert result['family'] == config.family
        assert result['track'] == 'compare'
        assert result['known_bug'] is False
    report = evaluate_mode_gate(rows, minimum_cases=1, minimum_repeats=1,
                                expected_categories=('CLEAN',))
    assert report['status'] == 'fail'
    assert len(report['categories'][0]['control_patch_runs']) == 2


@pytest.mark.parametrize('category', ['unit', 'compile'])
def test_run_group_preserves_capability_metadata_and_excludes_it(run_deleted_candidate, category):
    config, rows = run_deleted_candidate(category=category, track='capability', known_bug=True)
    for result in rows:
        assert result['category'] == config.category == category
        assert result['family'] == config.family != category
        assert result['track'] == config.track == 'capability'
        assert result['known_bug'] is config.known_bug is True
    report = evaluate_mode_gate(rows, minimum_cases=1, minimum_repeats=1,
                                expected_categories=(category,))
    assert report['status'] == 'pending'
    assert not report['categories']
    assert report['excluded'] == {'capability_track': 2}


def test_run_group_defaults_category_to_legacy_family(run_deleted_candidate):
    config, rows = run_deleted_candidate()
    for result in rows:
        assert result['category'] == result['family'] == config.family
        assert result['track'] == 'compare'
        assert result['known_bug'] is None
        assert result['source'] is None
        assert result['real_model'] is None
        assert result['trace_summary']['trace_proven'] is True


@pytest.mark.parametrize('metadata', [
    {'source': 'scripted'}, {'real_model': False}, {'source': 'scripted', 'real_model': False}])
def test_run_group_preserves_fake_markers_and_rejects_formal_evidence(run_deleted_candidate, metadata):
    _, rows = run_deleted_candidate(category='B', known_bug=True, adapter_metadata=metadata)
    for result in rows:
        for field, value in metadata.items():
            assert result[field] == value
        assert result['evidence_kind'] == 'inconclusive'
        assert result['trace_summary']['trace_proven'] is False
    report = evaluate_mode_gate(rows, minimum_cases=1, minimum_repeats=1,
                                expected_categories=('B',))
    assert report['status'] == 'pending'
    assert not report['categories']
    assert report['excluded'] == {'unproven_evidence': 2}


def test_hybrid_audited_model_does_not_require_optional_delegation():
    result = {'trace_origin': 'runtime_audit', 'gui_real': True, 'trace': [
        {'event': 'model_call', 'model_agent_id': 'one', 'response_audited': True}]}
    assert FourCellRunner._trace_summary(result, AblationGroup('multi', False, 'hybrid'))['trace_proven']
    assert not FourCellRunner._trace_summary(result, AblationGroup('multi', False, 'pipeline'))['trace_proven']
    result['trace'][0]['response_audited'] = False
    assert not FourCellRunner._trace_summary(result, AblationGroup('multi', False, 'hybrid'))['trace_proven']


def test_legacy_group_name_preserved_and_matrix_rotates_arms(tmp_path):
    assert AblationGroup('multi', False).name == 'multi-memory-off'
    assert AblationGroup('multi', False, 'hybrid').name == 'hybrid-multi-memory-off'

    @dataclasses.dataclass
    class Config:
        case_id: str = 'demo'
        seed: int = 999

    order = []

    class FakeRunner:
        def __init__(self, config, output):
            self.config = config

        def run_group(self, group):
            order.append((self.config.seed, group.arm))
            result = row(self.config.case_id, group.arm)
            result['repeat'] = self.config.seed
            result['binding']['seed'] = self.config.seed
            return result

    result = run_matrix([Config()], tmp_path / 'matrix', repeat=2, runner_factory=FakeRunner)
    assert order == [(0, 'pipeline'), (0, 'hybrid'), (1, 'hybrid'), (1, 'pipeline')]
    assert result['status'] == 'pending'
    assert result['expected_categories'] == ['B', 'M', 'N', 'V', 'CLEAN', 'G']
    assert result['missing_categories'] == ['M', 'N', 'V', 'CLEAN', 'G']
    assert len((tmp_path / 'matrix/rows.jsonl').read_text(encoding='utf-8').splitlines()) == 4
    with pytest.raises(FileExistsError):
        run_matrix([Config()], tmp_path / 'matrix', runner_factory=FakeRunner)
