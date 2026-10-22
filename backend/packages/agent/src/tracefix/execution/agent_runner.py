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
from tracefix.agents.claude_sdk_adapter import ExecutionInspection, ExecutionResult, SdkExecutionRequest, StopReceipt


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

    async def _docker(self, *arguments, environment=None, timeout=30, check=True):
        process = await asyncio.create_subprocess_exec('docker', *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=environment or self._environment(), **subprocess_options())
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await terminate_tree(process)
            raise
        if process.returncode and check:
            raise RuntimeError('Docker worker 控制失败，退出码 ' + str(process.returncode))
        return stdout.decode('utf-8')

    async def _inspect_container(self, execution_id):
        name = 'tf-sdk-' + execution_id.lower()
        output = await self._docker('container', 'inspect', name, check=False)
        if not output.strip():
            return None
        try:
            records = json.loads(output)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError('SDK worker 容器检查回执无效') from error
        if len(records) != 1:
            raise RuntimeError('SDK worker 容器身份不唯一')
        labels = (records[0].get('Config') or {}).get('Labels') or {}
        if labels.get('tracefix.execution') != execution_id:
            raise PermissionError('SDK worker 容器 execution 标签不匹配')
        expected_epoch = getattr(self, 'owner_epoch', None)
        actual_epoch = labels.get('tracefix.owner_epoch')
        if (expected_epoch is not None and actual_epoch is not None
                and (not str(actual_epoch).isdigit() or int(actual_epoch) > expected_epoch)):
            raise PermissionError('SDK worker 容器 owner epoch 晚于当前控制者')
        return records[0]

    async def _wait_stopped(self, execution_id, timeout):
        deadline = asyncio.get_running_loop().time() + max(0, timeout)
        while True:
            record = await self._inspect_container(execution_id)
            if record is None or (record.get('State') or {}).get('Running') is False:
                return record
            if asyncio.get_running_loop().time() >= deadline:
                return record
            await asyncio.sleep(0.1)

    async def inspect(self, execution_id):
        record = await self._inspect_container(execution_id)
        if record is None:
            return ExecutionInspection(execution_id=execution_id, status='UNKNOWN', active=False)
        state = record.get('State') or {}
        active = state.get('Running') is True
        result_path = self.workspace.execution_dir / 'state' / 'result.json'
        session_id = None
        if result_path.is_file() and not is_link(result_path):
            try:
                session_id = ExecutionResult.model_validate_json(
                    result_path.read_text(encoding='utf-8')).sdk_session_id
            except (OSError, ValueError):
                pass
        return ExecutionInspection(execution_id=execution_id,
            status='RUNNING' if active else 'STOPPED', sdk_session_id=session_id, active=active)

    async def interrupt(self, execution_id, stop_request_id, grace_seconds=15):
        record = await self._inspect_container(execution_id)
        if record is None:
            return StopReceipt(execution_id=execution_id, stop_request_id=stop_request_id,
                interrupt_sent=False, writers_stopped=False, status='UNKNOWN')
        state = record.get('State') or {}
        if state.get('Running') is not True:
            return StopReceipt(execution_id=execution_id, stop_request_id=stop_request_id,
                interrupt_sent=False, writers_stopped=True, status='STOPPED')
        name = 'tf-sdk-' + execution_id.lower()
        try:
            await self._docker('kill', '--signal', 'SIGUSR1', name)
            sent = True
        except RuntimeError:
            sent = False
        record = await self._wait_stopped(execution_id, grace_seconds)
        if record is not None and (record.get('State') or {}).get('Running') is True:
            await self._docker('stop', '--time', str(max(1, int(grace_seconds))), name, check=False)
            record = await self._wait_stopped(execution_id, grace_seconds)
        if record is not None and (record.get('State') or {}).get('Running') is True:
            await self._docker('kill', name, check=False)
            record = await self._wait_stopped(execution_id, 5)
        stopped = record is None or (record.get('State') or {}).get('Running') is False
        receipt = StopReceipt(execution_id=execution_id, stop_request_id=stop_request_id,
            interrupt_sent=sent, writers_stopped=stopped,
            status='STOPPED' if stopped else 'INTERRUPTING' if sent else 'UNKNOWN')
        if stopped:
            path = self.workspace.execution_dir / 'control' / 'stop_receipt.json'
            if path.parent.is_dir():
                with path.open('x', encoding='utf-8') as output:
                    output.write(receipt.model_dump_json())
        return receipt

    async def _archive_resume_outputs(self):
        """Preserve prior attempt artifacts before a resumed SDK execution."""
        execution_dir = self.workspace.execution_dir
        for name in ('state/result.json', 'linux_output_manifest.json', 'output_manifest.json',
                     'control/stop_receipt.json'):
            path = execution_dir / name
            if not path.exists() or is_link(path):
                continue
            suffix = 1
            while True:
                target = path.with_name(path.name + f'.attempt-{suffix}')
                if not target.exists():
                    path.replace(target)
                    break
                suffix += 1

    async def _remove_stopped_container(self, execution_id):
        record = await self._inspect_container(execution_id)
        if record is None:
            return
        if (record.get('State') or {}).get('Running') is True:
            raise RuntimeError('SDK worker 仍有活动写入者，禁止恢复')
        await self._docker('rm', 'tf-sdk-' + execution_id.lower())

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
        command = ['docker', 'run', '--name', self.container_name, '--network', self.network,
            '--label', 'tracefix.execution=' + request.execution_id,
            '--label', 'tracefix.workspace=' + digest(str(workspace.source.resolve()))]
        owner_epoch = getattr(self, 'owner_epoch', None)
        if owner_epoch is not None:
            command.extend(['--label', 'tracefix.owner_epoch=' + str(owner_epoch)])
        command.extend([
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
            '-m', 'tracefix.agents.claude_sdk_worker', '--request',
            '/control/' + getattr(self, '_request_name', 'request.json'),
            '--result', '/state/result.json'])
        return command

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

    async def start(self, request, emit, *, _resume=False):
        if not _resume and request.execution_id != self.workspace.execution_dir.name:
            self.workspace = self.workspace.fork_execution(request.execution_id)
        await self._preflight(request)
        self.workspace.check_input()
        writers = await self._docker('ps', '--filter',
            'label=tracefix.workspace=' + digest(str(self.workspace.source.resolve())),
            '--format', '{{.Names}}')
        if writers.strip():
            raise RuntimeError('SDK 工作区已有活动容器写入者')
        request_path = self.workspace.execution_dir / 'control' / 'request.json'
        if request_path.exists() and not _resume:
            raise PermissionError('此 execution 已有请求，必须使用恢复流程而不能重复启动')
        if not _resume:
            await self._capture_linux_manifest(request, 'input')
            payload = request.model_dump(mode='json')
            payload.update(workspace='/workspace', transcript_path='/state/transcript.jsonl')
            request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        else:
            attempt = 1
            while request_path.with_name(f'request-resume-{attempt}.json').exists():
                attempt += 1
            resume_path = request_path.with_name(f'request-resume-{attempt}.json')
            payload = request.model_dump(mode='json')
            payload.update(workspace='/workspace', transcript_path='/state/transcript.jsonl')
            with resume_path.open('x', encoding='utf-8') as output:
                json.dump(payload, output, ensure_ascii=False)
            self._request_name = resume_path.name
            await self._archive_resume_outputs()
            await self._remove_stopped_container(request.execution_id)
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
            stop_path = self.workspace.execution_dir / 'control' / 'stop_receipt.json'
            if stop_path.is_file() and not is_link(stop_path):
                receipt = StopReceipt.model_validate_json(stop_path.read_text(encoding='utf-8'))
                if receipt.execution_id == request.execution_id and receipt.writers_stopped:
                    return ExecutionResult(execution_id=request.execution_id, status='STOPPED',
                        sdk_session_id=request.sdk_session_id, error_code='SDK_CONTAINER_STOPPED',
                        transcript_ref=str(log_path))
            raise RuntimeError('SDK worker 未产生结果；源码与 transcript 已保留')
        result = ExecutionResult.model_validate_json(result_path.read_text(encoding='utf-8'))
        if result.execution_id != request.execution_id:
            raise PermissionError('SDK worker 结果 execution 身份不匹配')
        return result

    async def resume(self, execution_id, emit=None, *, sdk_session_id=None, rebuild=False):
        request_path = self.workspace.execution_dir / 'control' / 'request.json'
        if is_link(request_path) or not request_path.is_file():
            raise PermissionError('SDK 恢复缺少冻结 request.json')
        request = SdkExecutionRequest.model_validate_json(request_path.read_text(encoding='utf-8'))
        if request.execution_id != execution_id:
            raise PermissionError('SDK 恢复 execution 身份不匹配')
        inspection = await self.inspect(execution_id)
        if inspection.active:
            raise RuntimeError('SDK 恢复前检测到原 execution 仍在运行')
        if inspection.status == 'UNKNOWN':
            raise RuntimeError('SDK 恢复前无法确认原 execution 已停止')
        result_path = self.workspace.execution_dir / 'state' / 'result.json'
        if result_path.is_file() and not is_link(result_path) and not rebuild:
            prior = ExecutionResult.model_validate_json(result_path.read_text(encoding='utf-8'))
            if prior.sdk_session_id:
                request = request.model_copy(update={'sdk_session_id': prior.sdk_session_id})
        if sdk_session_id is not None:
            request = request.model_copy(update={'sdk_session_id': sdk_session_id})
        if rebuild:
            request = request.model_copy(update={'sdk_session_id': None})
        prompt = ('此前 session 已失效。保留的工作区是同一冻结任务的中断状态。'
            '先核对实际文件与已完成修改，仅继续未完成工作，不要重放初始操作。冻结目标：'
            + request.prompt if rebuild else '继续当前已冻结任务，从会话中断位置推进；不得重复已完成的操作。')
        request = request.model_copy(update={'workspace': self.workspace.source,
            'transcript_path': self.workspace.execution_dir / 'state' / 'transcript.jsonl',
            'prompt': prompt})
        return await self.start(request, emit, _resume=True)
