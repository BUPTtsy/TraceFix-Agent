"""Supervise local Web services after bootstrap has prepared the environment."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def ensure_ports_available(ports):
    for port in ports:
        with socket.socket() as listener:
            if os.name == 'nt':
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(('127.0.0.1', port))
            except OSError as error:
                raise RuntimeError(f'端口 {port} 已被占用，请先停止已有服务后重试。') from error


def stop_process(child):
    if os.name == 'nt':
        if child.poll() is None:
            subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            child.kill()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=5)


def wait_ready(children, urls, timeout=30):
    deadline = time.monotonic() + timeout
    pending = list(urls)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while pending:
        for name, child in children:
            if child.poll() is not None:
                raise RuntimeError(f'{name} 启动失败（退出码 {child.returncode}），请查看上方日志。')
        for url in pending[:]:
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status == 200:
                        pending.remove(url)
            except (OSError, urllib.error.URLError):
                pass
        if pending:
            if time.monotonic() >= deadline:
                raise RuntimeError('服务启动超时，请查看上方日志。')
            time.sleep(0.1)


def serve(*, dev=False):
    load_dotenv(ROOT / '.env', override=False, encoding='utf-8-sig')
    node = shutil.which('node')
    if not node:
        raise RuntimeError('找不到 Node.js，请安装 Node.js 22.13+。')
    port = int(os.getenv('PORT') or '3000')
    if not 1 <= port <= 65535:
        raise ValueError('PORT 必须介于 1 与 65535 之间')
    if dev and port != 3000:
        raise ValueError('开发模式的 Vite 代理要求 PORT=3000；请移除自定义 PORT 或使用普通 Web 模式。')
    if dev and os.getenv('TRACEFIX_CONTROL_HOST', '127.0.0.1') not in {'127.0.0.1', '0.0.0.0'}:
        raise ValueError('开发模式要求 TRACEFIX_CONTROL_HOST 为 127.0.0.1 或 0.0.0.0。')
    ensure_ports_available([port, 5173] if dev else [port])
    host = os.getenv('TRACEFIX_CONTROL_HOST') or '127.0.0.1'
    local_host = '127.0.0.1' if host == '0.0.0.0' else '[::1]' if host in {'::', '::1'} else host
    api_url = f'http://{local_host}:{port}'
    web_url = 'http://127.0.0.1:5173' if dev else api_url
    environment = {**os.environ, 'NODE_ENV': 'development' if dev else 'production'}
    process_options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    children = []
    previous_handlers = {}

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    try:
        signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, 'SIGBREAK'):
            signals.append(signal.SIGBREAK)
        for signum in signals:
            previous_handlers[signum] = signal.signal(signum, interrupt)
        print('正在启动 TraceFix HTTP 服务与前台页面……', flush=True)
        child = subprocess.Popen([node, 'backend/apps/console-api/server/index.mjs'],
                                 cwd=ROOT, env=environment, **process_options)
        children.append(('HTTP 服务', child))
        if dev:
            child = subprocess.Popen([node, str(ROOT / 'node_modules/vite/bin/vite.js')],
                                     cwd=ROOT / 'frontend/apps/web', env=environment, **process_options)
            children.append(('Vite 前台', child))
        wait_ready(children, [api_url + '/health', web_url + '/'])
        print(f'\nTraceFix 已就绪\n  前台：{web_url}\n  后台：{api_url}\n'
              '  在页面选择项目并启动 Test / Repair / Chat。\n'
              '  Ctrl+C 停止本次启动的服务及其子进程。\n'
              '  PostgreSQL 容器会保留，停止数据库请执行 docker compose stop postgres。\n', flush=True)
        while True:
            for name, child in children:
                if child.poll() is not None:
                    raise RuntimeError(f'{name} 已退出（退出码 {child.returncode}），正在停止其余服务。')
            time.sleep(0.2)
    except KeyboardInterrupt:
        print('\n正在停止 Web 服务……', flush=True)
        return 130
    finally:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            for name, child in reversed(children):
                stop_process(child)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description='启动已准备好的 TraceFix Web 服务')
    parser.add_argument('--dev', action='store_true', help='同时启动 Vite 开发服务')
    args = parser.parse_args()
    try:
        return serve(dev=args.dev)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Web 启动失败：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
