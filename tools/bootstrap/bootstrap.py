"""Native Windows/portable bootstrap. Only Python's standard library is required."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend/packages/agent/src'))

from tracefix.messages import ChineseArgumentParser


def safe_diagnostic(text):
    text = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', text)
    for name, value in os.environ.items():
        if len(value) >= 4 and re.search(r'(?i)(?:api[_-]?key|token|password|secret|credential)', name):
            text = text.replace(value, '[redacted]')
    text = re.sub(r'(?im)((?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+',
                  r'\1[redacted]', text)
    text = re.sub(r'''(?ix)(["']?(?:[\w-]*(?:api[_-]?key|token|password|secret|credential))["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s&,;]+)''',
                  r'\1[redacted]', text)
    text = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]+', '[redacted]', text)
    return re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1[redacted]@', text)


def venv_python(root: Path, windows: bool | None = None):
    if windows is None:
        windows = sys.platform == 'win32'
    return root / '.venv' / ('Scripts/python.exe' if windows else 'bin/python')


def run(argv, *, capture=False, timeout=None):
    # Never evaluate a shell string or put a credential on a command line.
    argv = [str(x) for x in argv]
    result = subprocess.run(argv, cwd=ROOT, capture_output=capture, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        detail = safe_diagnostic('\n'.join(part.strip() for part in (result.stdout, result.stderr)
                                          if capture and part and part.strip()))[-3000:]
        raise RuntimeError(f'命令执行失败（退出码 {result.returncode}）：{argv[0]}'
                           + (f'\n外部命令原始输出：\n{detail}' if detail else ''))
    return result.stdout.strip() if capture else ''


class StartupProgress:
    def __init__(self, total, *, plain=False, stream=None):
        self.total = total
        self.current = 0
        self.stream = stream if stream is not None else sys.stdout
        self.started = time.monotonic()
        self.animated = not (plain or 'NO_COLOR' in os.environ or os.getenv('TERM') == 'dumb')
        self.animated = self.animated and bool(getattr(self.stream, 'isatty', lambda: False)())
        self.animated = self.animated and self._enable_terminal()
        self.unicode = self._supports_unicode()
        self._stop = threading.Event()
        self._worker = None
        self._cursor_hidden = False

    def _enable_terminal(self):
        if os.name != 'nt':
            return True
        try:
            import ctypes
            import msvcrt
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(self.stream.fileno()))
            mode = ctypes.c_ulong()
            kernel = ctypes.windll.kernel32
            return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                        and kernel.SetConsoleMode(handle, mode.value | 4))
        except (AttributeError, OSError, ValueError):
            return False

    def _supports_unicode(self):
        try:
            '✻✓━·'.encode(getattr(self.stream, 'encoding', None) or 'utf-8')
            return True
        except (LookupError, UnicodeEncodeError):
            return False

    def _write(self, text):
        encoding = getattr(self.stream, 'encoding', None) or 'utf-8'
        self.stream.write(text.encode(encoding, errors='replace').decode(encoding))
        self.stream.flush()

    def _accent(self, text):
        return f'\033[38;2;218;119;86m{text}\033[0m' if self.animated else text

    def welcome(self, mode='Agent'):
        mark = '✻' if self.unicode else '*'
        self._write('\n' + self._accent(f'  {mark}  TraceFix') + f'  /  {mode}\n')
        self._write('  准备工作环境 · 进度按已完成步骤计算\n\n')

    def _line(self, label, marker, elapsed=0, *, live=False):
        columns = shutil.get_terminal_size((80, 24)).columns
        width = max(4, min(18, columns // 5))
        filled = min(width, int(width * self.current / self.total)) if self.total else width
        full, empty = ('━', '·') if self.animated and self.unicode else ('#', '-')
        bar = full * filled + empty * (width - filled)
        content = f'  {marker} {label}  [{bar}] {self.current}/{self.total}  {elapsed:.1f}s'
        if self.animated:
            available = max(1, columns - 1)
            clipped = []
            for character in content:
                cell_width = 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
                if cell_width > available:
                    break
                clipped.append(character)
                available -= cell_width
            content = ''.join(clipped)
            self._write('\r\033[2K' + self._accent(content) + ('' if live else '\n'))
        else:
            self._write(content + '\n')

    def _animate(self, label, started):
        frames = ('·', '✢', '✳', '✶', '✻', '✽', '✻', '✶', '✳', '✢') if self.unicode else ('|', '/', '-', '\\')
        frame = 0
        while not self._stop.wait(0.1):
            frame = (frame + 1) % len(frames)
            self._line(label, frames[frame], time.monotonic() - started, live=True)

    def perform(self, label, operation):
        started = time.monotonic()
        self._stop.clear()
        marker = '·' if self.animated and self.unicode else '...'
        try:
            if self.animated:
                self._write('\033[?25l')
                self._cursor_hidden = True
            self._line(label, marker, live=self.animated)
            if self.animated:
                self._worker = threading.Thread(target=self._animate, args=(label, started), daemon=True)
                self._worker.start()
            result = operation()
        except BaseException as error:
            self.close()
            self._line(label, '已取消' if isinstance(error, KeyboardInterrupt) else '失败', time.monotonic() - started)
            raise
        else:
            self.close()
            self.current += 1
            self._line(label, '完成', time.monotonic() - started)
            return result
        finally:
            self.close()

    def close(self):
        self._stop.set()
        if self._worker is not None:
            if self._worker.is_alive():
                self._worker.join()
            self._worker = None
        if self._cursor_hidden:
            self._write('\033[?25h')
            self._cursor_hidden = False

    def run(self, label, argv):
        return self.perform(label, lambda: run(argv, capture=True))

    def finish(self, label):
        self.close()
        self._write('\n' + self._accent(f'  启动准备完成 · {self.current}/{self.total} 步骤 · {time.monotonic() - self.started:.1f}s')
                    + f'\n  {label}\n\n')


def prerequisites(require_docker=True):
    result = {'python': platform.python_version(), 'python_64_bit': struct.calcsize('P') == 8,
              'git': bool(shutil.which('git')), 'docker': bool(shutil.which('docker')),
              'docker_linux_engine': False, 'compose_v2': False}
    if require_docker and result['docker']:
        try:
            result['docker_linux_engine'] = run(['docker', 'info', '--format', '{{.OSType}}'], capture=True, timeout=30) == 'linux'
            result['compose_v2'] = bool(run(['docker', 'compose', 'version', '--short'], capture=True, timeout=30))
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
    okay = sys.version_info[:2] == (3, 12) and result['python_64_bit'] and result['git']
    if require_docker:
        okay = okay and result['docker_linux_engine'] and result['compose_v2']
    return result, okay


def main(argv=None):
    parser = ChineseArgumentParser(description='TraceFix 启动器（其余参数将传给 tracefix）')
    parser.add_argument('--check', action='store_true', help='只检查主机前置条件，不安装依赖')
    parser.add_argument('--smoke', action='store_true', help='运行离线假数据 Smoke，不需要 Docker 或 API')
    parser.add_argument('--preview', action='store_true', help='预览终端界面，不需要 Docker 或 API')
    parser.add_argument('--web', action='store_true', help='准备完整 Agent 环境并启动 Web 控制台与 HTTP 服务')
    parser.add_argument('--console-only', action='store_true', help='仅准备并启动 Web 控制台，不启动 Docker 或检查模型配置')
    parser.add_argument('--dev', action='store_true', help='Web 模式使用 Vite 开发服务（5173），默认提供构建后的页面（3000）')
    parser.add_argument('--skip-install', action='store_true', help='复用已安装的环境')
    parser.add_argument('--skip-build', action='store_true', help='复用现有沙箱镜像')
    parser.add_argument('--default-mode', choices=['chat', 'test', 'repair'], default='chat', help=argparse.SUPPRESS)
    options, agent_args = parser.parse_known_args(argv)
    web = options.web or options.console_only
    if options.dev and not web:
        parser.error('--dev 需要 --web 或 --console-only')
    if web and (options.smoke or options.preview):
        parser.error('Web 模式不能与 --smoke 或 --preview 同时使用')
    if web and any(argument != '--plain' for argument in agent_args):
        parser.error('Web 模式不接收 CLI 任务参数，请在网页中选择项目并启动任务')
    os.chdir(ROOT)
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    offline = options.smoke or options.preview
    python = venv_python(ROOT)
    create_venv = not options.skip_install and not python.is_file()
    total = 2
    if not options.skip_install:
        total += 2 + int(create_venv)
    if options.console_only:
        total += 1 + int(not options.skip_install)
    elif not offline:
        total += 3 + (2 if not options.skip_build else 1)
        total += 1 + int(not options.skip_install)
    progress = StartupProgress(total, plain='--plain' in agent_args)

    def check_prerequisites():
        result, okay = prerequisites(require_docker=not (offline or options.console_only))
        if options.check:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        if not okay:
            detail = '' if options.check else json.dumps(result, ensure_ascii=False, indent=2) + '\n'
            raise RuntimeError(detail + '请安装 Python 3.12 x64 和 Git。真实运行前请启动使用 Linux 容器引擎和 Compose v2 的 Docker。离线使用请加 --smoke 或 --preview。')
        return result

    if options.check:
        check_prerequisites()
        return 0
    progress.welcome('Web 控制台' if web else '界面预览' if options.preview else '离线 Smoke' if options.smoke else 'Agent')
    progress.perform('检查启动环境', check_prerequisites)
    if not offline and not (ROOT / '.env').exists():
        shutil.copyfile(ROOT / '.env.example', ROOT / '.env')
        if not options.console_only and not os.getenv('TRACEFIX_API_KEY'):
            print('已创建 .env。请填入 TRACEFIX_API_KEY，然后重新执行刚才的命令。')
            return 2
    if not offline:
        npm = shutil.which('npm')
        node = shutil.which('node')
        if not npm or not node:
            raise RuntimeError('TypeScript CLI 需要 Node.js 22.13+ 和 npm')
        version = run([node, '--version'], capture=True)
        match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', version)
        if not match or (int(match[1]), int(match[2])) < (22, 13):
            raise RuntimeError('TypeScript CLI 需要 Node.js 22.13+ 和 npm')
    if options.skip_install and not python.is_file():
        raise RuntimeError('找不到已安装的 .venv。请先不带 --skip-install 执行一次。')
    if not options.skip_install:
        if not python.is_file():
            if (ROOT / '.venv').exists():
                raise RuntimeError('.venv 属于其他平台或不完整。请重命名后重试，启动器不会覆盖它。')
            progress.run('创建 Python 虚拟环境', [sys.executable, '-m', 'venv', ROOT / '.venv'])
        wheelhouse = ROOT / 'vendor/wheels-win_amd64'
        install_options = ['--no-index', '--find-links', str(wheelhouse)] if sys.platform == 'win32' and wheelhouse.is_dir() else []
        progress.run('安装锁定依赖', [python, '-m', 'pip', 'install', '--disable-pip-version-check',
            *install_options, '-r', 'requirements.lock'])
        progress.run('安装 TraceFix', [python, '-m', 'pip', 'install', '--disable-pip-version-check',
            '--no-deps', '--no-build-isolation', '-e', '.'])
    progress.run('检查 Python 环境', [python, '-c', "import sys; assert sys.version_info[:2] == (3, 12), '现有虚拟环境必须使用 Python 3.12'"])
    if offline:
        progress.finish('正在进入界面预览。' if options.preview else '正在运行离线 Smoke。')
        run([python, 'tools/bootstrap/launch.py', '--preview' if options.preview else '--smoke', *agent_args])
        return 0
    if options.console_only:
        if not options.skip_install:
            progress.run('安装控制台 workspace 依赖', [npm, 'ci'])
        progress.run('构建控制台软件包', [npm, 'run', 'build'])
        progress.finish('正在启动 Web 控制台；Test / Repair 需要完整 Agent 环境。')
        run([python, 'tools/bootstrap/services.py', *(['--dev'] if options.dev else [])])
        return 0
    progress.run('检查模型配置', [python, '-c', "from dotenv import load_dotenv; import os; load_dotenv('.env', encoding='utf-8-sig'); assert os.getenv('TRACEFIX_API_KEY'), '请先在 .env 中填写 TRACEFIX_API_KEY'"])
    progress.run('启动 PostgreSQL', ['docker', 'compose', '--env-file', '.env', 'up', '-d', '--wait', 'postgres'])
    if not options.skip_build:
        progress.run('构建 BugBoard 镜像', ['docker', 'build', '-f', 'bugboard/docker/Dockerfile', '-t', 'tracefix-bugboard:1.0', 'bugboard/target'])
        progress.run('构建浏览器镜像', ['docker', 'build', '-f', 'Dockerfile.browser', '-t', 'tracefix-browser:1.0', '.'])
    else:
        progress.run('检查沙箱镜像', ['docker', 'image', 'inspect', '--format', '{{.Id}}', 'tracefix-bugboard:1.0', 'tracefix-browser:1.0'])
    progress.run('初始化演示项目', [python, 'bugboard/scripts/init_demo.py', '--case', 'B01'])
    if not options.skip_install:
        progress.run('安装 TypeScript 控制台依赖', [npm, 'ci'])
    progress.run('构建控制台软件包', [npm, 'run', 'build', *([] if web else ['--workspace', '@tracefix/cli'])])
    if web:
        progress.finish('正在启动 Web 控制台；请在页面选择项目并启动 Agent Run。')
        run([python, 'tools/bootstrap/services.py', *(['--dev'] if options.dev else [])])
        return 0
    if not [argument for argument in agent_args if argument != '--plain'] and options.default_mode == 'repair':
        agent_args = ['--mode', 'repair', '--spec', 'profiles/persistence.spec.json', *agent_args]
    progress.finish('正在进入 TraceFix Agent。')
    run([node, 'frontend/apps/cli/dist/cli.mjs', *agent_args])
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'启动失败：{safe_diagnostic(str(error))}', file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print('启动已取消。', file=sys.stderr)
        raise SystemExit(130)
