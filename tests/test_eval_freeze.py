import dataclasses
import json
from pathlib import Path
import shutil

import pytest

from evals.cases import CASES, REVISIONS, case_mutations, frozen_cases
from evals.config import EvaluationConfig
from evals.freeze import freeze_cases, freeze_suite, load_frozen_suite, verify_anchors, verify_frozen_suite
from tracefix.runtime.contracts import TestSpec, digest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.tracefix/repositories/test-bug-repo'
TOOLS = {'test_fixture': True, 'tools': ['Read', 'RunTests']}
RECOVERY = {'test_fixture': True, 'attempts': 1}


@pytest.fixture
def prepared_inputs(tmp_path):
    if not (SOURCE / '.git').is_dir():
        pytest.skip('Pinned external repository is not cached')
    skills = tmp_path / 'skills'
    skills.mkdir()
    (skills / 'public.md').write_text('Use public runtime checks.\n', encoding='utf-8')
    return {'source_root': SOURCE, 'profile': ROOT / 'profiles/bugboard.yaml',
            'skills_root': skills, 'model': 'test-fixture-model',
            'tools_manifest': TOOLS, 'recovery_manifest': RECOVERY}


@pytest.fixture
def frozen_smoke(tmp_path, prepared_inputs):
    output = tmp_path / 'frozen'
    manifest = freeze_cases(output=output, **prepared_inputs)
    return output, manifest


def test_mutations_compatibility_and_single_hash_are_stable(frozen_smoke):
    configs = load_frozen_suite(frozen_smoke[0])
    config = next(item for item in configs if item.case_id == 'B01')
    mutation = config.case_mutations()[0]
    legacy = dataclasses.replace(config, mutations=(), mutation_file=mutation['file'],
                                 mutation_before=mutation['before'], mutation_after=mutation['after'])
    assert config.case_mutations() == legacy.case_mutations() == case_mutations(CASES[0])
    assert config.mutation_hash() == legacy.mutation_hash() == digest([
        mutation['file'], mutation['before'], mutation['after']])
    multi = next(item for item in configs if item.case_id == 'M01')
    assert len(multi.case_mutations()) == 2
    assert multi.mutation_hash() == digest(list(multi.case_mutations()))
    clean = next(item for item in configs if item.case_id == 'CLEAN-e7')
    assert clean.mutation_hash() == digest([None, None, None])


def test_smoke_pins_real_revisions_and_keeps_private_data_out_of_public_assets(frozen_smoke):
    output, manifest = frozen_smoke
    assert [entry['case_id'] for entry in manifest['cases']] == ['B01', 'B06', 'B11', 'M01', 'CLEAN-e7']
    assert {entry['fullrev'] for entry in manifest['cases']} == {REVISIONS['e7']}
    assert manifest['tools_hash'] == digest(TOOLS)
    assert manifest['recovery_rules_hash'] == digest(RECOVERY)
    entries = {entry['case_id']: entry for entry in manifest['cases']}
    assert entries['M01']['oracle_cases'] == ['B01', 'B11']
    assert entries['CLEAN-e7']['mutations'] == []
    assert entries['CLEAN-e7']['oracle_cases'] == []
    public = '\n'.join(path.read_text(encoding='utf-8') for path in (output / 'public').rglob('*') if path.is_file())
    for config in load_frozen_suite(output):
        assert 'mutation_before' not in config.agent_payload()
        assert 'mutations' not in config.agent_payload()
        assert 'source_root' not in config.agent_payload()
        spec = TestSpec.model_validate_json(config.spec.read_bytes())
        assert spec.behavior_scenarios
        for mutation in config.case_mutations():
            assert mutation['before'] not in public
            assert mutation['after'] not in public
    assert all(entry['admission_status'].startswith('pending') for entry in entries.values())


