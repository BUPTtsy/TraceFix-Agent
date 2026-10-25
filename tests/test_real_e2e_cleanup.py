import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest


def demo_initializer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / 'bugboard/scripts/init_demo.py'
    script.parent.mkdir(parents=True)
    shutil.copyfile(root / 'bugboard/scripts/init_demo.py', script)
    shutil.copyfile(root / 'bugboard/scripts/prepare_target.py', script.with_name('prepare_target.py'))
    cases = tmp_path / 'evals/cases.py'
    cases.parent.mkdir()
    shutil.copyfile(root / 'evals/cases.py', cases)
    return script


@pytest.mark.parametrize('empty_directory', [False, True])
def test_demo_initialization_rejects_unavailable_remote_before_copy(tmp_path, empty_directory):
    script = demo_initializer(tmp_path)
    if empty_directory:
        (tmp_path / 'bugboard/target').mkdir()
    destination = tmp_path / 'demo'

    result = subprocess.run([sys.executable, '-X', 'utf8', str(script), '--case', 'B01',
        '--repo-url', str(tmp_path / 'missing.git'),
        '--destination', str(destination)], capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30)

    assert result.returncode != 0
    assert '测试仓库 Git 操作失败' in result.stderr
    assert not destination.exists()
    assert not list((tmp_path / '.tracefix/repositories').glob('.bugboard-clone-*'))


@pytest.mark.parametrize('case', ['clean', 'B01'])
def test_demo_initialization_copies_template_and_freezes_case_in_baseline(tmp_path, case):
    script = demo_initializer(tmp_path)
    source = tmp_path / 'upstream'
    (source / 'src').mkdir(parents=True)
    (source / 'package.json').write_text('{"name":"bugboard"}\n', encoding='utf-8')
    (source / 'package-lock.json').write_text('{}\n', encoding='utf-8')
    original = "{method: 'PATCH', body: JSON.stringify(fields)}"
    (source / 'src/api.ts').write_text(original, encoding='utf-8')
    (source / 'node_modules').mkdir()
    (source / 'node_modules/excluded.txt').write_text('dependency', encoding='utf-8')
    (source / 'Dockerfile').write_text('FROM node', encoding='utf-8')
    cases = tmp_path / 'evals/cases.py'
    cases.write_text('CASES = ' + repr([{'id': 'B01', 'file': 'src/api.ts',
                                       'before': original, 'after': original.replace("'PATCH'", "'POST'")}]),
                     encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@localhost', 'commit', '-qm', '测试远程基线'], check=True)
    revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'clone', '--bare', str(source), str(remote)], check=True,
                    capture_output=True)
    destination = tmp_path / 'demo'

    result = subprocess.run([sys.executable, str(script), '--case', case,
        '--repo-url', str(remote), '--revision', revision,
        '--destination', str(destination)], capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=30)

    assert result.returncode == 0, result.stderr
    expected = original if case == 'clean' else original.replace("'PATCH'", "'POST'")
    assert (destination / 'src/api.ts').read_text(encoding='utf-8') == expected
    assert (source / 'src/api.ts').read_text(encoding='utf-8') == original
    assert not (destination / 'node_modules').exists()
    assert not (destination / 'Dockerfile').exists()
    baseline = subprocess.run(['git', '-C', str(destination), 'show', 'HEAD:src/api.ts'],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert baseline.returncode == 0 and baseline.stdout == expected


@pytest.mark.parametrize('api_key', ['', '   '])
def test_real_e2e_required_preflight_reports_missing_secret(tmp_path, monkeypatch, capsys, api_key):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    main = module['main']
    monkeypatch.setenv('TRACEFIX_API_KEY', api_key)
    monkeypatch.setenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('TRACEFIX_TEXT_MODEL', 'deepseek-chat')
    # SDK 配置来自宿主环境时会改变缺失项，测试内统一清空。
    for name in list(os.environ):
        if name.startswith(('TRACEFIX_CLAUDE_', 'TRACEFIX_SDK_')):
            monkeypatch.delenv(name)
    monkeypatch.setitem(main.__globals__, 'load_local_env', lambda: None)
    monkeypatch.setattr(module['shutil'], 'which', lambda name: name)
    monkeypatch.setitem(main.__globals__, 'command', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='linux'))
    output = tmp_path / 'report.json'

    assert main(['--required', '--preflight-only', '--output', str(output)]) == 2

    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'failed'
    # repair 编码只走 Claude Agent SDK，未配置 SDK 时同样列为缺失。
    assert [item.split('（', 1)[0] for item in report['missing']] == [
        'TRACEFIX_API_KEY', 'TRACEFIX_CLAUDE_MODEL', 'TRACEFIX_CLAUDE_API_KEY',
        'TRACEFIX_SDK_WORKER_IMAGE', 'TRACEFIX_SDK_NETWORK', 'TRACEFIX_SDK_EGRESS_PROXY']
    assert 'Repository secret' in report['message']
    assert 'TRACEFIX_CLAUDE_*' in report['message']
    assert 'Repository secret' in capsys.readouterr().out


