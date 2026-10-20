import json
from pathlib import Path
import subprocess

import pytest

from evals.e1 import FrozenOracle, _require_separate
from evals.docker_worker import worker_owned_source
from evals.freeze import freeze_cases, load_frozen_suite, verify_frozen_suite
from evals.oracle_bridge import OracleBridge
from tracefix.runtime.contracts import digest


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_oracle_evidence_does_not_modify_frozen_inventory(tmp_path, monkeypatch):
    source = ROOT / '.tracefix/repositories/test-bug-repo'
    if not (source / '.git').is_dir():
        pytest.skip('Pinned external repository is not cached')
    skills = tmp_path / 'skills'
    skills.mkdir()
    suite = tmp_path / 'suite'
    manifest = freeze_cases(source_root=source, output=suite,
        profile=ROOT / 'profiles/bugboard.yaml', skills_root=skills, model='test-fixture-model',
        tools_manifest={}, recovery_manifest={}, private_assets={
            'docker_oracle.py': ROOT / 'evals/docker_oracle.py',
            'private_oracle/oracle.mjs': ROOT / 'evals/private_oracle/oracle.mjs'})
    config = load_frozen_suite(suite)[0]

    class Adapter:
        def boundary(self):
            return self

        def verify(self, private_root, private_files):
            assert Path(private_root) == tmp_path / 'private'
            assert suite / 'private/private_oracle/oracle.mjs' in private_files
            return {'real_isolation': True, 'test_fixture_only': True}

    oracle = FrozenOracle(config, suite, Adapter(), private_root=tmp_path / 'private')
    patch = tmp_path / 'candidate.patch'
    patch.write_bytes(b'')
    binding = {'run_id': 'unit-run', 'group_id': 'unit-group', 'case_id': config.case_id,
        'source_revision': config.source_revision, 'source_hash': '1' * 64,
        'environment_digest': 'unit-environment', 'seed': 0, 'spec_hash': '2' * 64,
        'protocol_hash': '3' * 64, 'patch_hash': digest(b'')}

    def execute(command, **kwargs):
        request_path = Path(command[-1])
        request = json.loads(request_path.read_text(encoding='utf-8'))
        assert request_path.is_relative_to(tmp_path / 'private')
        assert request['oracle_cases'] == ['B01']
        result = {'case_id': config.case_id, 'binding': binding, 'passed': True,
                  'patch_hash': binding['patch_hash'], 'source_hash': binding['source_hash']}
        return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b'')

    monkeypatch.setattr('evals.oracle_bridge.subprocess.run', execute)
    assert oracle.score(binding, patch, verify_candidate=lambda: digest(b'')) == {'status': 'settled'}
    assert oracle.score(binding, patch, verify_candidate=lambda: digest(b'')) == {'status': 'settled'}
    records = [json.loads(line) for line in (oracle.private_root / 'ledger.jsonl').read_text().splitlines()]
    assert [record['status'] for record in records] == ['scored', 'duplicate_request']
    monkeypatch.undo()
    assert verify_frozen_suite(suite)['manifest_hash'] == manifest['manifest_hash']


@pytest.mark.parametrize('private', ['suite', 'suite/private-run', '.'])
def test_run_evidence_cannot_overlap_frozen_assets(tmp_path, private):
    with pytest.raises(ValueError, match='独立目录'):
        _require_separate(tmp_path / private, tmp_path / 'suite')


@pytest.mark.parametrize('seed', [True, False, '0', None])
def test_oracle_seed_must_be_an_integer(seed):
    binding = {key: 'unit-value' for key in (
        'run_id', 'group_id', 'case_id', 'source_revision', 'source_hash',
        'environment_digest', 'spec_hash', 'protocol_hash')}
    binding.update(seed=seed, patch_hash=digest(b''))
    with pytest.raises(ValueError, match='binding'):
        OracleBridge._validate_binding(binding)


def test_worker_source_preserves_git_metadata_and_requires_fresh_destination(tmp_path):
    source = tmp_path / 'source'
    (source / '.git').mkdir(parents=True)
    (source / '.git/HEAD').write_text('ref: refs/heads/main\n')
    (source / 'app.py').write_text('print("frozen")\n')
    destination = worker_owned_source(tmp_path)
    assert destination == tmp_path / 'data/worker-source'
    assert (destination / '.git/HEAD').read_bytes() == (source / '.git/HEAD').read_bytes()
    (destination / 'app.py').write_text('print("candidate")\n')
    assert (source / 'app.py').read_text() == 'print("frozen")\n'
    with pytest.raises(FileExistsError, match='fresh'):
        worker_owned_source(tmp_path)


def test_worker_source_rejects_symlink_before_copy(tmp_path):
    source = tmp_path / 'source'
    (source / '.git').mkdir(parents=True)
    target = tmp_path / 'external.txt'
    target.write_text('private')
    try:
        (source / 'linked.txt').symlink_to(target)
    except OSError:
        pytest.skip('Filesystem cannot create symlinks')
    with pytest.raises(ValueError, match='link'):
        worker_owned_source(tmp_path)
    assert not (tmp_path / 'data/worker-source').exists()