def test_frozen_paths_remain_loadable_after_copy(frozen_smoke, tmp_path):
    output, original = frozen_smoke
    relocated = tmp_path / 'relocated'
    shutil.copytree(output, relocated)
    configs = load_frozen_suite(relocated)
    assert verify_frozen_suite(relocated)['manifest_hash'] == original['manifest_hash']
    for config in configs:
        assert config.source_root.is_relative_to(relocated)
        assert config.spec.is_relative_to(relocated)
        assert config.skills_root.is_relative_to(relocated)
        assert config.fixed_inputs()['mutation_hash'] == config.mutation_hash()


@pytest.mark.parametrize('target', ['public', 'private/configs', 'private/tools.json', 'manifest.json'])
def test_frozen_manifest_rejects_asset_or_manifest_drift(frozen_smoke, target):
    output, _ = frozen_smoke
    path = output / target
    if path.is_dir():
        path = next(candidate for candidate in path.rglob('*') if candidate.is_file())
    path.write_bytes(path.read_bytes() + b' ')
    if target == 'manifest.json':
        value = json.loads(path.read_text(encoding='utf-8'))
        value['status'] = 'tampered'
        path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='漂移|摘要'):
        verify_frozen_suite(output)


def test_existing_output_is_never_reused(frozen_smoke, prepared_inputs):
    with pytest.raises(FileExistsError):
        freeze_cases(output=frozen_smoke[0], **prepared_inputs)


def test_manifest_rejects_new_unlisted_files(frozen_smoke):
    output, _ = frozen_smoke
    (output / 'private/ledger.jsonl').write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='漂移'):
        verify_frozen_suite(output)


def test_real_standard_anchors_and_three_revisions_are_validated(prepared_inputs):
    cases = frozen_cases('standard')
    assert len(cases) == 22
    assert len({case['id'] for case in cases}) == len(cases)
    assert {case['revision'] for case in cases} == set(REVISIONS.values())
    total = 0
    for case in cases:
        evidence = verify_anchors(SOURCE, case['revision'], case_mutations(case))
        assert len(evidence) == len(case_mutations(case))
        total += len(evidence)
        verify_anchors(SOURCE, case['good_revision'], ())
    assert total == 18
    assert all(case['oracle_status'] == 'pending' for case in cases if case['category'] == 'N')


def test_anchor_checks_apply_multiple_changes_in_order(prepared_inputs):
    mutation = case_mutations(CASES[0])[0]
    second = {'file': mutation['file'], 'before': mutation['after'], 'after': mutation['before']}
    assert len(verify_anchors(SOURCE, REVISIONS['e7'], [mutation, second])) == 2
    with pytest.raises(ValueError, match='恰好'):
        verify_anchors(SOURCE, REVISIONS['e7'], [mutation, mutation])


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/outside', 'C:\\outside', '.git/config'])
def test_config_rejects_mutation_path_escape(frozen_smoke, path):
    config = load_frozen_suite(frozen_smoke[0])[0]
    with pytest.raises(ValueError, match='路径'):
        dataclasses.replace(config, mutations=({'file': path, 'before': 'old', 'after': 'new'},))


def test_freeze_checks_tool_and_recovery_bindings(frozen_smoke, tmp_path):
    configs = load_frozen_suite(frozen_smoke[0])
    with pytest.raises(ValueError, match='摘要'):
        freeze_suite(configs, tmp_path / 'invalid', tools_manifest={'changed': True},
                     recovery_manifest=RECOVERY)
    assert not (tmp_path / 'invalid').exists()


def test_standard_freezes_pending_cases_without_claiming_oracle_readiness(tmp_path, prepared_inputs):
    output = tmp_path / 'standard'
    manifest = freeze_cases(output=output, tier='standard', **prepared_inputs)
    assert len(load_frozen_suite(output)) == 22
    assert manifest['status'] == 'prepared_not_evaluated'
    assert all(entry['public_spec_validation'] == 'schema_only_not_browser_replayed'
               for entry in manifest['cases'])
