"""Evaluator-only Docker scoring; copied outside every Agent execution surface."""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid


BINDING_FIELDS = ('run_id', 'group_id', 'case_id', 'source_revision', 'source_hash',
                  'environment_digest', 'seed', 'spec_hash', 'protocol_hash', 'patch_hash')
GUI_CASES = tuple(f'B{index:02d}' for index in range(1, 13))


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def plain_path(value):
    path = Path(value).absolute()
    if any(parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)()
           for parent in (path, *path.parents)):
        raise ValueError('linked evaluator path')
    return path.resolve()


def tree_hash(root):
    root = Path(root)
    files = {}
    for path in sorted(root.rglob('*')):
        if '.git' in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise ValueError('linked source file')
        if path.is_file():
            files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return digest(files)


def command(arguments, *, cwd=None, timeout=180, check=True):
    result = subprocess.run([str(item) for item in arguments], cwd=cwd, capture_output=True,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f'{arguments[0]} {arguments[1]} failed ({result.returncode}): '
                           + result.stderr.decode('utf-8', errors='replace')[-2000:])
    return result


def validate_request(request, private_root):
    binding = request.get('binding')
    if (not isinstance(binding, dict) or not set(BINDING_FIELDS) <= binding.keys()
            or any(not isinstance(binding[key], str) or not binding[key]
                   for key in BINDING_FIELDS if key != 'seed')
            or type(binding['seed']) is not int):
        raise ValueError('incomplete candidate binding')
    for key in ('source_hash', 'spec_hash', 'protocol_hash', 'patch_hash'):
        if re.fullmatch(r'[0-9a-f]{64}', binding[key]) is None:
            raise ValueError('invalid ' + key)
    if (request.get('case_id') != binding['case_id']
            or request.get('source_revision') != binding['source_revision']
            or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', binding['source_revision']) is None):
        raise ValueError('request binding mismatch')
    oracle_cases = request.get('oracle_cases')
    clean = request.get('known_bug') is False or binding['case_id'].split('-', 1)[0] == 'CLEAN'
    if not isinstance(oracle_cases, list) or len(set(oracle_cases)) != len(oracle_cases):
        raise ValueError('invalid private Oracle selection')
    if any(case not in GUI_CASES for case in oracle_cases) or (not oracle_cases and not clean):
        raise ValueError('unsupported private Oracle selection')
    if clean and oracle_cases:
        raise ValueError('clean control cannot select defect checks')
    mutations = request.get('mutations', [])
    if not isinstance(mutations, list) or (clean and mutations):
        raise ValueError('invalid private mutation list')
    for mutation in mutations:
        if (not isinstance(mutation, dict) or set(mutation) != {'file', 'before', 'after'}
                or any(not isinstance(value, str) or not value for value in mutation.values())
                or mutation['before'] == mutation['after']):
            raise ValueError('invalid private mutation')
        safe_source_path(mutation['file'])
    patch = plain_path(request['candidate_patch'])
    if not patch.is_relative_to(private_root) or not patch.is_file():
        raise ValueError('candidate patch must be copied into evaluator private root')
    script = plain_path(request.get('oracle_script') or Path(__file__).parent / 'private_oracle/oracle.mjs')
    if not script.is_relative_to(private_root) or not script.is_file():
        raise ValueError('Oracle script must be evaluator private')
    report = plain_path(request.get('report_path') or private_root / 'report.json')
    if not report.is_relative_to(private_root) or report.exists():
        raise ValueError('Oracle report must be fresh and evaluator private')
    return binding, patch, script, report


def safe_source_path(value):
    path = PurePosixPath(value)
    if ('\\' in value or ':' in value or path.is_absolute()
            or any(part in {'', '.', '..'} for part in value.split('/'))
            or len(path.parts) < 2 or path.parts[0] not in {'src', 'server'}):
        raise ValueError('candidate path outside permitted application source')
    return value


def export_candidate(request, private_root):
    binding, patch, script, report = validate_request(request, private_root)
    patch_bytes = patch.read_bytes()
    if digest(patch_bytes) != binding['patch_hash']:
        raise ValueError('candidate patch hash mismatch')
    repository = plain_path(request['source_repository'])
    revision = binding['source_revision']
    resolved = command(['git', '-C', repository, 'rev-parse', '--verify', revision + '^{commit}'])
    if resolved.stdout.decode().strip() != revision:
        raise ValueError('source revision mismatch')
    work = Path(tempfile.mkdtemp(prefix='oracle-', dir=private_root))
    source = work / 'source'
    source.mkdir()
    archive = command(['git', '-C', repository, 'archive', '--format=tar', revision])
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as stream:
        if any(member.issym() or member.islnk() for member in stream.getmembers()):
            raise ValueError('linked source archive')
        stream.extractall(source, filter='data')
    for mutation in request.get('mutations', []):
        target = source / mutation['file']
        text = target.read_text(encoding='utf-8')
        if text.count(mutation['before']) != 1:
            raise ValueError('mutation baseline does not match fixed revision')
        target.write_bytes(text.replace(mutation['before'], mutation['after'], 1).encode('utf-8'))
    source_hash = tree_hash(source)
    if source_hash != binding['source_hash']:
        raise ValueError('mutated source hash mismatch')
    copied_patch = work / 'candidate.diff'
    copied_patch.write_bytes(patch_bytes)
    command(['git', '-C', source, 'init', '-q'])
    if patch_bytes.strip():
        stats = command(['git', '-C', source, 'apply', '--numstat', '-z', copied_patch]).stdout
        changed_paths = []
        for record in stats.split(b'\0'):
            if record:
                fields = record.decode('utf-8').split('\t')
                if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                    raise ValueError('unsupported candidate patch')
                safe_source_path(fields[2])
                if not (source / fields[2]).is_file():
                    raise ValueError('candidate cannot create application files')
                changed_paths.append(fields[2])
        command(['git', '-C', source, 'apply', '--check', '--', copied_patch])
        command(['git', '-C', source, 'apply', '--', copied_patch])
        if any(not (source / path).is_file() or (source / path).is_symlink() for path in changed_paths):
            raise ValueError('candidate cannot delete or link application files')
    if digest(patch.read_bytes()) != binding['patch_hash']:
        raise ValueError('candidate changed during materialization')
    return {'binding': binding, 'patch': patch, 'script': script, 'report': report,
            'source': source, 'work': work, 'source_hash': source_hash,
            'candidate_source_hash': tree_hash(source), 'patch_hash': digest(patch_bytes)}


def bind_mount(source, destination):
    source = PureWindowsPath(str(source)).as_posix() if os.name == 'nt' else str(source)
    output = io.StringIO()
    csv.writer(output, lineterminator='').writerow(
        ['type=bind', 'src=' + source, 'dst=' + destination, 'readonly'])
    return output.getvalue()


def mount_source_identity(value):
    value = str(value).replace('\\', '/')
    for prefix in ('/run/desktop/mnt/host/', '/host_mnt/', '/mnt/'):
        if value.startswith(prefix):
            tail = value[len(prefix):]
            if re.match(r'^[A-Za-z]/', tail):
                value = tail[0] + ':' + tail[1:]
            break
    return value.lower() if re.match(r'^[A-Za-z]:/', value) else value


class DockerOracle:
    def __init__(self, *, docker='docker', timeout=180):
        self.docker, self.timeout = docker, timeout

    def run(self, *arguments, check=True):
        return command([self.docker, *arguments], timeout=self.timeout, check=check)

    def score(self, request, prepared):
        binding = prepared['binding']
        requested_images = [request.get('app_image', ''), request.get('browser_image', '')]
        if any(re.fullmatch(r'sha256:[0-9a-f]{64}', image) is None for image in requested_images):
            raise ValueError('Oracle requires frozen app/browser image IDs')
        images = json.loads(self.run('image', 'inspect', *requested_images).stdout)
        if len(images) != 2 or any(re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('Id', '')) is None for item in images):
            raise ValueError('immutable Oracle app/browser images unavailable')
        app_image, browser_image = [item['Id'] for item in images]
        if [app_image, browser_image] != requested_images:
            raise ValueError('Oracle image identity changed')
        name = 'tf-oracle-' + uuid.uuid4().hex[:16]
        network, volume = name + '-net', name + '-deps'
        source = prepared['source']
        for directory in ('dist', 'node_modules'):
            (source / directory).mkdir(exist_ok=True)
        oracle_root = prepared['work'] / 'oracle'
        oracle_root.mkdir()
        oracle_script = oracle_root / 'oracle.mjs'
        oracle_script.write_bytes(prepared['script'].read_bytes())
        gui_request = {'binding': binding, 'case_id': binding['case_id'],
                       'oracle_cases': request['oracle_cases'], 'url': 'http://app:3000'}
        (oracle_root / 'request.json').write_text(json.dumps(gui_request), encoding='utf-8')
        common = ['--network', network, '--label', 'tracefix.oracle=' + binding['run_id'],
                  '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                  '--pids-limit=256', '--memory=1g', '--cpus=2',
                  '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777']
        try:
            self.run('network', 'create', '--internal', '--label', 'tracefix.oracle=' + binding['run_id'], network)
            network_info = json.loads(self.run('network', 'inspect', network).stdout)
            if (len(network_info) != 1 or network_info[0].get('Internal') is not True
                    or network_info[0].get('Name') != network):
                raise ValueError('Oracle network is not internal')
            self.run('volume', 'create', '--label', 'tracefix.oracle=' + binding['run_id'], volume)
            self.run('run', '-d', '--name', name + '-app', *common, '--network-alias', 'app',
                     '--user', '1000:1000', '--mount', bind_mount(source, '/app'),
                     '--mount', 'type=volume,src=' + volume + ',dst=/app/node_modules,readonly',
                     '--tmpfs', '/app/dist:rw,nosuid,size=128m,mode=1777',
                     '-e', 'HOME=/tmp', '-e', 'TRACEFIX_SOURCE=' + prepared['candidate_source_hash'],
                     app_image, 'node', 'scripts/serve.mjs')
            health = ("let ok=false;for(let index=0;index<60;index++){try{const response=await "
                      "fetch('http://127.0.0.1:3000/health');if(response.ok){ok=true;break}}catch{}"
                      "await new Promise(resolve=>setTimeout(resolve,500))}if(!ok)process.exit(1)")
            self.run('exec', name + '-app', 'node', '--input-type=module', '-e', health)
            self.run('run', '-d', '--name', name + '-browser', *common, '--user', 'pwuser',
                     '--mount', bind_mount(oracle_root, '/oracle'), '--entrypoint', 'node',
                     '-e', 'HOME=/tmp', '-e', 'TRACEFIX_PLAYWRIGHT_MODULE=/opt/mcp/node_modules/playwright',
                     browser_image, '-e', 'setInterval(()=>{},10000)')
            before = json.loads(self.run('container', 'inspect', name + '-app', name + '-browser').stdout)
            self.verify_containers(before, name, network, app_image, browser_image, source, oracle_root)
            completed = self.run('exec', name + '-browser', 'node', '/oracle/oracle.mjs',
                                 '/oracle/request.json', check=False)
            lines = completed.stdout.decode('utf-8', errors='replace').strip().splitlines()
            result = json.loads(lines[-1]) if lines else None
            if (not isinstance(result, dict) or type(result.get('passed')) is not bool
                    or result.get('binding') != binding or result.get('case_id') != binding['case_id']
                    or completed.returncode not in {0, 1}
                    or (completed.returncode == 0) != result['passed']):
                raise RuntimeError('private GUI Oracle did not return a bound deterministic score: '
                                   + completed.stderr.decode('utf-8', errors='replace')[-1000:])
            after = json.loads(self.run('container', 'inspect', name + '-app', name + '-browser').stdout)
            self.verify_containers(after, name, network, app_image, browser_image, source, oracle_root)
            if (tree_hash(source) != prepared['candidate_source_hash']
                    or digest(prepared['patch'].read_bytes()) != binding['patch_hash']):
                raise ValueError('candidate changed during private scoring')
            return {**result, 'source_hash': prepared['source_hash'],
                    'candidate_source_hash': prepared['candidate_source_hash'],
                    'patch_hash': prepared['patch_hash'], 'oracle_script_hash': digest(oracle_script.read_bytes()),
                    'oracle_environment_digest': digest([app_image, browser_image]),
                    'image_ids': {'app': app_image, 'browser': browser_image},
                    'container_evidence': {'before': before, 'after': after},
                    'network': network, 'network_evidence': network_info,
                    'application_contains_oracle': False}
        finally:
            self.run('rm', '-f', '-v', name + '-browser', name + '-app', check=False)
            self.run('volume', 'rm', volume, check=False)
            self.run('network', 'rm', network, check=False)

    @staticmethod
    def verify_containers(records, name, network, app_image, browser_image, source, oracle_root):
        if not isinstance(records, list) or len(records) != 2:
            raise ValueError('Oracle container inventory mismatch')
        for record, suffix, image, mount in zip(records, ('-app', '-browser'),
                                               (app_image, browser_image), (source, oracle_root)):
            host = record.get('HostConfig', {})
            mounts = record.get('Mounts', [])
            bind = [item for item in mounts if item.get('Type') == 'bind']
            expected_destination = '/app' if suffix == '-app' else '/oracle'
            source_name = mount_source_identity(mount)
            actual_source = mount_source_identity(bind[0].get('Source', '')) if len(bind) == 1 else ''
            if (record.get('Name') != '/' + name + suffix or record.get('Image') != image
                    or record.get('State', {}).get('Running') is not True
                    or host.get('Privileged') is not False or host.get('ReadonlyRootfs') is not True
                    or host.get('NetworkMode') != network or host.get('PidMode') == 'host'
                    or len(bind) != 1 or bind[0].get('RW') is not False
                    or bind[0].get('Destination') != expected_destination
                    or actual_source != source_name
                    or network not in record.get('NetworkSettings', {}).get('Networks', {})):
                raise ValueError('Oracle container boundary mismatch')


