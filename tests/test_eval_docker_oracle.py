import json
from pathlib import Path
import subprocess

import pytest

from evals.docker_oracle import (DockerOracle, bind_mount, digest, export_candidate,
                                score_request, tree_hash, validate_request)


def git(root, *arguments):
    return subprocess.run(['git', '-C', str(root), *arguments], check=True, capture_output=True).stdout


@pytest.fixture
def candidate_request(tmp_path):
    repository = tmp_path / 'repository'
    (repository / 'src').mkdir(parents=True)
    (repository / 'server').mkdir()
    (repository / 'src/value.ts').write_bytes(b'export const saved = true;\n')
    (repository / 'server/task.mjs').write_bytes(b'export const title = true;\n')
    git(repository, 'init', '-q')
    git(repository, 'config', 'core.autocrlf', 'false')
    git(repository, '-c', 'user.name=Oracle test', '-c', 'user.email=oracle@example.invalid', 'add', '.')
    git(repository, '-c', 'user.name=Oracle test', '-c', 'user.email=oracle@example.invalid', 'commit', '-qm', '冻结测试源码')
    revision = git(repository, 'rev-parse', 'HEAD').decode().strip()
    private = tmp_path / 'private'
    private.mkdir()
    script = private / 'oracle.mjs'
    script.write_text('private GUI Oracle', encoding='utf-8')
    mutation = {'file': 'src/value.ts', 'before': 'saved = true', 'after': 'saved = false'}
    (repository / 'src/value.ts').write_bytes(b'export const saved = false;\n')
    defective_hash = tree_hash(repository)
    reverse = git(repository, 'diff', '--no-ext-diff', '--no-textconv', '-R')
    patch = private / 'candidate.diff'
    patch.write_bytes(reverse)
    binding = {'run_id': 'oracle-run', 'group_id': 'hybrid-single-memory-off', 'case_id': 'B01',
               'source_revision': revision, 'source_hash': defective_hash, 'seed': 7,
               'environment_digest': 'fixed-environment', 'spec_hash': '1' * 64,
               'protocol_hash': '2' * 64, 'patch_hash': digest(reverse)}
    request = {'source_repository': str(repository), 'source_revision': revision, 'case_id': 'B01',
               'mutations': [mutation], 'oracle_cases': ['B01'], 'candidate_patch': str(patch),
               'oracle_script': str(script), 'binding': binding, 'report_path': str(private / 'report.json')}
    request_path = private / 'request.json'
    request_path.write_text(json.dumps(request), encoding='utf-8')
    return repository, private, request, request_path


def test_private_export_uses_fixed_revision_mutation_and_candidate_hash(candidate_request):
    repository, private, request, _ = candidate_request
    (repository / 'server/task.mjs').write_bytes(b'host dirty content must not be exported')
    prepared = export_candidate(request, private)
    assert prepared['source_hash'] == request['binding']['source_hash']
    assert prepared['candidate_source_hash'] != prepared['source_hash']
    assert prepared['source'].joinpath('src/value.ts').read_text(encoding='utf-8') == 'export const saved = true;\n'
    assert prepared['source'].joinpath('server/task.mjs').read_bytes() == b'export const title = true;\n'
    assert not any(path.name == 'oracle.mjs' for path in prepared['source'].rglob('*'))


@pytest.mark.parametrize('damage', ['patch', 'source', 'revision', 'case', 'outside_patch', 'oracle'])
def test_mismatched_binding_or_private_inputs_fail_closed(candidate_request, damage):
    repository, private, request, _ = candidate_request
    if damage == 'patch':
        Path(request['candidate_patch']).write_bytes(b'forged patch')
    elif damage == 'source':
        request['binding']['source_hash'] = '0' * 64
    elif damage == 'revision':
        request['source_revision'] = '0' * 40
    elif damage == 'case':
        request['case_id'] = 'B06'
    elif damage == 'outside_patch':
        outside = private.parent / 'untrusted.diff'
        outside.write_bytes(Path(request['candidate_patch']).read_bytes())
        request['candidate_patch'] = str(outside)
    else:
        request['oracle_script'] = str(repository / 'src/value.ts')
    with pytest.raises(ValueError):
        export_candidate(request, private)


def test_multiple_mutations_and_clean_empty_candidate(candidate_request):
    repository, private, request, _ = candidate_request
    second = {'file': 'server/task.mjs', 'before': 'title = true', 'after': 'title = false'}
    request['mutations'].append(second)
    request['oracle_cases'].append('B11')
    request['case_id'] = request['binding']['case_id'] = 'M01'
    (repository / 'server/task.mjs').write_bytes(b'export const title = false;\n')
    request['binding']['source_hash'] = tree_hash(repository)
    Path(request['candidate_patch']).write_bytes(b'')
    request['binding']['patch_hash'] = digest(b'')
    prepared = export_candidate(request, private)
    assert prepared['candidate_source_hash'] == request['binding']['source_hash']
    assert b'false' in prepared['source'].joinpath('server/task.mjs').read_bytes()
    request['case_id'] = request['binding']['case_id'] = 'CLEAN'
    request['mutations'], request['oracle_cases'] = [], []
    git(repository, 'checkout', '--', '.')
    request['binding']['source_hash'] = tree_hash(repository)
    prepared = export_candidate(request, private)
    assert prepared['candidate_source_hash'] == request['binding']['source_hash']


def test_candidate_cannot_change_private_or_protected_files(candidate_request):
    _, private, request, _ = candidate_request
    patch = b'diff --git a/package.json b/package.json\nnew file mode 100644\n--- /dev/null\n+++ b/package.json\n@@ -0,0 +1 @@\n+{}\n'
    Path(request['candidate_patch']).write_bytes(patch)
    request['binding']['patch_hash'] = digest(patch)
    with pytest.raises(ValueError, match='permitted application source'):
        export_candidate(request, private)


def test_score_writes_only_fresh_private_report(candidate_request):
    _, private, request, request_path = candidate_request

    class Executor:
        def score(self, values, prepared):
            return {'case_id': values['case_id'], 'binding': prepared['binding'], 'passed': True,
                    'fix': {'passed': True}, 'regression': {'passed': True}}

    result = score_request(request_path, executor=Executor())
    assert json.loads(Path(request['report_path']).read_text(encoding='utf-8')) == result
    with pytest.raises(ValueError, match='fresh'):
        score_request(request_path, executor=Executor())


def test_bind_mount_handles_windows_commas():
    assert 'readonly' in bind_mount(Path('source,with-comma'), '/oracle')


def test_container_inventory_rejects_private_mount_in_application(tmp_path):
    source, oracle = tmp_path / 'source', tmp_path / 'oracle'
    records = []
    for suffix, image, root, destination in [('-app', 'app-id', source, '/app'),
                                              ('-browser', 'browser-id', oracle, '/oracle')]:
        records.append({'Name': '/test' + suffix, 'Image': image, 'State': {'Running': True},
            'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True, 'NetworkMode': 'test-net'},
            'Mounts': [{'Type': 'bind', 'Source': str(root), 'Destination': destination, 'RW': False}],
            'NetworkSettings': {'Networks': {'test-net': {}}}})
    DockerOracle.verify_containers(records, 'test', 'test-net', 'app-id', 'browser-id', source, oracle)
    records[0]['Mounts'].append({'Type': 'bind', 'Source': str(oracle), 'Destination': '/private', 'RW': False})
    with pytest.raises(ValueError, match='boundary'):
        DockerOracle.verify_containers(records, 'test', 'test-net', 'app-id', 'browser-id', source, oracle)
