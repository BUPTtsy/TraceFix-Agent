"""Host differences only; application and browser sandboxes remain Linux containers."""
import asyncio
import csv
import io
import os
import signal
import subprocess
import sys
from pathlib import PureWindowsPath


def is_windows():
    return sys.platform == 'win32'


def subprocess_options():
    if is_windows():
        # Windows asyncio uses ProactorEventLoop; no POSIX setsid/killpg calls.
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)}
    return {'start_new_session': True}


async def terminate_tree(process):
    if process.returncode is not None:
        return
    if is_windows():
        # Only the child PID created by this process is passed to taskkill.
        try:
            killer = await asyncio.create_subprocess_exec('taskkill.exe', '/PID', str(process.pid), '/T', '/F',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(killer.wait(), 10)
            except asyncio.TimeoutError:
                killer.kill()
                await killer.wait()
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def container_user():
    if is_windows():
        return '1000:1000'
    uid, gid = os.getuid(), os.getgid()
    return f'{uid}:{gid}' if uid else '1000:1000'


def bind_mount(root):
    # Docker CLI --mount is CSV; quote commas without shell interpolation.
    host = PureWindowsPath(str(root)).as_posix() if is_windows() else str(root)
    stream = io.StringIO()
    csv.writer(stream, lineterminator='').writerow(['type=bind', 'src='+host, 'dst=/app', 'readonly'])
    return stream.getvalue()


def safe_relative(value):
    """Use one portable path contract, including Windows device and ADS rules."""
    windows = PureWindowsPath(value)
    parts = value.split('/')
    if (not value or '\\' in value or windows.drive or windows.root or ':' in value
            or any(p in {'', '.', '..'} or p.endswith((' ', '.')) for p in parts)):
        raise PermissionError('路径必须是使用正斜杠的可移植相对路径')
    devices = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))}
    protected = {'.git', '.env', 'node_modules', '.tracefix', '__pycache__'}
    if any(p.lower().split('.')[0] in devices or p.lower() in protected
           or p.lower().startswith('.env.') for p in parts):
        raise PermissionError('受保护或保留的路径')
    return value


def is_link(path):
    return path.is_symlink() or path.is_junction()
