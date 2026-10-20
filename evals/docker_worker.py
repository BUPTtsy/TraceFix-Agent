"""Agent execution entry point inside the hardened Docker container."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path


ROOT = Path('/agent-run')


def container_preflight():
    if not Path('/.dockerenv').is_file() or os.getuid() == 0:
        raise RuntimeError('Agent worker requires a non-root Docker container')
    root_readonly = False
    try:
        Path('/tracefix-root-write-probe').write_text('probe')
    except OSError:
        root_readonly = True
    if not root_readonly:
        raise RuntimeError('Agent root filesystem must be read-only')
    if any(Path(path).exists() for path in ('/var/run/docker.sock', '/run/docker.sock')):
        raise RuntimeError('Docker socket must not be exposed to Agent')
    status = Path('/proc/self/status').read_text()
    values = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    if int(values.get('CapEff', '1').strip(), 16) or values.get('NoNewPrivs', '').strip() != '1':
        raise RuntimeError('Agent capabilities or no-new-privileges are invalid')
    return {'inside_docker': True, 'uid': os.getuid(), 'root_readonly': root_readonly,
            'docker_socket_visible': False, 'capabilities': values['CapEff'].strip(),
            'no_new_privileges': True, 'pid': os.getpid()}


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def worker_owned_source(root=ROOT):
    """在 worker 身份下复制可信源码，保留独立 Git 元数据供正式导出验证。"""
    root = Path(root).absolute()
    source, destination = root / 'source', root / 'data/worker-source'
    def linked(path):
        return (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400))
    for path in (root, *root.parents, source, destination.parent, destination):
        if linked(path):
            raise ValueError('worker source path must not contain links')
    if not source.is_dir() or not (source / '.git').is_dir():
        raise ValueError('worker source must contain an independent Git repository')
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('worker source destination must be fresh')
    for path in source.rglob('*'):
        if (linked(path)
                or not (stat.S_ISREG(path.lstat().st_mode) or stat.S_ISDIR(path.lstat().st_mode))):
            raise ValueError('worker source contains a link or unsupported entry')
    shutil.copytree(source, destination)
    return destination


def execute(payload):
    from evals.config import EvaluationConfig
    from evals.runner import SessionAdapter

    config = EvaluationConfig.load(ROOT / 'inputs/config.json')
    source = worker_owned_source()

    class ContainerSessionAdapter(SessionAdapter):
        @staticmethod
        def _gui_evidence(engine, state):
            atomic_json(ROOT / 'candidate-binding.json', {
                'workspace': str(engine.workspace.root), 'base': state.patch_base_commit or 'HEAD'})
            return SessionAdapter._gui_evidence(engine, state)

    result = ContainerSessionAdapter()(source=source, root=ROOT,
        env=dict(os.environ), config=dataclasses.replace(config, source_root=source, held_out=False),
        binding=payload['binding'])
    verify = result.pop('verify_candidate', None)
    empty_hash = hashlib.sha256(b'').hexdigest()
    if (not result.get('candidate_patch') and result.get('outcome') in {'NO_BUG_FOUND', 'INCONCLUSIVE'}
            and callable(verify) and verify() == empty_hash):
        (ROOT / 'candidate.patch').write_bytes(b'')
        result['candidate_patch'] = '/agent-run/candidate.patch'
    result['container_execution'] = container_preflight()
    atomic_json(ROOT / 'result.json', result)


def candidate_hash():
    import subprocess

    value = json.loads((ROOT / 'candidate-binding.json').read_text())
    workspace = Path(value['workspace']).resolve()
    if not workspace.is_relative_to(ROOT / 'data') or not workspace.is_dir():
        raise ValueError('candidate workspace is outside Agent data')
    completed = subprocess.run(['git', '-C', str(workspace), 'diff', value['base'], '--'],
                               capture_output=True, check=True)
    return hashlib.sha256(completed.stdout).hexdigest()


def serve():
    atomic_json(ROOT / 'ready.json', container_preflight())
    while not (ROOT / 'stop').exists():
        request = ROOT / 'task.json'
        if request.is_file() and not (ROOT / 'result.json').exists():
            try:
                execute(json.loads(request.read_text(encoding='utf-8')))
            except Exception as error:
                atomic_json(ROOT / 'result.json', {'status': 'infrastructure_error',
                    'outcome': 'INFRA_FAILURE', 'infra_failure': True,
                    'error': f'{type(error).__name__}: {error}'})
        time.sleep(0.05)


def main():
    if sys.argv[1:] == ['candidate-hash']:
        container_preflight()
        print(candidate_hash())
    elif sys.argv[1:] == ['serve']:
        serve()
    else:
        raise ValueError('unsupported Docker worker command')


if __name__ == '__main__':
    main()
