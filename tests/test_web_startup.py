import importlib.util
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def load_tool(name):
    filename = Path(__file__).resolve().parents[1] / 'tools/bootstrap' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'tracefix_web_{name}_test', filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bootstrap_fixture(tmp_path, monkeypatch):
    module = load_tool('bootstrap')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.chdir(tmp_path)
    python = module.venv_python(tmp_path)
    python.parent.mkdir(parents=True)
    python.touch()
    (tmp_path / '.env').write_text('TRACEFIX_API_KEY=fixture\n', encoding='utf-8')
    prerequisites = []
    calls = []
    monkeypatch.setattr(module, 'prerequisites', lambda **kwargs: (prerequisites.append(kwargs) or {}, True))
    monkeypatch.setattr(module.shutil, 'which', lambda name: name)

    def run(argv, **kwargs):
        calls.append([str(value) for value in argv])
        return 'v22.14.0' if '--version' in argv else ''

    monkeypatch.setattr(module, 'run', run)
    return module, calls, prerequisites


def test_full_web_prepares_agent_and_starts_services(tmp_path, monkeypatch):
    module, calls, prerequisites = bootstrap_fixture(tmp_path, monkeypatch)
    assert module.main(['--web', '--skip-install', '--skip-build', '--plain']) == 0
    assert prerequisites == [{'require_docker': True}]
    assert ['docker', 'compose', '--env-file', '.env', 'up', '-d', '--wait', 'postgres'] in calls
    assert any(command[1:3] == ['image', 'inspect'] for command in calls)
    assert ['npm', 'run', 'build'] in calls
    assert calls[-1] == [str(module.venv_python(tmp_path)), 'tools/bootstrap/services.py']
    assert not any('frontend/apps/cli/dist/cli.mjs' in command for command in calls)


@pytest.mark.parametrize('agent_arguments', [
    [], ['--mode', 'test'], ['--mode', 'repair', '--spec', 'profiles/persistence.spec.json'],
    ['--run', '--goal', 'batch task'],
])
def test_cli_startup_uses_new_default_without_overriding_explicit_task(tmp_path, monkeypatch, agent_arguments):
    module, calls, _ = bootstrap_fixture(tmp_path, monkeypatch)
    assert module.main(['--skip-install', '--skip-build', *agent_arguments]) == 0
    assert calls[-1] == ['node', 'frontend/apps/cli/dist/cli.mjs', *agent_arguments]


def test_console_only_does_not_require_docker_or_key(tmp_path, monkeypatch):
    module, calls, prerequisites = bootstrap_fixture(tmp_path, monkeypatch)
    (tmp_path / '.env').unlink()
    (tmp_path / '.env.example').write_text('TRACEFIX_API_KEY=\n', encoding='utf-8')
    monkeypatch.delenv('TRACEFIX_API_KEY', raising=False)
    assert module.main(['--console-only', '--dev', '--skip-install', '--plain']) == 0
    assert prerequisites == [{'require_docker': False}]
    assert all(command[0] != 'docker' for command in calls)
    assert not any('bugboard/scripts/init_demo.py' in command for command in calls)
    assert calls[-1][-2:] == ['tools/bootstrap/services.py', '--dev']
    assert (tmp_path / '.env').read_text(encoding='utf-8') == 'TRACEFIX_API_KEY=\n'


@pytest.mark.parametrize('arguments', [
    ['--dev'], ['--web', '--smoke'], ['--web', '--preview'], ['--web', '--mode', 'repair'],
])
def test_incompatible_modes_fail_before_startup(arguments):
    module = load_tool('bootstrap')
    with pytest.raises(SystemExit) as error:
        module.main(arguments)
    assert error.value.code == 2


def test_windows_wheelhouse_contains_every_locked_requirement():
    root = Path(__file__).resolve().parents[1]
    wheels = [parse_wheel_filename(path.name) for path in (root / 'vendor/wheels-win_amd64').glob('*.whl')]
    supported = set(cpython_tags((3, 12), platforms=['win_amd64']))
    supported.update(compatible_tags((3, 12), interpreter='cp312', platforms=['win_amd64']))
    environment = {**default_environment(), 'sys_platform': 'win32', 'os_name': 'nt',
        'platform_system': 'Windows', 'platform_machine': 'AMD64', 'python_version': '3.12',
        'python_full_version': '3.12.0', 'platform_python_implementation': 'CPython',
        'implementation_name': 'cpython', 'implementation_version': '3.12.0'}
    missing = []
    for line in (root / 'requirements.lock').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        if not any(name == canonicalize_name(requirement.name) and version in requirement.specifier
                   and tags & supported for name, version, build, tags in wheels):
            missing.append(str(requirement))
    assert not missing, f'Windows CPython 3.12 x64 离线 wheel 缺失：{missing}'


def test_busy_port_is_rejected_without_starting_services():
    module = load_tool('services')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        with pytest.raises(RuntimeError, match='已被占用'):
            module.ensure_ports_available([listener.getsockname()[1]])


def test_child_exit_is_reported_before_http_readiness():
    module = load_tool('services')
    child = SimpleNamespace(poll=lambda: 1, returncode=1)
    with pytest.raises(RuntimeError, match='HTTP 服务 启动失败'):
        module.wait_ready([('HTTP 服务', child)], ['http://127.0.0.1:1/health'])


@pytest.mark.parametrize('failure', [RuntimeError('fixture failure'), KeyboardInterrupt()])
def test_startup_failure_and_interrupt_clean_up_created_children(monkeypatch, failure):
    module = load_tool('services')
    monkeypatch.setattr(module, 'load_dotenv', lambda *args, **kwargs: None)
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.delenv('TRACEFIX_CONTROL_HOST', raising=False)
    monkeypatch.setattr(module.shutil, 'which', lambda name: name)
    monkeypatch.setattr(module, 'ensure_ports_available', lambda ports: None)
    children = []
    stopped = []

    def spawn(*args, **kwargs):
        child = SimpleNamespace(pid=len(children) + 100, poll=lambda: None)
        children.append(child)
        return child

    def ready(*args, **kwargs):
        raise failure

    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    monkeypatch.setattr(module, 'wait_ready', ready)
    monkeypatch.setattr(module, 'stop_process', stopped.append)
    previous = signal.getsignal(signal.SIGINT)
    if isinstance(failure, KeyboardInterrupt):
        assert module.serve(dev=True) == 130
    else:
        with pytest.raises(RuntimeError, match='fixture failure'):
            module.serve(dev=True)
    assert len(children) == 2
    assert stopped == list(reversed(children))
    assert signal.getsignal(signal.SIGINT) == previous


def test_windows_stop_targets_only_owned_process_tree(monkeypatch):
    module = load_tool('services')
    monkeypatch.setattr(module, 'os', SimpleNamespace(name='nt'))
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **kwargs: calls.append(argv))
    child = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda **kwargs: 0)
    module.stop_process(child)
    assert calls == [['taskkill.exe', '/PID', '12345', '/T', '/F']]
