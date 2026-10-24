"""Real Docker Session adapter with evaluator-owned restricted lifecycle control."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import yaml

from evals.docker_broker import DockerBroker, mount_argument, plain_path
from evals.isolation import AgentExposure, DockerAgentBoundary, IsolationError


IMAGE_LABEL = 'org.tracefix.eval.build-manifest'
PUBLIC_MODULES = ('__init__.py', 'config.py', 'runner.py', 'docker_worker.py', 'docker_proxy.py')
FORWARDED_ENV = frozenset({'TRACEFIX_API_KEY', 'TRACEFIX_BASE_URL', 'TRACEFIX_DATABASE_URL',
    'TRACEFIX_AGENT_MODE', 'TRACEFIX_REPAIR_MODE', 'TRACEFIX_CROSS_RUN_MEMORY',
    'TRACEFIX_RUN_ID', 'TRACEFIX_EVAL_GROUP', 'TRACEFIX_EVAL_SEED', 'TRACEFIX_HELD_OUT',
    'TRACEFIX_MEMORY_FROZEN', 'TRACEFIX_TEXT_MODEL', 'TRACEFIX_VISION_MODEL', 'TRACEFIX_THINKING',
    'TRACEFIX_EMBEDDING_URL', 'TRACEFIX_EMBEDDING_REVISION'})


def docker(*arguments, executable='docker', timeout=300, check=True):
    completed = subprocess.run([executable, *arguments], capture_output=True,
                               timeout=timeout, check=False)
    if check and completed.returncode:
        raise RuntimeError('Docker lifecycle command failed: ' + completed.stderr.decode(errors='replace')[-1000:])
    return completed


def build_image(repository, *, tag='tracefix-eval-agent:local', docker_executable='docker'):
    repository = plain_path(repository)
    with tempfile.TemporaryDirectory(prefix='tracefix-agent-image-') as folder:
        context = Path(folder)
        runtime = repository / 'backend/packages/agent/src/tracefix'
        shutil.copytree(runtime, context / 'tracefix', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (context / 'evals').mkdir()
        for name in PUBLIC_MODULES:
            shutil.copyfile(repository / 'evals' / name, context / 'evals' / name)
        shutil.copyfile(repository / 'requirements.lock', context / 'requirements.lock')
        shutil.copyfile(repository / 'evals/docker/Dockerfile', context / 'Dockerfile')
        entries = {path.relative_to(context).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(context.rglob('*')) if path.is_file()}
        manifest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        docker('build', '--build-arg', 'BUILD_MANIFEST=' + manifest, '-t', tag, str(context),
               executable=docker_executable, timeout=1800)
        record = json.loads(docker('image', 'inspect', tag, executable=docker_executable).stdout)[0]
        return {'image_id': record['Id'], 'tag': tag, 'build_manifest_hash': manifest,
                'included_files': entries, 'oracle_files_in_context': False}


def _container_endpoint(value):
    if not value:
        return value
    parts = urlsplit(value)
    if parts.hostname in {'localhost', '127.0.0.1', '::1'}:
        authority = parts.netloc.rsplit('@', 1)
        port = ':' + str(parts.port) if parts.port else ''
        netloc = (authority[0] + '@' if len(authority) == 2 else '') + 'host.docker.internal' + port
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return value


def public_worker_config(config, replacements):
    """运行源码已由评测器注入；Agent 配置不得包含缺陷配方或样本标签。"""
    return dataclasses.replace(config, **replacements, case_id='public-task', family='public',
        source_revision='0' * 40, mutations=(), mutation_file=None, mutation_before=None,
        mutation_after=None, category=None, known_bug=None, track='compare', command=(), held_out=False)


class DockerSessionAdapter:
    def __init__(self, image, *, docker_executable='docker'):
        self.image = image
        self.docker_executable = docker_executable
        self.worker_id = None
        self.worker_name = None
        self.network = None
        self.broker = None
        self.root = None
        self.exposure = None
        self.proof = None

    def _docker(self, *arguments, **kwargs):
        return docker(*arguments, executable=self.docker_executable, **kwargs)

    def prepare(self, root, config, env=None, source=None):
        self.close()
        self.root = plain_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if source is not None and plain_path(source) != self.root / 'source':
            raise IsolationError('source must be the isolated group source directory')
        for name in ('data', 'cache', 'browser', 'session', 'tmp', 'dependencies', 'broker'):
            (self.root / name).mkdir(exist_ok=True)
            (self.root / name).chmod(0o777)
        inputs = self.root / 'inputs'
        inputs.mkdir(exist_ok=False)
        public_paths = {'profile': config.profile, 'spec': config.spec, 'public_protocol': config.public_protocol,
                        'initial_experience': config.initial_experience}
        # 容器内路径必须保持 POSIX 形式；Windows 宿主上 Path 会序列化成反斜杠相对路径。
        replacements = {'source_root': PurePosixPath('/agent-run/source'),
                        'skills_root': PurePosixPath('/agent-run/inputs/skills')}
        for name, path in public_paths.items():
            path = plain_path(path)
            destination = inputs / (name + path.suffix)
            shutil.copyfile(path, destination)
            replacements[name] = PurePosixPath('/agent-run/inputs') / destination.name
        skill_source = plain_path(config.skills_root)
        for item in skill_source.rglob('*'):
            plain_path(item)
        shutil.copytree(skill_source, inputs / 'skills')
        staged = public_worker_config(config, replacements)
        (inputs / 'config.json').write_text(json.dumps(staged.payload(), ensure_ascii=False), encoding='utf-8')
        profile = yaml.safe_load(config.profile.read_text(encoding='utf-8'))
        image_names = [self.image, profile['image'], profile['browser_image']]
        images = json.loads(self._docker('image', 'inspect', *image_names).stdout)
        if len(images) != 3 or any(not re.fullmatch(r'sha256:[a-f0-9]{64}', image.get('Id', '')) for image in images):
            raise IsolationError('Agent/app/browser immutable image inventory unavailable')
        manifest = (images[0].get('Config', {}).get('Labels') or {}).get(IMAGE_LABEL, '')
        if not re.fullmatch(r'[a-f0-9]{64}', manifest):
            raise IsolationError('Agent image must be built by the clean evaluation image builder')
        whitelist = {'app': images[1]['Id'], 'browser': images[2]['Id'],
                     profile['image']: images[1]['Id'], profile['browser_image']: images[2]['Id']}
        forwarded = {key: str(value) for key, value in dict(os.environ if env is None else env).items()
                     if key in FORWARDED_ENV}
        for key in ('TRACEFIX_RUN_ID', 'TRACEFIX_EVAL_GROUP', 'TRACEFIX_EVAL_SEED'):
            forwarded.pop(key, None)
        nonce = uuid.uuid4().hex
        resource_prefix = 'tf-eval-' + nonce
        self.broker = DockerBroker(self.root, whitelist, docker_executable=self.docker_executable,
                                   timeout=config.timeout_seconds + 60, resource_prefix=resource_prefix)
        self.broker.start()
        # Run ID 可跨 seed 重复；Docker 名字必须由评测器的组级随机域隔离。
        forwarded['TRACEFIX_DOCKER_RESOURCE_PREFIX'] = resource_prefix
        self.worker_name = 'tracefix-eval-agent-' + nonce
        self.network = self.worker_name + '-net'
        self._docker('network', 'create', '--label', 'tracefix.eval=' + nonce, self.network)
        for key in ('TRACEFIX_DATABASE_URL', 'TRACEFIX_BASE_URL', 'TRACEFIX_EMBEDDING_URL'):
            if key in forwarded:
                forwarded[key] = _container_endpoint(forwarded[key])
        forwarded.update({'TRACEFIX_DATA': '/agent-run/data', 'TRACEFIX_MEMORY_DB': '/agent-run/data/memory.sqlite3',
            'TRACEFIX_CACHE_ROOT': '/agent-run/cache', 'TRACEFIX_BROWSER_PROFILE': '/agent-run/browser',
            'TRACEFIX_SESSION_ROOT': '/agent-run/session', 'TMPDIR': '/agent-run/tmp', 'HOME': '/tmp',
            'PYTHONDONTWRITEBYTECODE': '1', 'TRACEFIX_BROKER_TIMEOUT': str(config.timeout_seconds + 60)})
        arguments = ['run', '-d', '--name', self.worker_name, '--network', self.network,
            '--label', 'tracefix.eval=' + nonce, '--user', '1000:1000', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=512', '--memory=2g', '--cpus=2',
            '--tmpfs', '/tmp:rw,nosuid,size=256m,mode=1777',
            '--mount', mount_argument(self.root, '/agent-run', False),
            '--mount', mount_argument(inputs, '/agent-run/inputs', True)]
        for key, value in forwarded.items():
            arguments.extend(['-e', key + '=' + value])
        arguments.extend([images[0]['Id'], 'python', '-m', 'evals.docker_worker', 'serve'])
        try:
            self.worker_id = self._docker(*arguments).stdout.decode().strip()
            deadline = time.monotonic() + 30
            while not (self.root / 'ready.json').is_file():
                if time.monotonic() > deadline:
                    logs = self._docker('logs', self.worker_id, check=False).stdout.decode(errors='replace')[-1000:]
                    raise RuntimeError('Agent worker preflight timeout: ' + logs)
                time.sleep(0.05)
            preflight = json.loads((self.root / 'ready.json').read_text())
            networks = json.loads(self._docker('network', 'inspect', self.network).stdout)
            network_evidence = {'kind': 'per_group_bridge', 'network_id': networks[0]['Id'],
                'inspect_hash': hashlib.sha256(json.dumps(networks, sort_keys=True).encode()).hexdigest(),
                'api_target': self._target(forwarded.get('TRACEFIX_BASE_URL', 'https://api.deepseek.com')),
                'database_target': self._target(forwarded.get('TRACEFIX_DATABASE_URL', '')),
                'egress_policy': 'bridge egress; evaluator has no listener; no egress allowlist claimed'}
            self.exposure = AgentExposure(roots={
                'workspace': (self.root / 'source', self.root / 'data'),
                'tools': (inputs, self.root / 'broker'), 'memory': (self.root / 'data',),
                'skills': (inputs / 'skills',), 'retrieval': (self.root / 'data', self.root / 'cache'),
                'cache': (self.root / 'cache', self.root / 'tmp', self.root / 'dependencies'),
                'reports': (self.root,)}, container_ids=(self.worker_id,),
                expected_image_ids={self.worker_id: images[0]['Id']},
                network_boundary_evidence=json.dumps(network_evidence, sort_keys=True),
                cache_snapshot_evidence='fresh per-group cache/tmp/dependency bind directories',
                retrieval_snapshot_evidence='fresh per-group data; initial experience hash=' +
                    hashlib.sha256(config.initial_experience.read_bytes()).hexdigest())
            self.proof = {'worker': preflight, 'container_id': self.worker_id, 'image_id': images[0]['Id'],
                          'build_manifest_hash': manifest, 'network': network_evidence}
            return self.proof
        except Exception:
            self.close()
            raise

    @staticmethod
    def _target(value):
        parts = urlsplit(value)
        return {'scheme': parts.scheme, 'host': parts.hostname, 'port': parts.port}

    def __call__(self, *, source, root, env, config, binding, **_kwargs):
        self.prepare(root, config, env=env, source=source)
        request = self.root / 'task.tmp'
        # 运行器仅需 group_id 生成内部 repo 名；完整最终绑定留在评测侧。
        request.write_text(json.dumps({'binding': {'group_id': self.worker_name}}), encoding='utf-8')
        request.replace(self.root / 'task.json')
        deadline = time.monotonic() + config.timeout_seconds + 15
        while not (self.root / 'result.json').is_file():
            if time.monotonic() >= deadline:
                self._docker('kill', self.worker_id, check=False)
                return {'status': 'timeout', 'outcome': 'INFRA_FAILURE', 'infra_failure': True,
                        'error': 'isolated Agent execution timeout'}
            time.sleep(0.1)
        result = json.loads((self.root / 'result.json').read_text(encoding='utf-8'))
        candidate = result.get('candidate_patch')
        if candidate:
            if candidate != '/agent-run/candidate.patch':
                raise IsolationError('worker candidate path mismatch')
            result['candidate_patch'] = str(self.root / 'candidate.patch')
            result['verify_candidate'] = self.verify_candidate
        result['isolation_preflight'] = self.proof
        return result

    def verify_candidate(self):
        value = self._docker('exec', self.worker_id, 'python', '-m', 'evals.docker_worker',
                             'candidate-hash').stdout.decode().strip()
        if not re.fullmatch(r'[a-f0-9]{64}', value):
            raise IsolationError('invalid isolated candidate hash')
        return value

    def boundary(self):
        if not self.exposure or not self.worker_id:
            raise IsolationError('isolated Agent has not been launched')
        return DockerAgentBoundary(self.exposure, docker_executable=self.docker_executable)

    def close(self):
        if self.broker:
            self.broker.close()
            self.broker = None
        if self.worker_id:
            self._docker('rm', '-f', self.worker_id, check=False)
            self.worker_id = None
        if self.network:
            self._docker('network', 'rm', self.network, check=False)
            self.network = None
        self.exposure = None


def factory(config, output):
    image = os.getenv('TRACEFIX_EVAL_AGENT_IMAGE')
    if not image:
        raise ValueError('build the clean Agent image and set TRACEFIX_EVAL_AGENT_IMAGE to its immutable ID')
    return DockerSessionAdapter(image)
