"""Evaluator-owned, fail-closed Docker control transport for container workers."""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath


class BrokerRejected(ValueError):
    pass


def mount_argument(source, destination, readonly=True):
    stream = io.StringIO()
    fields = ['type=bind', 'src=' + str(source), 'dst=' + destination]
    if readonly:
        fields.append('readonly')
    csv.writer(stream, lineterminator='').writerow(fields)
    return stream.getvalue()


def plain_path(path):
    path = Path(path).absolute()
    if any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
           for item in (path, *path.parents)):
        raise BrokerRejected('linked broker path')
    return path.resolve()


class DockerBroker:
    def __init__(self, root, image_ids, *, docker_executable='docker', timeout=300, resource_prefix=None):
        self.root = plain_path(root)
        self.channel = self.root / 'broker'
        self.channel.mkdir(exist_ok=True)
        self.images = dict(image_ids)
        if not self.images or any(not re.fullmatch(r'sha256:[a-f0-9]{64}', value)
                                  for value in self.images.values()):
            raise BrokerRejected('immutable image inventory required')
        self.docker_executable = docker_executable
        self.timeout = timeout
        self.resource_prefix = resource_prefix or ('tf-' + uuid.uuid4().hex)
        if not re.fullmatch(r'tf-[a-zA-Z0-9_-]{3,100}', self.resource_prefix):
            raise BrokerRejected('invalid evaluator resource prefix')
        self.networks = set()
        self.containers = {}
        self.volumes = {}
        self.audit = []
        self.processes = set()
        self.seen = set()
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.thread = None

    def _owned_name(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'tf-[a-zA-Z0-9_-]{3,120}', value):
            raise BrokerRejected('invalid runtime resource name')
        if value != self.resource_prefix and not value.startswith(self.resource_prefix + '-'):
            raise BrokerRejected('resource outside current evaluation group')
        return value

    def _source(self, source):
        path = PurePosixPath(source)
        if not path.is_absolute() or '..' in path.parts or '\\' in source:
            raise BrokerRejected('invalid container source path')
        relative = path.relative_to('/agent-run') if path.is_relative_to('/agent-run') else None
        if relative is None or not relative.parts or relative.parts[0] not in {'data', 'tmp', 'source'}:
            raise BrokerRejected('mount outside runtime workspace')
        host = plain_path(self.root.joinpath(*relative.parts))
        if not host.is_relative_to(self.root) or not host.is_dir():
            raise BrokerRejected('mount source unavailable')
        return host

    def _mount(self, value):
        fields = next(csv.reader([value]))
        values = {}
        for item in fields:
            key, separator, field = item.partition('=')
            if key in values or key not in {'type', 'src', 'dst', 'readonly'}:
                raise BrokerRejected('unsupported mount option')
            if key == 'readonly' and separator:
                raise BrokerRejected('invalid readonly option')
            values[key] = field
        destination = values.get('dst')
        if destination not in {'/app', '/workspace', '/app/node_modules', '/workspace/node_modules'}:
            raise BrokerRejected('unsupported mount destination')
        if values.get('type') == 'volume':
            source = self.volumes.get(values.get('src'))
            if source is None or destination not in {'/app/node_modules', '/workspace/node_modules'}:
                raise BrokerRejected('unregistered dependency mount')
        elif values.get('type') == 'bind':
            source = self._source(values.get('src', ''))
            if destination not in {'/app', '/workspace'}:
                raise BrokerRejected('unsupported source mount')
        else:
            raise BrokerRejected('unsupported mount type')
        return mount_argument(source, destination, 'readonly' in values)

    def _run_arguments(self, arguments):
        flags = {'--rm', '-d', '-i', '--init', '--read-only', '--cap-drop=ALL',
                 '--security-opt=no-new-privileges', '--network=none'}
        paired = {'--name', '--network', '--network-alias', '--label', '--user', '--pids-limit',
                  '--memory', '--cpus', '--tmpfs', '--mount', '-e', '--workdir', '--entrypoint'}
        # Runtime 的资源限制使用 --key=value，仍统一经过成对选项的值校验。
        arguments = list(arguments)
        output = ['run']
        options = {}
        index = 0
        while index < len(arguments) and arguments[index].startswith('-'):
            argument = arguments[index]
            key, separator, value = argument.partition('=')
            if separator and key in paired and argument not in flags:
                arguments[index:index + 1] = [key, value]
                argument = key
            if argument in flags:
                if argument in options:
                    raise BrokerRejected('duplicate container option')
                options[argument] = True
                output.append(argument)
                index += 1
                continue
            if argument not in paired or index + 1 >= len(arguments):
                raise BrokerRejected('unsupported container option')
            value = arguments[index + 1]
            if argument in options and argument not in {'--tmpfs', '--mount', '-e'}:
                raise BrokerRejected('duplicate container option')
            options[argument] = value
            if argument == '--mount':
                value = self._mount(value)
            elif argument == '--name':
                self._owned_name(value)
                if value in self.containers:
                    raise BrokerRejected('container name already registered')
            elif argument == '--network':
                if value not in self.networks and value != 'none':
                    raise BrokerRejected('unregistered network')
            elif argument == '--network-alias':
                if value != 'app':
                    raise BrokerRejected('unsupported network alias')
            elif argument == '--label':
                if not re.fullmatch(r'tracefix\.run=[a-zA-Z0-9_-]{3,100}', value):
                    raise BrokerRejected('invalid runtime label')
            elif argument == '--user':
                if value not in {'1000:1000', 'pwuser'}:
                    raise BrokerRejected('container user must be non-root')
            elif argument == '--pids-limit':
                if value not in {'128', '256'}:
                    raise BrokerRejected('invalid pid limit')
            elif argument == '--memory':
                if value != '1g':
                    raise BrokerRejected('invalid memory limit')
            elif argument == '--cpus':
                if value != '2':
                    raise BrokerRejected('invalid cpu limit')
            elif argument == '--tmpfs':
                if not re.fullmatch(r'/(?:tmp|app/dist|workspace/dist):rw,nosuid,size=(?:128|512)m,mode=1777', value):
                    raise BrokerRejected('unsupported tmpfs')
            elif argument == '-e':
                if not re.fullmatch(r'(?:HOME|npm_config_cache|NODE_PATH|TRACEFIX_SOURCE)=[^\x00\r\n]{1,200}', value):
                    raise BrokerRejected('unsupported container environment')
            elif argument == '--workdir':
                if value != '/workspace':
                    raise BrokerRejected('unsupported working directory')
            elif argument == '--entrypoint':
                if not value or value.startswith('-') or len(value) > 512:
                    raise BrokerRejected('invalid container entrypoint')
            output.extend([argument, value])
            index += 2
        required = {'--name', '--user', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only'}
        if not required <= options.keys() or not ({'--network', '--network=none'} & options.keys()):
            raise BrokerRejected('required container restrictions missing')
        if '--network' in options and '--network=none' in options:
            raise BrokerRejected('conflicting network options')
        if index >= len(arguments) or arguments[index] not in {*self.images, *self.images.values()}:
            raise BrokerRejected('unregistered container image')
        image = self.images.get(arguments[index], arguments[index])
        output.extend([image, *arguments[index + 1:]])
        name = options['--name']
        self.containers[name] = image
        return output

    def validate(self, arguments):
        if (not isinstance(arguments, list) or not arguments or len(arguments) > 200
                or any(not isinstance(item, str) or '\x00' in item or len(item) > 500_000 for item in arguments)):
            raise BrokerRejected('invalid Docker request')
        with self.lock:
            if arguments[:2] == ['image', 'inspect']:
                if len(arguments) < 3 or any(image not in {*self.images, *self.images.values()} for image in arguments[2:]):
                    raise BrokerRejected('unregistered image inspection')
                return ['image', 'inspect', *(self.images.get(image, image) for image in arguments[2:])]
            if arguments[:2] == ['container', 'inspect']:
                if len(arguments) < 3 or any(name not in self.containers for name in arguments[2:]):
                    raise BrokerRejected('unregistered container inspection')
                return arguments
            if arguments[0] == 'run':
                return self._run_arguments(arguments[1:])
            if arguments[0] == 'exec' and len(arguments) >= 3:
                if arguments[1] not in self.containers or not arguments[2] or arguments[2].startswith('-'):
                    raise BrokerRejected('unregistered exec target')
                return arguments
            if arguments[0] == 'restart' and len(arguments) == 2 and arguments[1] in self.containers:
                return arguments
            if arguments[0] == 'rm':
                index = 1
                while index < len(arguments) and arguments[index] in {'-f', '-v'}:
                    index += 1
                if index == len(arguments) or any(name not in self.containers for name in arguments[index:]):
                    raise BrokerRejected('unregistered removal target')
                return arguments
            if arguments[:2] == ['network', 'rm'] and len(arguments) == 3 and arguments[2] in self.networks:
                return arguments
            if arguments[:2] == ['network', 'inspect'] and len(arguments) == 3 and arguments[2] in self.networks:
                return arguments
            raise BrokerRejected('Docker command is not supported')

    def _control(self, arguments):
        if arguments[:2] == ['network', 'create']:
            if (len(arguments) != 6 or arguments[2:4] != ['--internal', '--label']
                    or not re.fullmatch(r'tracefix\.run=[a-zA-Z0-9_-]{3,100}', arguments[4])):
                raise BrokerRejected('unsupported runtime network')
            name = self._owned_name(arguments[5])
            with self.lock:
                if name in self.networks:
                    raise BrokerRejected('runtime network already registered')
                self.networks.add(name)
            return [*arguments[:5], name]
        if arguments[:2] == ['volume', 'create']:
            if (len(arguments) != 5 or arguments[2] != '--label'
                    or not re.fullmatch(r'tracefix\.run=[a-zA-Z0-9_-]{3,100}', arguments[3])):
                raise BrokerRejected('unsupported dependency store')
            name = self._owned_name(arguments[4])
            if name in self.volumes:
                raise BrokerRejected('dependency store already registered')
            dependency_root = self.root / 'dependencies'
            dependency_root.mkdir(exist_ok=True)
            # 同名逻辑 volume 在恢复时可重建，不能复用前一次依赖内容。
            target = Path(tempfile.mkdtemp(prefix=name + '-', dir=dependency_root))
            target.chmod(0o777)
            self.volumes[name] = target
            image = self.images['app']
            seed_name = name + '-seed'
            self.containers[seed_name] = image
            return ['run', '--rm', '--name', seed_name, '--network=none', '--user', '1000:1000',
                    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--read-only',
                    '--pids-limit=128', '--memory=1g', '--cpus=2',
                    '--mount', mount_argument(target, '/eval-deps', False), '--entrypoint', 'node', image,
                    '-e', "require('fs').cpSync('/app/node_modules','/eval-deps',{recursive:true,dereference:true})"]
        if arguments[:2] == ['volume', 'rm']:
            if len(arguments) != 3 or arguments[2] not in self.volumes:
                raise BrokerRejected('unregistered dependency removal')
            self.volumes.pop(arguments[2])
            return None
        return self.validate(arguments)

    def start(self):
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop_event.wait(0.03):
            for directory in self.channel.iterdir():
                if not re.fullmatch(r'[a-f0-9]{32}', directory.name) or directory.name in self.seen:
                    continue
                try:
                    checked = plain_path(directory)
                    if not checked.is_relative_to(self.channel) or not (checked / 'request.json').is_file():
                        continue
                    self.seen.add(directory.name)
                    threading.Thread(target=self._execute, args=(checked,), daemon=True).start()
                except (OSError, BrokerRejected):
                    continue

    def _execute(self, directory):
        process = None
        watchdog = None
        translated = None
        completed = False
        try:
            request_file = plain_path(directory / 'request.json')
            if request_file.stat().st_size > 1_000_000:
                raise BrokerRejected('oversized broker request')
            request = json.loads(request_file.read_text(encoding='utf-8'))
            arguments = request['arguments']
            if type(request.get('stream')) is not bool:
                raise BrokerRejected('invalid broker stream flag')
            with self.lock:
                if self.stop_event.is_set():
                    raise BrokerRejected('broker is closed')
                translated = self._control(arguments)
                if translated is None:
                    self._write(directory, 'done.json', {'returncode': 0})
                    return
                process = subprocess.Popen([self.docker_executable, *translated], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.processes.add(process)
                self.audit.append({'arguments': translated, 'started_at': time.time()})
            # stdin 很快关闭时仍须执行超时；不能依赖输入转发线程看守。
            watchdog = threading.Timer(self.timeout, self._expire, args=(process,))
            watchdog.daemon = True
            watchdog.start()
            sender = threading.Thread(target=self._send_input, args=(directory, process), daemon=True)
            sender.start()
            output_index = 0
            output_bytes = 0
            while chunk := os.read(process.stdout.fileno(), 65_536):
                output_bytes += len(chunk)
                if output_bytes > 100_000_000:
                    process.kill()
                    raise BrokerRejected('broker output limit exceeded')
                path = directory / f'out-{output_index:08d}'
                self._write_bytes(path, chunk)
                output_index += 1
            returncode = process.wait(timeout=self.timeout)
            completed = True
            if returncode == 0:
                with self.lock:
                    if arguments[0] == 'rm':
                        for name in arguments[1:]:
                            self.containers.pop(name, None)
                    elif arguments[:2] == ['network', 'rm']:
                        self.networks.discard(arguments[2])
            self._write(directory, 'done.json', {'returncode': returncode, 'output_chunks': output_index})
        except Exception:
            if process and process.poll() is None:
                process.kill()
            self._write(directory, 'error.json', {'error': 'Docker broker rejected or failed request'})
        finally:
            if watchdog:
                watchdog.cancel()
            if process:
                with self.lock:
                    self.processes.discard(process)
                    if completed and translated[:1] == ['run'] and '--rm' in translated:
                        name = translated[translated.index('--name') + 1]
                        self.containers.pop(name, None)

    @staticmethod
    def _expire(process):
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _send_input(self, directory, process):
        index = 0
        deadline = time.monotonic() + self.timeout
        while process.poll() is None and not self.stop_event.is_set():
            if time.monotonic() >= deadline or (directory / 'cancel').is_file():
                process.kill()
                break
            path = directory / f'in-{index:08d}'
            try:
                if path.is_file():
                    checked = plain_path(path)
                    if checked.stat().st_size > 65_536:
                        process.kill()
                        break
                    process.stdin.write(checked.read_bytes())
                    process.stdin.flush()
                    index += 1
                    continue
                if (directory / 'stdin-closed').is_file():
                    process.stdin.close()
                    return
            except (OSError, BrokerRejected):
                return
            time.sleep(0.02)

    @staticmethod
    def _write_bytes(path, value):
        temporary = path.with_name(path.name + '.tmp')
        if temporary.is_symlink() or path.is_symlink():
            raise BrokerRejected('linked broker response')
        temporary.write_bytes(value)
        temporary.replace(path)

    def _write(self, directory, name, value):
        self._write_bytes(directory / name, json.dumps(value).encode())

    def close(self):
        with self.lock:
            self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.lock:
            processes = tuple(self.processes)
            containers = tuple(self.containers)
            networks = tuple(self.networks)
        for process in processes:
            if process.poll() is None:
                process.kill()
        for name in reversed(containers):
            subprocess.run([self.docker_executable, 'rm', '-f', name], capture_output=True, timeout=30)
        for name in networks:
            subprocess.run([self.docker_executable, 'network', 'rm', name], capture_output=True, timeout=30)
