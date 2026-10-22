"""SDK worker 项目工具链挂载回归，mock Docker 控制面。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from tracefix.execution import agent_runner
from tracefix.execution.agent_runner import AgentExecutionRunner, _mount, worker_capabilities
from tracefix.execution.platforms import container_user
from tracefix.runtime.contracts import digest

IMAGE = 'sha256:' + 'ab' * 32
WORKER = 'sha256:' + '0' * 64
WORKER_PATH = '/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin'
LABEL = 'tracefix.sdk.toolchain=' + IMAGE
BIN, LIB, DEPS = ('tf-sdk-tcbin-abababababab', 'tf-sdk-tclib-abababababab',
                  'tf-sdk-deps-abababababab')


def make_runner(tmp_path, entries=None):
    source = tmp_path / 'source'
    source.mkdir(exist_ok=True)
    workspace = SimpleNamespace(source=source, execution_dir=tmp_path / 'execution', config_dir=None,
                                input_snapshot=SimpleNamespace(entries=entries or {}))
    runner = AgentExecutionRunner(workspace, network='tf-egress', egress_proxy='http://proxy:3128')
    runner._worker_path = WORKER_PATH
    return runner


def make_request(toolchain=''):
    return SimpleNamespace(execution_id='exec-1', toolchain_image=toolchain,
        config=SimpleNamespace(worker_image=WORKER, sdk_version='0.2.168', cli_version='2.1.301',
                               egress_allowlist=['https://api.example.test']))


def mounts(command):
    return [command[index + 1] for index, item in enumerate(command) if item == '--mount']


def test_build_command_mounts_toolchain_and_path(tmp_path):
    command = make_runner(tmp_path).build_command(make_request(IMAGE))
    toolchain = [item for item in mounts(command) if item.startswith('type=volume')]
    assert toolchain == [
        'type=volume,src=' + BIN + ',dst=/opt/toolchain/bin,readonly',
        'type=volume,src=' + LIB + ',dst=/opt/toolchain/lib/node_modules,readonly',
        'type=volume,src=' + DEPS + ',dst=/workspace/node_modules,readonly']
    # 最小挂载面：不得再挂整个 /usr/local 到 /opt/toolchain
    assert not any(item.endswith('dst=/opt/toolchain') or 'dst=/opt/toolchain,' in item
                   for item in mounts(command))
    assert command[command.index('/workspace/dist:rw,nosuid,nodev,size=256m,mode=1777') - 1] == '--tmpfs'
    assert 'PATH=' + WORKER_PATH + ':/opt/toolchain/bin' in command
    assert command.index('--entrypoint') > command.index('PATH=' + WORKER_PATH + ':/opt/toolchain/bin')
    assert command[command.index('--entrypoint'):][:3] == ['--entrypoint', '/usr/local/bin/python', WORKER]


def test_build_command_without_toolchain_is_unchanged(tmp_path):
    runner = make_runner(tmp_path)
    workspace = runner.workspace
    expected = ['docker', 'run', '--name', 'tf-sdk-exec-1', '--network', 'tf-egress',
        '--label', 'tracefix.execution=exec-1',
        '--label', 'tracefix.workspace=' + digest(str(workspace.source.resolve())),
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--user', container_user(), '--pids-limit', '256', '--workdir', '/workspace',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
        '--mount', _mount(workspace.source, '/workspace'),
        '--mount', _mount(workspace.execution_dir / 'control', '/control', readonly=True),
        '--mount', _mount(workspace.execution_dir / 'state', '/state'),
        '--mount', _mount(workspace.execution_dir / 'sdk-config', '/sdk-config'),
        '--env', 'ANTHROPIC_API_KEY',
        '--env', 'TRACEFIX_SDK_WORKER=1', '--env', 'HOME=/sdk-config',
        '--env', 'CLAUDE_CONFIG_DIR=/sdk-config', '--env', 'PYTHONUNBUFFERED=1',
        '--env', 'HTTP_PROXY=http://proxy:3128', '--env', 'HTTPS_PROXY=http://proxy:3128',
        '--entrypoint', '/usr/local/bin/python', WORKER,
        '-m', 'tracefix.agents.claude_sdk_worker', '--request', '/control/request.json',
        '--result', '/state/result.json']
    assert runner.build_command(make_request('')) == expected
    del runner._worker_path
    assert runner.build_command(SimpleNamespace(execution_id='exec-1',
        config=make_request().config)) == expected


def test_existing_snapshot_paths_are_not_shadowed(tmp_path):
    runner = make_runner(tmp_path, {'node_modules': {'kind': 'directory'}})
    command = runner.build_command(make_request(IMAGE))
    assert not any('/workspace/node_modules' in item for item in mounts(command))
    assert '/workspace/dist:rw,nosuid,nodev,size=256m,mode=1777' in command
    assert any('dst=/opt/toolchain/bin' in item for item in mounts(command))
    assert any('dst=/opt/toolchain/lib/node_modules' in item for item in mounts(command))
    runner = make_runner(tmp_path, {'dist': {'kind': 'directory'}})
    command = runner.build_command(make_request(IMAGE))
    assert not any(item.startswith('/workspace/dist') for item in command)


def test_build_command_requires_verified_worker_path(tmp_path):
    runner = make_runner(tmp_path)
    del runner._worker_path
    with pytest.raises(PermissionError, match='PATH'):
        runner.build_command(make_request(IMAGE))


class FakeDocker:
    """模拟 docker：create 携带 volume-label 时隐式建卷并打标签。"""

    def __init__(self, volumes=None, fail_create=False, containers=()):
        self.volumes = dict(volumes or {})
        self.containers = set(containers)
        self.fail_create = fail_create
        self.calls = []

    async def __call__(self, *args, environment=None, timeout=30, check=True):
        self.calls.append(args)
        if args[:2] == ('volume', 'inspect'):
            labels = self.volumes.get(args[2])
            return '[]' if labels is None else json.dumps([{'Name': args[2], 'Labels': labels}])
        if args[:2] == ('volume', 'create'):
            raise AssertionError('工具链卷不得单独 volume create')
        if args[:2] == ('volume', 'rm'):
            self.volumes.pop(args[2], None)
            return ''
        if args[0] == 'create':
            name = args[args.index('--name') + 1]
            if name in self.containers:
                raise RuntimeError('Docker worker 控制失败，退出码 125')
            options = dict(item.split('=', 1) for item in args[args.index('--mount') + 1].split(','))
            label = options['volume-label'].split('=', 1)
            # 真实 docker：卷在 create 校验阶段即被隐式创建
            self.volumes.setdefault(options['src'], {label[0]: label[1]})
            if self.fail_create:
                raise RuntimeError('Docker worker 控制失败，退出码 1')
            self.containers.add(name)
            return 'id-' + name + '\n'
        if args[0] == 'rm':
            self.containers.discard(args[-1])
            return ''
        return ''


@pytest.mark.asyncio
async def test_toolchain_volumes_created_by_single_create(tmp_path):
    runner = make_runner(tmp_path)
    runner._docker = docker = FakeDocker(containers={'tf-tcfill-tcbin-abababababab'})
    await runner._ensure_toolchain_volumes(IMAGE)
    assert docker.volumes == {name: {'tracefix.sdk.toolchain': IMAGE} for name in (BIN, LIB, DEPS)}
    creates = [call for call in docker.calls if call[0] == 'create']
    assert creates == [
        ('create', '--network', 'none', '--name', 'tf-tcfill-tcbin-abababababab', '--mount',
         'type=volume,src=' + BIN + ',dst=/usr/local/bin,volume-label=' + LABEL, IMAGE),
        ('create', '--network', 'none', '--name', 'tf-tcfill-tclib-abababababab', '--mount',
         'type=volume,src=' + LIB + ',dst=/usr/local/lib/node_modules,volume-label=' + LABEL, IMAGE),
        ('create', '--network', 'none', '--name', 'tf-tcfill-deps-abababababab', '--mount',
         'type=volume,src=' + DEPS + ',dst=/app/node_modules,volume-label=' + LABEL, IMAGE)]
    # 遗留填充容器在 create 之前被强制删除；成功后填充容器删除
    first = docker.calls.index(creates[0])
    assert docker.calls[first - 1] == ('rm', '--force', 'tf-tcfill-tcbin-abababababab')
    assert docker.containers == set()
    assert sum(call[0] == 'rm' and call[1] != '--force' for call in docker.calls) == 3
    # 已存在且标签匹配时直接复用
    docker.calls.clear()
    await runner._ensure_toolchain_volumes(IMAGE)
    assert all(call[:2] == ('volume', 'inspect') for call in docker.calls)


@pytest.mark.asyncio
async def test_toolchain_volume_label_mismatch_is_rejected(tmp_path):
    runner = make_runner(tmp_path)
    runner._docker = docker = FakeDocker({BIN: {'tracefix.sdk.toolchain': 'sha256:' + 'cd' * 32}})
    with pytest.raises(PermissionError, match='标签'):
        await runner._ensure_toolchain_volumes(IMAGE)
    assert not any(call[0] in {'create', 'rm'} or call[:2] == ('volume', 'rm') for call in docker.calls)
    # 无标签的同名卷（例如旧流程半成品）同样拒绝
    runner._docker = FakeDocker({BIN: {}})
    with pytest.raises(PermissionError, match='标签'):
        await runner._ensure_toolchain_volumes(IMAGE)


@pytest.mark.asyncio
async def test_toolchain_populate_failure_removes_partial_volume(tmp_path):
    runner = make_runner(tmp_path)
    runner._docker = docker = FakeDocker(fail_create=True)
    with pytest.raises(RuntimeError):
        await runner._ensure_toolchain_volumes(IMAGE)
    assert docker.volumes == {}
    assert ('volume', 'rm', BIN) in docker.calls
    assert docker.calls[-2:] == [('rm', '--force', 'tf-tcfill-tcbin-abababababab'),
                                 ('volume', 'rm', BIN)]


@pytest.mark.asyncio
async def test_toolchain_image_must_be_frozen_id(tmp_path):
    runner = make_runner(tmp_path)
    runner._docker = docker = FakeDocker()
    with pytest.raises(PermissionError, match='image ID'):
        await runner._ensure_toolchain_volumes('bugboard:latest')
    assert docker.calls == []


def worker_record(capabilities=None, image=WORKER):
    labels = {'tracefix.sdk.version': '0.2.168', 'tracefix.cli.version': '2.1.301'}
    if capabilities is not None:
        labels['tracefix.sdk.capabilities'] = capabilities
    return [{'Id': image, 'Config': {'Env': ['PATH=' + WORKER_PATH, 'LANG=C.UTF-8'], 'Labels': labels}}]


async def run_preflight(tmp_path, toolchain_id, capabilities='toolchain'):
    runner = make_runner(tmp_path)
    del runner._worker_path
    request = make_request(IMAGE)
    policy = digest(json.dumps(sorted(request.config.egress_allowlist)).encode())

    async def docker(*args, **kwargs):
        if args[:2] == ('network', 'inspect'):
            return json.dumps([{'Internal': True, 'Labels': {'tracefix.egress.enforced': 'true',
                                                             'tracefix.egress.policy': policy}}])
        if args[:3] == ('image', 'inspect', WORKER):
            return json.dumps(worker_record(capabilities))
        if args[:3] == ('image', 'inspect', IMAGE):
            return json.dumps([{'Id': toolchain_id}])
        raise AssertionError(args)

    runner._docker = docker
    await runner._preflight(request)
    return runner


@pytest.mark.asyncio
async def test_preflight_checks_toolchain_image_identity(tmp_path):
    runner = await run_preflight(tmp_path, IMAGE)
    assert runner._worker_path == WORKER_PATH
    with pytest.raises(PermissionError, match='工具链镜像身份'):
        await run_preflight(tmp_path, 'sha256:' + 'cd' * 32)


@pytest.mark.asyncio
@pytest.mark.parametrize('capabilities', [None, '', 'other', 'toolchains,x'])
async def test_preflight_rejects_worker_without_toolchain_capability(tmp_path, capabilities):
    with pytest.raises(PermissionError, match='不支持项目工具链挂载'):
        await run_preflight(tmp_path, IMAGE, capabilities)


@pytest.mark.asyncio
async def test_preflight_accepts_capability_list_with_whitespace(tmp_path):
    runner = await run_preflight(tmp_path, IMAGE, ' other , toolchain ')
    assert runner._worker_path == WORKER_PATH


def fake_run_docker(monkeypatch, output=None, error=None):
    calls = []

    async def run(*args, environment=None, timeout=30, check=True):
        calls.append((args, environment, check))
        if error is not None:
            raise error
        return output

    monkeypatch.setattr(agent_runner, '_run_docker', run)
    return calls


@pytest.mark.asyncio
async def test_worker_capabilities_parses_label(monkeypatch):
    calls = fake_run_docker(monkeypatch, json.dumps(worker_record(' toolchain, ,future ')))
    assert await worker_capabilities(WORKER) == frozenset({'toolchain', 'future'})
    assert calls[0][0] == ('image', 'inspect', WORKER)
    fake_run_docker(monkeypatch, json.dumps(worker_record(None)))
    assert await worker_capabilities(WORKER) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize('output, error', [
    (json.dumps(worker_record('toolchain', image='sha256:' + '1' * 64)), None),
    (json.dumps(worker_record('toolchain') * 2), None),
    ('not json', None),
    ('{}', None),
    (json.dumps([{'Id': WORKER, 'Config': {'Labels': {'tracefix.sdk.capabilities': 7}}}]), None),
    (None, RuntimeError('Docker worker 控制失败，退出码 1')),
    (None, OSError('docker missing')),
])
async def test_worker_capabilities_errors_return_empty(monkeypatch, output, error):
    fake_run_docker(monkeypatch, output, error)
    assert await worker_capabilities(WORKER) == frozenset()


@pytest.mark.asyncio
async def test_worker_capabilities_uses_secret_free_environment(monkeypatch):
    seen = {}

    async def exec_(*args, env=None, **kwargs):
        seen['args'], seen['env'], seen['kwargs'] = args, env, kwargs
        raise OSError('stop')

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'secret-value')
    monkeypatch.setattr(agent_runner.asyncio, 'create_subprocess_exec', exec_)
    assert await worker_capabilities(WORKER) == frozenset()
    assert seen['args'] == ('docker', 'image', 'inspect', WORKER)
    assert 'ANTHROPIC_API_KEY' not in seen['env'] and 'shell' not in seen['kwargs']


def test_cleanup_mountpoints_removes_empty_and_preserves_nonempty(tmp_path):
    runner = make_runner(tmp_path)
    source = runner.workspace.source
    (source / 'node_modules').mkdir()
    (source / 'dist').mkdir()
    (source / 'dist' / 'bundle.js').write_text('x', encoding='utf-8')
    runner.cleanup_mountpoints()
    assert not (source / 'node_modules').exists()
    assert (source / 'dist' / 'bundle.js').is_file()
    # 幂等：重复调用不报错、不改变结果
    runner.cleanup_mountpoints()
    assert not (source / 'node_modules').exists()
    assert (source / 'dist' / 'bundle.js').is_file()


def test_cleanup_mountpoints_skips_snapshot_paths_and_files(tmp_path):
    runner = make_runner(tmp_path, {'node_modules': {'kind': 'directory'}})
    source = runner.workspace.source
    (source / 'node_modules').mkdir()
    (source / 'dist').write_text('', encoding='utf-8')
    runner.cleanup_mountpoints()
    assert (source / 'node_modules').is_dir()
    assert (source / 'dist').is_file()
    (source / 'dist').unlink()
    (source / 'dist').mkdir()
    runner.cleanup_mountpoints()
    assert not (source / 'dist').exists()


class _StopStart(Exception):
    pass


@pytest.mark.asyncio
async def test_start_raises_event_line_limit(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    runner.workspace.execution_dir = tmp_path / 'exec-1'
    (runner.workspace.execution_dir / 'control').mkdir(parents=True)
    runner.workspace.check_input = lambda: None
    seen = {}

    async def preflight(request):
        return None

    async def docker(*args, **kwargs):
        return ''

    async def manifest(request, stage):
        return {}

    async def exec_(*argv, **kwargs):
        seen.update(kwargs)
        raise _StopStart

    runner._preflight, runner._docker, runner._capture_linux_manifest = preflight, docker, manifest
    monkeypatch.setattr(runner, '_environment', lambda request=None: {})
    monkeypatch.setattr(agent_runner.asyncio, 'create_subprocess_exec', exec_)
    request = make_request('')
    request.execution_id = 'exec-1'
    request.model_dump = lambda mode='json': {}
    with pytest.raises(_StopStart):
        await runner.start(request, None)
    assert seen['limit'] == agent_runner._EVENT_LINE_LIMIT >= 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_event_line_limit_reads_oversized_event():
    reader = asyncio.StreamReader(limit=agent_runner._EVENT_LINE_LIMIT)
    line = json.dumps({'type': 'sdk.message', 'data': 'x' * (1 << 20)}).encode() + b'\n'
    reader.feed_data(line)
    reader.feed_eof()
    assert await reader.readline() == line
