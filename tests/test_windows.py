"""Portable contract tests plus real OS branches when run on Windows CI."""
import asyncio
import csv
import importlib.util
import io
import os
import sys
import threading
import time
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracefix.execution import platforms
from tracefix.execution.runner import DockerRunner, process
from tracefix.execution.workspace import Workspace, git
from tracefix.runtime.contracts import FileEdit, PatchProposal, digest
from tracefix.storage.checkpoints import ThreadedPostgresSaver


def test_windows_container_identity_and_mount_without_posix(monkeypatch):
    monkeypatch.setattr(platforms, 'is_windows', lambda: True)
    assert platforms.container_user() == '1000:1000'
    assert 'start_new_session' not in platforms.subprocess_options()
    assert platforms.subprocess_options()['creationflags'] == 0x200
    mount = platforms.bind_mount(PureWindowsPath(r'C:\Users\研究生\My Project, demo\run'))
    assert next(csv.reader([mount])) == ['type=bind', 'src=C:/Users/研究生/My Project, demo/run', 'dst=/app', 'readonly']


async def test_windows_cancellation_targets_only_created_child(monkeypatch):
    monkeypatch.setattr(platforms, 'is_windows', lambda: True)
    killer = SimpleNamespace(wait=AsyncMock(return_value=0), kill=lambda: None)
    launch = AsyncMock(return_value=killer)
    monkeypatch.setattr(platforms.asyncio, 'create_subprocess_exec', launch)
    child = SimpleNamespace(pid=54321, returncode=None, wait=AsyncMock(return_value=1))
    child.kill = lambda: setattr(child, 'returncode', 1)
    await platforms.terminate_tree(child)
    assert launch.call_args.args == ('taskkill.exe', '/PID', '54321', '/T', '/F')
    child.wait.assert_awaited_once()


@pytest.mark.parametrize('path', [r'C:\secret', 'C:secret', 'src/file.ts:stream', 'src/../private',
    'src/NUL.ts', 'src/COM1', '.GIT/config', '.git./config', 'src/title.ts ', r'src\..\private'])
def test_windows_reserved_aliases_and_ads_are_rejected(path):
    with pytest.raises(PermissionError):
        platforms.safe_relative(path)


def test_unicode_crlf_hash_and_patch_survive_host_git_config(tmp_path):
    root = tmp_path / '中文 project'
    (root / 'src').mkdir(parents=True)
    raw = '// 中文\r\nexport const value = 1;\r\n'.encode('utf-8')
    (root / 'src/value.ts').write_bytes(raw)
    git(root, 'init', '-q')
    git(root, 'config', 'core.autocrlf', 'true')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=CI', '-c', 'user.email=ci@localhost', 'commit', '-qm', 'initial')
    workspace = Workspace(root, ['src/**'])
    assert digest(workspace.baseline_bytes('src/value.ts')) == digest(raw)
    assert '\r\n' in workspace.read('src/value.ts')
    patch = PatchProposal(summary='unicode test', evidence_refs=['fixture'], edits=[FileEdit(
        path='src/value.ts', before_hash=digest(raw), content='// 中文\nexport const value = 2;\n')])
    assert workspace.apply(patch)['patch_hash']
    assert 'value = 2' in workspace.diff()


async def test_threaded_saver_delegates_all_async_calls_off_event_loop():
    # The bridge reuses the official synchronous saver. DB SQL is tested separately.
    saver = ThreadedPostgresSaver(object())
    main_thread = threading.get_ident()
    threads = []
    def call(value=None):
        threads.append(threading.get_ident())
        return value
    saver.get_tuple = lambda config: call({'id': config['id']})
    saver.put = lambda *args: call({'config': args[0]})
    saver.put_writes = lambda *args: call()
    saver.delete_thread = lambda *args: call()
    saver.list = lambda *args, **kwargs: iter(call([{'id': 'a'}, {'id': 'b'}]))
    assert await saver.aget_tuple({'id': 'run'}) == {'id': 'run'}
    assert await saver.aput({'id': 'run'}, {}, {}, {}) == {'config': {'id': 'run'}}
    await saver.aput_writes({}, [('key', 'value')], 'task', 'path')
    await saver.adelete_thread('run')
    assert [r async for r in saver.alist(None, limit=2)] == [{'id': 'a'}, {'id': 'b'}]
    assert len(threads) == 5 and all(t != main_thread for t in threads)


