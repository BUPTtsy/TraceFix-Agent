import json
from pathlib import Path
import runpy
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest


@pytest.mark.parametrize('api_key', ['', '   '])
def test_real_e2e_required_preflight_reports_missing_secret(tmp_path, monkeypatch, capsys, api_key):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    main = module['main']
    monkeypatch.setenv('TRACEFIX_API_KEY', api_key)
    monkeypatch.setenv('TRACEFIX_BASE_URL', 'https://api.deepseek.com')
    monkeypatch.setenv('TRACEFIX_TEXT_MODEL', 'deepseek-chat')
    monkeypatch.setitem(main.__globals__, 'load_local_env', lambda: None)
    monkeypatch.setattr(module['shutil'], 'which', lambda name: name)
    monkeypatch.setitem(main.__globals__, 'command', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='linux'))
    output = tmp_path / 'report.json'

    assert main(['--required', '--preflight-only', '--output', str(output)]) == 2

    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'failed'
    assert report['missing'] == ['TRACEFIX_API_KEY']
    assert 'Repository secret' in report['message']
    assert 'Repository secret' in capsys.readouterr().out


def test_real_e2e_preflight_does_not_run_flow(tmp_path, monkeypatch, capsys):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    main = module['main']
    monkeypatch.setitem(main.__globals__, 'load_local_env', lambda: None)
    monkeypatch.setitem(main.__globals__, 'prerequisites', lambda: [])
    monkeypatch.setitem(main.__globals__, 'run_flow', lambda *args: pytest.fail('preflight must not launch real E2E'))
    output = tmp_path / 'report.json'

    assert main(['--required', '--preflight-only', '--output', str(output)]) == 0

    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'ready'
    assert 'REAL_E2E: READY' in capsys.readouterr().out


def test_real_e2e_uses_postgres_password_for_compose_and_dsn(monkeypatch):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/checks/verify_real_e2e.py'))
    monkeypatch.setenv('POSTGRES_PASSWORD', 'pa:ss@word#1')

    dsn, compose_env = module['postgres_connection_config']()

    assert unquote(urlsplit(dsn).password or '') == 'pa:ss@word#1'
    assert compose_env == 'POSTGRES_PASSWORD=pa:ss@word#1\n'


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
