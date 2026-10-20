"""仅通过受控 Docker worker 启动 Claude SDK 执行。"""

from __future__ import annotations

import asyncio
import csv
import inspect
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from tracefix.execution.candidate_export import SDKWorkspace
from tracefix.execution.platforms import container_user, is_link, subprocess_options, terminate_tree
from tracefix.runtime.contracts import digest


def _mount(path: Path, target: str, *, readonly=False):
    output = io.StringIO()
    options = ['type=bind', 'src=' + path.resolve().as_posix(), 'dst=' + target]
    if readonly:
        options.append('readonly')
    csv.writer(output, lineterminator='').writerow(options)
    return output.getvalue()


class AgentExecutionRunner:
    def __init__(self, workspace: SDKWorkspace, *, network: str = '', egress_proxy: str = '',
                 credentials=None):
        self.workspace = workspace
        self.network = network
        self.egress_proxy = egress_proxy
        self.credentials = credentials
        self.container_name = ''

    def _environment(self, request=None):
        permitted = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'DOCKER_HOST',
                     'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'DOCKER_CONFIG'}
        environment = {key: value for key, value in os.environ.items() if key.upper() in permitted}
        if request is not None:
            reference = request.config.credential_ref
            if not reference.startswith('env:') or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reference[4:]):
                raise PermissionError('SDK 凭据必须来自受控环境引用')
            secret = (self.credentials or os.environ).get(reference[4:], '')
            if not secret:
                raise PermissionError('SDK 模型凭据不可用')
            environment['ANTHROPIC_API_KEY'] = secret
        return environment

    async def _docker(self, *arguments, environment=None, timeout=30):
        process = await asyncio.create_subprocess_exec('docker', *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment or self._environment(), **subprocess_options())
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await terminate_tree(process)
            raise
        if process.returncode:
            raise RuntimeError('Docker worker 控制失败，退出码 ' + str(process.returncode))
        return stdout.decode('utf-8')

    async def _preflight(self, request):
        if not self.network or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', self.network):
            raise PermissionError('SDK worker 缺少受控出口网络')
        proxy = urlsplit(self.egress_proxy)
        if proxy.scheme not in {'http', 'https'} or not proxy.hostname or proxy.username or proxy.password:
            raise PermissionError('SDK worker 必须配置无内嵌凭据的受控出口代理')
        if not request.config.egress_allowlist:
            raise PermissionError('SDK worker 缺少冻结出口白名单')
        image = request.config.worker_image
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
            raise PermissionError('SDK worker 镜像必须冻结为完整 image ID')
        networks = json.loads(await self._docker('network', 'inspect', self.network))
        if len(networks) != 1:
            raise PermissionError('SDK 出口网络身份不唯一')
        network = networks[0]
        labels = network.get('Labels') or {}
        expected = digest(json.dumps(sorted(request.config.egress_allowlist)).encode())
        if (network.get('Internal') is not True or labels.get('tracefix.egress.enforced') != 'true'
                or labels.get('tracefix.egress.policy') != expected):
            raise PermissionError('SDK 出口网络未绑定冻结策略或允许直接联网')
        images = json.loads(await self._docker('image', 'inspect', image))
        if len(images) != 1 or images[0].get('Id') != image:
            raise PermissionError('SDK worker 镜像身份不匹配')
        versions = (images[0].get('Config') or {}).get('Labels') or {}
        if (versions.get('tracefix.sdk.version') != request.config.sdk_version
                or versions.get('tracefix.cli.version') != request.config.cli_version):
            raise PermissionError('SDK worker 版本标签与冻结配置不匹配')

    def build_command(self, request, workspace=None):
        workspace = workspace or self.workspace
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', request.execution_id):
            raise PermissionError('execution_id 不能用于容器身份')
        self.container_name = 'tf-sdk-' + request.execution_id.lower()
        return ['docker', 'run', '--name', self.container_name, '--network', self.network,
            '--label', 'tracefix.execution=' + request.execution_id,
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user', container_user(), '--pids-limit', '256', '--workdir', '/workspace',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
            '--mount', _mount(workspace.source, '/workspace'),
            '--mount', _mount(workspace.execution_dir / 'control', '/control', readonly=True),
            '--mount', _mount(workspace.execution_dir / 'state', '/state'),
            '--mount', _mount(workspace.config_dir or workspace.execution_dir / 'sdk-config', '/sdk-config'),
            '--env', 'ANTHROPIC_API_KEY',
            '--env', 'TRACEFIX_SDK_WORKER=1', '--env', 'HOME=/sdk-config',
            '--env', 'CLAUDE_CONFIG_DIR=/sdk-config', '--env', 'PYTHONUNBUFFERED=1',
            '--env', 'HTTP_PROXY=' + self.egress_proxy, '--env', 'HTTPS_PROXY=' + self.egress_proxy,
            '--entrypoint', '/usr/local/bin/python', request.config.worker_image,
            '-m', 'tracefix.agents.claude_sdk_worker', '--request', '/control/request.json',
            '--result', '/state/result.json']

    async def _capture_linux_manifest(self, request, stage):
        program = ('import json; from pathlib import Path; '
                   'from tracefix.execution.candidate_export import _snapshot; '
                   'print(json.dumps(_snapshot(Path("/workspace")).model_dump()))')
        output = await self._docker('run', '--rm', '--network', 'none', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', container_user(),
            '--mount', _mount(self.workspace.source, '/workspace', readonly=True),
            '--entrypoint', '/usr/local/bin/python', request.config.worker_image, '-c', program)
        manifest = json.loads(output)
        target = self.workspace.execution_dir / ('linux_' + stage + '_manifest.json')
        if target.exists():
            raise PermissionError('Linux 执行边界清单不允许覆盖')
        target.write_text(json.dumps(manifest), encoding='utf-8')
        return manifest

    async def start(self, request, emit):
        if request.execution_id != self.workspace.execution_dir.name:
            self.workspace = self.workspace.fork_execution(request.execution_id)
        await self._preflight(request)
        self.workspace.check_input()
        request_path = self.workspace.execution_dir / 'control' / 'request.json'
        if request_path.exists():
            raise PermissionError('此 execution 已有请求，必须使用恢复流程而不能重复启动')
        await self._capture_linux_manifest(request, 'input')
        payload = request.model_dump(mode='json')
        payload.update(workspace='/workspace', transcript_path='/state/transcript.jsonl')
        request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        argv = self.build_command(request)
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._environment(request), **subprocess_options())
        log_path = self.workspace.execution_dir / 'transcript' / 'worker.jsonl'

        async def events():
            with log_path.open('ab') as transcript:
                while line := await process.stdout.readline():
                    transcript.write(line)
                    transcript.flush()
                    try:
                        event = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if emit is not None:
                        response = emit(event.get('type', 'sdk.message'), event.get('data', event))
                        if inspect.isawaitable(response):
                            await response

        async def errors():
            with (self.workspace.execution_dir / 'transcript' / 'worker.stderr').open('ab') as output:
                while chunk := await process.stderr.read(65536):
                    output.write(chunk)

        readers = [asyncio.create_task(events()), asyncio.create_task(errors())]
        try:
            await asyncio.gather(process.wait(), *readers)
        except BaseException:
            await self._docker('stop', '--time', str(request.config.stop_grace_seconds), self.container_name)
            await terminate_tree(process)
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        finally:
            inspection = json.loads(await self._docker('container', 'inspect', self.container_name))
            if len(inspection) != 1 or inspection[0].get('State', {}).get('Running') is not False:
                raise RuntimeError('SDK worker 写入者尚未确认停止')
            await self._capture_linux_manifest(request, 'output')
            self.workspace.freeze()
        result_path = self.workspace.execution_dir / 'state' / 'result.json'
        if is_link(result_path) or not result_path.is_file() or result_path.stat().st_nlink != 1:
            raise RuntimeError('SDK worker 未产生结果；源码与 transcript 已保留')
        from tracefix.agents.claude_sdk_adapter import ExecutionResult
        result = ExecutionResult.model_validate_json(result_path.read_text(encoding='utf-8'))
        if result.execution_id != request.execution_id:
            raise PermissionError('SDK worker 结果 execution 身份不匹配')
        return result
