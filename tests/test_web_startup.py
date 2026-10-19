import importlib.util
import base64
import os
import signal
import socket
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def load_tool(name):
    filename = Path(__file__).resolve().parents[1] / 'scripts/bootstrap' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'tracefix_web_{name}_test', filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tracked_powershell_scripts(root):
    result = subprocess.run(
        ['git', 'ls-files', '-z', '--', '*.ps1', '*.psm1', '*.psd1'],
        cwd=root,
        check=True,
        capture_output=True,
    )
    scripts = []
    for raw_path in result.stdout.split(b'\0'):
        if not raw_path:
            continue
        relative = raw_path.decode('utf-8').replace('\\', '/')
        parts = relative.split('/')
        if 'artifacts' in parts or '.tmp' in parts:
            continue
        if relative == 'bugboard/target' or relative.startswith('bugboard/target/'):
            continue
        scripts.append(root / Path(*relative.split('/')))
    return scripts


def test_tracked_powershell_scripts_use_utf8_bom(tmp_path):
    root = Path(__file__).resolve().parents[1]
    scripts = tracked_powershell_scripts(root)
    assert scripts, 'No tracked PowerShell scripts found.'
    missing_bom = [str(path.relative_to(root)) for path in scripts
                   if not path.read_bytes().startswith(b'\xef\xbb\xbf')]
    assert not missing_bom, f'PowerShell scripts must use UTF-8 BOM: {missing_bom}'


@pytest.mark.parametrize('shell_name', ['powershell.exe', 'pwsh.exe'])
def test_tracked_powershell_scripts_parse_in_windows_shell(shell_name):
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f'{shell_name} is unavailable')
    root = Path(__file__).resolve().parents[1]
    scripts = tracked_powershell_scripts(root)
    assert scripts, 'No tracked PowerShell scripts found.'
    command = (
        "$ErrorActionPreference='Stop'; "
        "$parseTokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$env:TRACEFIX_TEST_SCRIPT,[ref]$parseTokens,[ref]$parseErrors) | Out-Null; "
        "if($parseErrors.Count){$parseErrors | Out-String | Write-Output; exit 3}"
    )
    for script in scripts:
        environment = {**os.environ, 'TRACEFIX_TEST_SCRIPT': str(script)}
        result = subprocess.run(
            [shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
            env=environment,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )
        assert result.returncode == 0, f'{script}: {result.stdout}{result.stderr}'
        assert 'ParserError' not in result.stdout + result.stderr


@pytest.mark.parametrize('shell_name', ['powershell.exe', 'pwsh.exe'])
def test_interface_launcher_parses_and_reports_missing_environment_in_windows_shell(tmp_path, shell_name):
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f'{shell_name} is unavailable')
    original = Path(__file__).resolve().parents[1] / 'scripts/bootstrap/start-interface.ps1'
    original.read_bytes().decode('utf-8-sig')
    destination = tmp_path / 'scripts/bootstrap/start-interface.ps1'
    destination.parent.mkdir(parents=True)
    shutil.copyfile(original, destination)
    command = (
        "$ErrorActionPreference='Stop'; "
        "$launcher=$env:TRACEFIX_TEST_LAUNCHER; "
        "$parseTokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$launcher,[ref]$parseTokens,[ref]$parseErrors) | Out-Null; "
        "if($parseErrors.Count){Write-Output 'TRACEFIX_PARSER_ERROR'; exit 3}; "
        "try { & $launcher; exit 4 } catch { "
        "if($_.Exception.Message -match 'scripts/bootstrap/start.ps1 --web'){"
        "Write-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_.Exception.Message))); "
        "Write-Output 'TRACEFIX_MISSING_ENVIRONMENT'; exit 0}; "
        "Write-Output $_.Exception.GetType().FullName; exit 5 }"
    )
    environment = {**os.environ, 'TRACEFIX_TEST_LAUNCHER': str(destination)}
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command],
                            env=environment, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TRACEFIX_MISSING_ENVIRONMENT' in result.stdout
    encoded_message = result.stdout.splitlines()[0]
    message = base64.b64decode(encoded_message).decode('utf-8')
    assert message == '请先执行 scripts/bootstrap/start.ps1 --web 完成首次环境安装和镜像构建。'
    assert 'TRACEFIX_PARSER_ERROR' not in result.stdout
    assert 'ParserError' not in result.stdout + result.stderr


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
    prepare = next(index for index, command in enumerate(calls)
                   if 'bugboard/scripts/prepare_target.py' in command)
    initialize = next(index for index, command in enumerate(calls)
                      if 'bugboard/scripts/init_demo.py' in command)
    assert prepare < initialize
    check = next(index for index, command in enumerate(calls)
                 if 'scripts/codegraph/deploy_codegraph.py' in command)
    assert calls[check][-1] == '--check'
    assert check < prepare
    assert calls[-1] == [str(module.venv_python(tmp_path)), 'scripts/bootstrap/services.py']
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
    assert not any('bugboard/scripts/prepare_target.py' in command for command in calls)
    assert not any('scripts/codegraph/deploy_codegraph.py' in command for command in calls)
    assert calls[-1][-2:] == ['scripts/bootstrap/services.py', '--dev']
    assert (tmp_path / '.env').read_text(encoding='utf-8') == 'TRACEFIX_API_KEY=\n'


def test_full_web_installs_runtime_before_preparing_target(tmp_path, monkeypatch):
    module, calls, _ = bootstrap_fixture(tmp_path, monkeypatch)
    assert module.main(['--web', '--skip-build', '--plain']) == 0
    install = calls.index(['npm', 'ci', '--include=optional'])
    check = next(index for index, command in enumerate(calls)
                 if 'scripts/codegraph/deploy_codegraph.py' in command)
    prepare = next(index for index, command in enumerate(calls)
                   if 'bugboard/scripts/prepare_target.py' in command)
    assert install < check < prepare
    assert sum(command[:2] == ['npm', 'ci'] for command in calls) == 1


def test_missing_runtime_stops_web_before_database_or_services(tmp_path, monkeypatch):
    module, calls, _ = bootstrap_fixture(tmp_path, monkeypatch)
    original = module.run

    def run(argv, **kwargs):
        if 'scripts/codegraph/deploy_codegraph.py' in argv:
            raise RuntimeError('CodeGraph 版本不匹配')
        return original(argv, **kwargs)

    monkeypatch.setattr(module, 'run', run)
    with pytest.raises(RuntimeError, match='CodeGraph 版本不匹配'):
        module.main(['--web', '--skip-install', '--skip-build', '--plain'])
    assert not any(command[0] == 'docker' for command in calls)
    assert not any('scripts/bootstrap/services.py' in command for command in calls)


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