def score_request(request_path, *, executor=None):
    request_path = plain_path(request_path)
    request = json.loads(request_path.read_text(encoding='utf-8-sig'))
    private_root = plain_path(request.get('private_root') or request_path.parent)
    if not request_path.is_relative_to(private_root):
        raise ValueError('scoring request must be evaluator private')
    prepared = export_candidate(request, private_root)
    result = (executor or DockerOracle(timeout=request.get('timeout_seconds', 180))).score(request, prepared)
    prepared['report'].parent.mkdir(parents=True, exist_ok=True)
    with prepared['report'].open('x', encoding='utf-8') as output:
        output.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + '\n')
    return result


def prepare_private_oracle(private_root):
    private_root = plain_path(private_root)
    private_root.mkdir(parents=True, exist_ok=True)
    destinations = [private_root / 'docker_oracle.py', private_root / 'private_oracle/oracle.mjs']
    sources = [Path(__file__).resolve(), Path(__file__).parent / 'private_oracle/oracle.mjs']
    for source, destination in zip(sources, destinations):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise ValueError('private Oracle asset already exists with different content')
        else:
            shutil.copyfile(source, destination)
    return {'script': str(destinations[0]), 'oracle_script': str(destinations[1]),
            'assets': [{'path': path.relative_to(private_root).as_posix(), 'sha256': digest(path.read_bytes())}
                       for path in destinations]}


