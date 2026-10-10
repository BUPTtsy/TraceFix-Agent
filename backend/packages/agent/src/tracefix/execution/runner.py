"""受限 Docker 应用容器的生命周期和命令执行适配器。"""

import asyncio
import json
import os
import re

from tracefix.runtime.contracts import digest
from tracefix.execution.platforms import subprocess_options, terminate_tree, container_user, bind_mount


async def process(argv, timeout=120):
    """No shell. Arguments originate exclusively from audited host configuration."""
    p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT, **subprocess_options())
    try:
        output, _ = await asyncio.wait_for(p.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await terminate_tree(p)
        raise
    text = output.decode(errors='replace')
    return {'passed': p.returncode == 0, 'exit_code': p.returncode, 'output': text[-100_000:]}


class DockerRunner:
    """为单次运行创建隔离网络、应用容器和浏览器容器命令。"""

    def __init__(self, profile, workspace, run_id):
        """保存运行配置并派生稳定的容器和网络名称。"""
        self.profile, self.workspace, self.run_id = profile, workspace, run_id
        self.name = 'tf-' + run_id
        self.network = self.name + '-net'
        self.dependency_volume = self.name + '-node-modules'
        self.resolved_image_ids = None
        self._browser_command = None
        self._browser_image_index = None
        self._started = False
        self.actual_digest = ''

    async def docker(self, *args, check=True):
        """通过无 shell 的 Docker CLI 调用执行一条受控命令。"""
        result = await process(['docker', *args], self.profile.timeout_seconds)
        if check and not result['passed']:
            raise RuntimeError(f'Docker 命令执行失败（退出码 {result["exit_code"]}）：' + result['output'][-1500:])
        return result

    def _environment_digest(self, image_ids):
        return digest({'profile': self.profile.model_dump(), 'image_ids': image_ids})

    def _bind_image_ids(self, image_ids):
        self.resolved_image_ids = {'app': image_ids[0], 'browser': image_ids[1]}
        if self._browser_command is not None:
            # CLI 在异步准备前已持有此列表；原位更新使首次启动及后续重启都使用同一 ID。
            self._browser_command[self._browser_image_index] = image_ids[1]

    @staticmethod
    def _valid_image_id(image_id):
        return type(image_id) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) is not None

    async def _resolve_image_ids(self):
        """启动前只解析一次 tag，后续启动参数使用不可变镜像 ID。"""
        snapshot = self.workspace.require_repository_snapshot()
        self.workspace.check_frozen(snapshot)
        info = await self.docker('image', 'inspect', self.profile.image, self.profile.browser_image)
        images = json.loads(info['output'])
        if type(images) is not list or len(images) != 2 or any(type(image) is not dict for image in images):
            raise RuntimeError('镜像 inspect 返回结构无效')
        image_ids = [image.get('Id') for image in images]
        if any(not self._valid_image_id(image_id) for image_id in image_ids):
            raise RuntimeError('镜像 inspect 未返回完整的 app/browser 镜像 ID')
        self._bind_image_ids(image_ids)
        return self._environment_digest(image_ids)

    async def inspect_images(self, *, runtime=False):
        """启动前解析 tag，运行期核对实际容器，返回本次采样摘要。"""
        if not runtime and not self._started:
            if self.resolved_image_ids is None:
                self.actual_digest = await self._resolve_image_ids()
            else:
                self.actual_digest = self._environment_digest([
                    self.resolved_image_ids['app'], self.resolved_image_ids['browser']])
            return self.actual_digest
        # 即使 runner 刚在审批恢复时重建，最终门禁也不得回退到 tag 或旧缓存。
        self.actual_digest = ''
        info = await self.docker('container', 'inspect', self.name, self.name+'-browser')
        records = json.loads(info['output'])
        if type(records) is not list or len(records) != 2 or any(type(record) is not dict for record in records):
            raise RuntimeError('运行期 app/browser 容器 inspect 返回结构无效')
        image_ids = []
        for name, record in zip((self.name, self.name+'-browser'), records):
            labels = (record.get('Config') or {}).get('Labels') or {}
            running = (record.get('State') or {}).get('Running') is True
            networks = (record.get('NetworkSettings') or {}).get('Networks') or {}
            if (not self._valid_image_id(record.get('Image')) or not running
                    or record.get('Name') != '/'+name
                    or type(record.get('Id')) is not str or not record['Id']
                    or labels.get('tracefix.run') != self.run_id
                    or self.network not in networks
                    or (record.get('HostConfig') or {}).get('NetworkMode') != self.network):
                raise RuntimeError(f'运行期 {name} 容器身份或状态不匹配')
            image_ids.append(record['Image'])
        if self.resolved_image_ids is None:
            self._bind_image_ids(image_ids)
        self._started = True
        self.actual_digest = self._environment_digest(image_ids)
        return self.actual_digest

    def prepare_mountpoints(self):
        """只核对 export 已冻结的空挂载目录，不在启动时补建。"""
        self.workspace.prepare_mountpoints()

    async def start(self, source_manifest):
        """创建内部网络和只读应用容器，然后执行健康检查。"""
        snapshot = self.workspace.require_repository_snapshot()
        if digest(snapshot) != source_manifest:
            raise PermissionError('沙箱请求与绑定仓库快照摘要不一致')
        self.prepare_mountpoints()
        self.workspace.check_frozen(snapshot)
        if self.resolved_image_ids is None:
            await self._resolve_image_ids()
        self._started = True
        await self.docker('network', 'create', '--internal', '--label', 'tracefix.run='+self.run_id, self.network)
        await self.docker('volume', 'create', '--label', 'tracefix.run='+self.run_id, self.dependency_volume)
        await self.docker('run', '-d', '--name', self.name, '--network', self.network, '--network-alias', 'app',
            '--label', 'tracefix.run='+self.run_id, '--user', container_user(),
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=1g', '--cpus=2',
            '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=128m,mode=1777',
            '--tmpfs', '/app/dist:rw,nosuid,size=128m,mode=1777',
            '--mount', bind_mount(self.workspace.root),
            '--mount', 'type=volume,src='+self.dependency_volume+',dst=/app/node_modules,readonly',
            '-e', 'NODE_PATH=/deps/node_modules', '-e', 'TRACEFIX_SOURCE='+source_manifest,
            self.resolved_image_ids['app'], *self.profile.commands['start'])
        return await self.health()

    async def command(self, name):
        """在应用容器内执行配置中的非启动命令。"""
        if name not in self.profile.commands or name == 'start':
            raise PermissionError("未知的命令模板")
        result = await self.docker('exec', self.name, *self.profile.commands[name], check=False)
        return result

    async def health(self):
        """从容器内部轮询健康端点，验证应用可达性。"""
        url = f'http://127.0.0.1:{self.profile.port}{self.profile.health_path}'
        # Static host-owned program; the URL is passed as an argv value.
        code = "let ok=false;for(let i=0;i<30;i++){try{const r=await fetch(process.argv[1]);if(r.ok){ok=true;break}}catch{}await new Promise(r=>setTimeout(r,500))}if(!ok)process.exit(1);console.log('健康检查通过')"
        return await self.docker('exec', self.name, 'node', '--input-type=module', '-e', code, url, check=False)

    async def version(self):
        """读取应用报告的源清单，用于确认挂载源码版本。"""
        url = f'http://127.0.0.1:{self.profile.port}{self.profile.version_path}'
        r = await self.docker('exec', self.name, 'node', '--input-type=module', '-e',
            "console.log(await (await fetch(process.argv[1])).text())", url)
        return json.loads(r['output'])['source_manifest']

    async def rebuild(self):
        """执行构建并重启服务，使补丁后的进程重新加载。"""
        result = await self.command('build')
        if result['passed']:
            # A new server process is required after a server-side patch.
            await self.docker('restart', self.name)
            result['health'] = await self.health()
            result['passed'] = result['passed'] and result['health']['passed']
        return result

    async def close(self):
        """幂等地清理应用、浏览器容器和专用网络。"""
        try:
            await self.docker('rm', '-f', '-v', self.name, check=False)
            await self.docker('rm', '-f', self.name+'-browser', check=False)
            await self.docker('volume', 'rm', self.dependency_volume, check=False)
            await self.docker('network', 'rm', self.network, check=False)
        finally:
            # continuation 可复用固定镜像 ID 重新启动；最终 runtime=True 仍须读取真实容器。
            self._started = False
            self.actual_digest = ''

    def browser_command(self):
        """组装使用同一内部网络的无特权浏览器容器命令。"""
        if self._browser_command is not None:
            return self._browser_command
        command = ['docker', 'run', '--rm', '-i', '--init', '--network', self.network,
                '--name', self.name+'-browser', '--label', 'tracefix.run='+self.run_id,
                '--user', 'pwuser', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=1g', '--cpus=2',
                '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777']
        self._browser_image_index = len(command)
        # 未解析时保留空参数而非 tag，禁止绕过准备流程提前启动浏览器。
        image_id = self.resolved_image_ids['browser'] if self.resolved_image_ids else ''
        command += [image_id, '--headless', '--isolated', '--no-sandbox',
                '--browser', 'chromium', '--viewport-size', '1280x800', '--image-responses', 'allow',
                '--snapshot-mode', 'full', '--block-service-workers',
                '--allowed-origins', ';'.join(self.profile.allowed_origins)]
        self._browser_command = command
        return command