def test_real_e2e_preflight_does_not_run_flow(tmp_path, monkeypatch, capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    main = module['main']
    monkeypatch.setitem(main.__globals__, 'load_local_env', lambda: None)
    monkeypatch.setitem(main.__globals__, 'prerequisites', lambda: [])
    # preflight-only 先于依赖安装，不得检查 runner 依赖。
    monkeypatch.setitem(main.__globals__, 'runtime_prerequisites', lambda: pytest.fail('preflight must not check runner dependencies'))
    monkeypatch.setitem(main.__globals__, 'run_flow', lambda *args: pytest.fail('preflight must not launch real E2E'))
    output = tmp_path / 'report.json'

    assert main(['--required', '--preflight-only', '--output', str(output)]) == 0

    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'ready'
    assert 'REAL_E2E: READY' in capsys.readouterr().out


def test_real_e2e_runtime_prerequisites_follow_runner_dependency_file(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    runtime_prerequisites = module['runtime_prerequisites']
    monkeypatch.setitem(runtime_prerequisites.__globals__, 'ROOT', tmp_path)
    assert runtime_prerequisites() == ['Playwright 验收运行器依赖（npm ci --prefix backend/packages/acceptance-runner）']
    cli = tmp_path / 'backend/packages/acceptance-runner/node_modules/@playwright/test/cli.js'
    cli.parent.mkdir(parents=True)
    cli.touch()
    assert runtime_prerequisites() == []


def test_real_e2e_required_reports_missing_runner_dependencies_without_running_flow(tmp_path, monkeypatch, capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    main = module['main']
    item = 'Playwright 验收运行器依赖（npm ci --prefix backend/packages/acceptance-runner）'
    monkeypatch.setitem(main.__globals__, 'load_local_env', lambda: None)
    monkeypatch.setitem(main.__globals__, 'prerequisites', lambda: [])
    monkeypatch.setitem(main.__globals__, 'runtime_prerequisites', lambda: [item])
    monkeypatch.setitem(main.__globals__, 'run_flow', lambda *args: pytest.fail('missing runner dependencies must not launch real E2E'))
    output = tmp_path / 'report.json'

    assert main(['--required', '--output', str(output)]) == 2

    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'failed'
    assert item in report['missing']
    assert item in capsys.readouterr().out


def test_real_e2e_uses_postgres_password_for_compose_and_dsn(monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    monkeypatch.setenv('POSTGRES_PASSWORD', 'pa:ss@word#1')

    dsn, compose_env = module['postgres_connection_config']()

    assert unquote(urlsplit(dsn).password or '') == 'pa:ss@word#1'
    assert compose_env == 'POSTGRES_PASSWORD=pa:ss@word#1\n'


@pytest.mark.parametrize('source', ['argument', 'environment'])
def test_real_e2e_database_override_is_bound_to_agent_and_state(tmp_path, monkeypatch, source):
    root = Path(__file__).resolve().parents[1]
    module = runpy.run_path(str(root / 'scripts/checks/verify_real_e2e.py'))
    run_flow = module['run_flow']
    specification = tmp_path / 'spec.json'
    specification.write_bytes((root / 'profiles/persistence.spec.json').read_bytes())
    dsn = 'postgresql://tracefix:test@127.0.0.1:55432/tracefix_e2e_isolated'
    monkeypatch.delenv('TRACEFIX_E2E_DATABASE_URL', raising=False)
    if source == 'environment':
        monkeypatch.setenv('TRACEFIX_E2E_DATABASE_URL', dsn)
    calls = []

    def command(arguments, **options):
        arguments = [str(argument) for argument in arguments]
        calls.append((arguments, options))
        if 'bugboard/scripts/init_demo.py' in arguments:
            assert options['env']['TRACEFIX_DATABASE_URL'] == dsn
            Path(arguments[-1]).mkdir(parents=True)
        if '--run' in arguments:
            assert options['env']['TRACEFIX_DATABASE_URL'] == dsn
            artifact = Path(options['env']['TRACEFIX_DATA']) / 'artifacts/bugboard/run_isolated'
            artifact.mkdir(parents=True)
            return SimpleNamespace(returncode=0, stdout='')
        return SimpleNamespace(returncode=0, stdout='existing-postgres' if 'ps' in arguments else '')

    def load_state(data_root, actual_dsn, run_id):
        assert actual_dsn == dsn and run_id == 'run_isolated'
        raise RuntimeError('isolated state boundary')

    monkeypatch.setitem(run_flow.__globals__, 'ROOT', tmp_path)
    monkeypatch.setitem(run_flow.__globals__, 'command', command)
    monkeypatch.setitem(run_flow.__globals__, 'load_state', load_state)
    with pytest.raises(RuntimeError, match='isolated state boundary'):
        run_flow(SimpleNamespace(case='B01', spec=specification,
                                database_url=dsn if source == 'argument' else None), tmp_path / 'report.json')
    assert not any('down' in arguments for arguments, options in calls)


@pytest.mark.parametrize('already_running', [False, True])
def test_real_e2e_preserves_preexisting_postgres_on_failure(tmp_path, monkeypatch, already_running):
    root = Path(__file__).resolve().parents[1]
    module = runpy.run_path(str(root / 'scripts/checks/verify_real_e2e.py'))
    spec = tmp_path / 'persistence.spec.json'
    spec.write_bytes((root / 'profiles/persistence.spec.json').read_bytes())
    run_flow = module['run_flow']
    commands = []

    def command(arguments, **options):
        arguments = [str(argument) for argument in arguments]
        commands.append(arguments)
        if 'bugboard/scripts/init_demo.py' in arguments:
            Path(arguments[-1]).mkdir(parents=True)
        if '--run' in arguments:
            frozen = Path(arguments[arguments.index('--spec') + 1])
            assert frozen != spec
            assert frozen.read_bytes() == spec.read_bytes()
            raise RuntimeError('injected launch failure')
        return SimpleNamespace(returncode=0, stdout='existing-container' if 'ps' in arguments and already_running else '')

    monkeypatch.setitem(run_flow.__globals__, 'ROOT', tmp_path)
    monkeypatch.setitem(run_flow.__globals__, 'command', command)
    with pytest.raises(RuntimeError, match='injected launch failure'):
        run_flow(SimpleNamespace(case='B01', spec=spec), tmp_path / 'report.json')
    teardown = [arguments for arguments in commands if 'down' in arguments]
    assert len(teardown) == (0 if already_running else 1)
    assert (tmp_path / 'report.json').is_file()