async def test_real_subprocess_unicode_and_timeout_cleanup():
    result = await process([sys.executable, '-X', 'utf8', '-c', 'print("中文 child")'])
    assert result['passed'] and '中文 child' in result['output']
    with pytest.raises(asyncio.TimeoutError):
        await process([sys.executable, '-c', 'import time;time.sleep(30)'], timeout=0.1)


def load_bootstrap():
    path = Path(__file__).resolve().parents[1] / 'scripts/bootstrap/bootstrap.py'
    spec = importlib.util.spec_from_file_location('tracefix_bootstrap_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_first_run_preserves_config_and_stops_before_install(tmp_path, monkeypatch):
    module = load_bootstrap()
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'prerequisites', lambda **kwargs: ({}, True))
    monkeypatch.delenv('TRACEFIX_API_KEY', raising=False)
    (tmp_path / '.env.example').write_text('TRACEFIX_API_KEY=\n', encoding='utf-8')
    calls = []
    monkeypatch.setattr(module, 'run', lambda *args, **kwargs: calls.append(args))
    monkeypatch.chdir(tmp_path)
    assert module.main([]) == 2
    assert (tmp_path / '.env').read_text(encoding='utf-8') == 'TRACEFIX_API_KEY=\n'
    assert not calls


def test_windows_venv_layout():
    module = load_bootstrap()
    root = Path('project')
    assert module.venv_python(root, windows=True) == root / '.venv/Scripts/python.exe'
    assert module.venv_python(root, windows=False) == root / '.venv/bin/python'


def test_bootstrap_progress_hides_success_output_and_keeps_failure_detail(tmp_path, monkeypatch, capsys):
    module = load_bootstrap()
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    progress = module.StartupProgress(1)
    progress.run('安装锁定依赖', [sys.executable, '-c', "print('Requirement already satisfied: hidden')"])
    output = capsys.readouterr().out
    assert '安装锁定依赖' in output
    assert '1/1' in output
    assert 'Requirement already satisfied' not in output

    with pytest.raises(RuntimeError, match='diagnostic tail'):
        module.StartupProgress(1).run('失败步骤',
            [sys.executable, '-c', "import sys; print('diagnostic tail', file=sys.stderr); sys.exit(3)"])


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_startup_animation_counts_completed_steps_and_cleans_up(monkeypatch, failure):
    module = load_bootstrap()
    stream = io.StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: True)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('TERM', raising=False)
    monkeypatch.setattr(module.StartupProgress, '_enable_terminal', lambda self: True)
    progress = module.StartupProgress(1, stream=stream)

    def operation():
        time.sleep(0.23)
        assert progress.current == 0
        if failure:
            raise failure('startup stopped')
        return 'ready'

    if failure:
        with pytest.raises(failure):
            progress.perform('准备中文环境', operation)
    else:
        assert progress.perform('准备中文环境', operation) == 'ready'
    output = stream.getvalue()
    assert output.count('\r') >= 3
    assert '\x1b[?25l' in output and '\x1b[?25h' in output
    assert progress.current == (0 if failure else 1)
    assert progress._worker is None
    assert not progress._cursor_hidden
    assert '1/1' not in output if failure else '1/1' in output


@pytest.mark.parametrize('plain,no_color,tty', [(True, False, True), (False, True, True), (False, False, False)])
def test_startup_static_modes_have_no_terminal_controls(monkeypatch, plain, no_color, tty):
    module = load_bootstrap()
    stream = io.StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: tty)
    monkeypatch.setattr(module.StartupProgress, '_enable_terminal', lambda self: True)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    progress = module.StartupProgress(1, plain=plain, stream=stream)
    progress.welcome('预览')
    progress.perform('检查环境', lambda: None)
    progress.finish('准备完成')
    output = stream.getvalue()
    assert '\x1b' not in output and '\r' not in output
    assert '0/1' in output and '1/1' in output


def test_bootstrap_failure_diagnostic_is_readable_and_redacted(tmp_path, monkeypatch):
    module = load_bootstrap()
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    diagnostic = '\x1b[31m中文错误\x1b[0m\nAuthorization: Bearer private-token\nAPI_KEY=private-key\nCookie: session=private-cookie'
    command = [sys.executable, '-X', 'utf8', '-c', f'import sys; print({diagnostic!r}, file=sys.stderr); sys.exit(3)']
    with pytest.raises(RuntimeError) as raised:
        module.StartupProgress(1, plain=True, stream=io.StringIO()).run('失败步骤', command)
    output = str(raised.value)
    assert '中文错误' in output and '退出码 3' in output
    assert 'private-' not in output and '\x1b' not in output
