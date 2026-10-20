import dataclasses
import hashlib
import subprocess

import pytest

from evals.config import AblationGroup, EvaluationConfig
from evals.runner import FourCellRunner, _tree_hash


@pytest.fixture
def configuration(tmp_path):
    source = tmp_path / 'repository'
    (source / 'src').mkdir(parents=True)
    (source / 'src/value.py').write_bytes(b'first = True\nsecond = True\n')
    for arguments in (['init', '-q'], ['config', 'user.email', 'fixture@example.invalid'],
                      ['config', 'user.name', '评测测试'], ['add', '.'], ['commit', '-qm', '冻结测试源码']):
        subprocess.run(['git', '-C', str(source), *arguments], check=True, capture_output=True)
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              check=True, capture_output=True, text=True).stdout.strip()
    inputs = {}
    for name in ('profile', 'spec', 'public_protocol', 'initial_experience'):
        inputs[name] = tmp_path / (name + '.json')
        inputs[name].write_bytes(b'{}')
    skills = tmp_path / 'skills'
    skills.mkdir()
    return EvaluationConfig(case_id='M01', family='combined', source_root=source,
        source_revision=revision, skills_root=skills, goal='验证两处状态持久化', model='fixture',
        seed=0, tools_hash='tools', recovery_rules_hash='recovery', mutations=(
            {'file': 'src/value.py', 'before': 'first = True', 'after': 'first = False'},
            {'file': 'src/value.py', 'before': 'second = True', 'after': 'second = False'}), **inputs)


def test_runner_injects_multiple_mutations_before_adapter_and_closes_after_scoring(configuration, tmp_path):
    events = []

    class Adapter:
        def __call__(self, source, root, **kwargs):
            events.append('adapter')
            assert (source / 'src/value.py').read_bytes() == b'first = False\nsecond = False\n'
            assert _tree_hash(source) == kwargs['binding']['source_hash']
            candidate = root / 'candidate.patch'
            candidate.write_bytes(b'')
            return {'candidate_patch': str(candidate), 'repair_mode': 'hybrid',
                    'materialized_patch_hash': hashlib.sha256(b'').hexdigest(),
                    'source': 'scripted', 'real_model': False}

        def close(self):
            events.append('close')

    class Oracle:
        def score(self, binding, candidate, **kwargs):
            assert events == ['adapter']
            events.append('oracle')
            return {'status': 'scored', 'oracle_passed': False,
                    'candidate_patch_hash': binding['patch_hash']}

    row = FourCellRunner(configuration, tmp_path / 'runs', adapter=Adapter(), oracle=Oracle()).run_group(
        AblationGroup('multi', False, 'hybrid'))
    assert events == ['adapter', 'oracle', 'close']
    assert row['evidence_kind'] == 'inconclusive'
    assert not row['has_patch']
    assert row['oracle_patch_hash'] == row['patch_hash']


def test_runner_rejects_ambiguous_injection_and_still_closes(configuration, tmp_path):
    closed = []

    class Adapter:
        def __call__(self, **kwargs):
            pytest.fail('无效冻结注入不应启动 Agent')

        def close(self):
            closed.append(True)

    config = dataclasses.replace(configuration, mutations=(
        {'file': 'src/value.py', 'before': 'missing', 'after': 'changed'},))
    with pytest.raises(ValueError, match='必须唯一'):
        FourCellRunner(config, tmp_path / 'runs', adapter=Adapter()).run_group(
            AblationGroup('multi', False, 'hybrid'))
    assert closed == [True]