def validate_suite(*, source_repository, source_revision, private_root, cases,
                   app_image, browser_image, repeats=2, seed=17):
    if type(repeats) is not int or repeats < 2:
        raise ValueError('Oracle validation requires at least two repetitions')
    private_root = plain_path(private_root)
    assets = prepare_private_oracle(private_root)
    suite_root = Path(tempfile.mkdtemp(prefix='validation-', dir=private_root))
    baseline = suite_root / 'baseline'
    baseline.mkdir()
    archive = command(['git', '-C', source_repository, 'archive', '--format=tar', source_revision])
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:') as stream:
        if any(member.issym() or member.islnk() for member in stream.getmembers()):
            raise ValueError('linked validation baseline')
        stream.extractall(baseline, filter='data')
    records = []
    for case in cases:
        case_id = case.get('case_id') or case['id']
        if re.fullmatch(r'[A-Za-z0-9_-]{1,80}', case_id) is None:
            raise ValueError('invalid validation case identity')
        mutations = case.get('mutations', [])
        original = {}
        for mutation in mutations:
            path = baseline / safe_source_path(mutation['file'])
            original.setdefault(mutation['file'], path.read_bytes())
            text = path.read_text(encoding='utf-8')
            if text.count(mutation['before']) != 1:
                raise ValueError('validation mutation baseline mismatch')
            path.write_bytes(text.replace(mutation['before'], mutation['after'], 1).encode('utf-8'))
        source_hash = tree_hash(baseline)
        golden = b''
        for relative, content in original.items():
            current = (baseline / relative).read_bytes()
            golden += b''.join(difflib.diff_bytes(difflib.unified_diff,
                current.splitlines(keepends=True), content.splitlines(keepends=True),
                fromfile=('a/' + relative).encode(), tofile=('b/' + relative).encode()))
            (baseline / relative).write_bytes(content)
        variants = [('clean', b'', True)] if not mutations else [('defective', b'', False), ('correct', golden, True)]
        for variant, patch, expected in variants:
            for repetition in range(1, repeats + 1):
                directory = suite_root / (case_id + '-' + variant + '-' + str(repetition))
                directory.mkdir()
                patch_path = directory / 'candidate.patch'
                patch_path.write_bytes(patch)
                binding = {'run_id': directory.name, 'group_id': 'oracle-validation', 'case_id': case_id,
                    'source_revision': source_revision, 'source_hash': source_hash,
                    'environment_digest': digest([app_image, browser_image]), 'seed': seed,
                    'spec_hash': digest({'case_id': case_id, 'oracle_cases': case['oracle_cases']}),
                    'protocol_hash': digest('private-oracle-validation-v1'), 'patch_hash': digest(patch)}
                request = {'private_root': str(private_root), 'source_repository': str(source_repository),
                    'source_revision': source_revision, 'case_id': case_id, 'mutations': mutations,
                    'oracle_cases': case['oracle_cases'], 'known_bug': case.get('known_bug', bool(mutations)),
                    'candidate_patch': str(patch_path), 'oracle_script': assets['oracle_script'],
                    'report_path': str(directory / 'report.json'), 'binding': binding,
                    'app_image': app_image, 'browser_image': browser_image}
                request_path = directory / 'request.json'
                request_path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
                try:
                    result = score_request(request_path)
                    records.append({'case_id': case_id, 'variant': variant, 'repetition': repetition,
                        'expected': expected, 'actual': result['passed'], 'matched': result['passed'] == expected,
                        'report_path': request['report_path'], 'binding': binding,
                        'fix_passed': result['fix']['passed'], 'regression_passed': result['regression']['passed']})
                except Exception as error:
                    records.append({'case_id': case_id, 'variant': variant, 'repetition': repetition,
                        'expected': expected, 'actual': None, 'matched': False,
                        'error': f'{type(error).__name__}: {error}', 'binding': binding})
    result = {'passed': bool(records) and all(record['matched'] for record in records),
              'source_revision': source_revision, 'repeats': repeats, 'records': records,
              'private_root': str(private_root), 'suite_root': str(suite_root), 'model_calls': 0}
    (suite_root / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run evaluator-private Docker GUI Oracle')
    parser.add_argument('--request', required=True, type=Path)
    options = parser.parse_args(argv)
    try:
        result = score_request(options.request)
    except Exception as error:
        print(json.dumps({'passed': False, 'status': 'infrastructure_error',
                          'error': f'{type(error).__name__}: {error}'}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
