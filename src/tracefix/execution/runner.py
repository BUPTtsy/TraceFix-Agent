import asyncio
import json
import os

from tracefix.runtime.contracts import digest
from tracefix.execution.platforms import subprocess_options, terminate_tree, container_user, bind_mount, is_link


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
    def __init__(self, profile, workspace, run_id):
        self.profile, self.workspace, self.run_id = profile, workspace, run_id
        self.name = 'tf-' + run_id
        self.network = self.name + '-net'
        self.actual_digest = ''

    async def docker(self, *args, check=True):
        result = await process(['docker', *args], self.profile.timeout_seconds)
        if check and not result['passed']:
            raise RuntimeError(f'Docker 命令执行失败（退出码 {result["exit_code"]}）：' + result['output'][-1500:])
        return result

    async def inspect_images(self):
        info = await self.docker('image', 'inspect', self.profile.image, self.profile.browser_image)
        images = json.loads(info['output'])
        self.actual_digest = digest({'profile': self.profile.model_dump(), 'image_ids': [i['Id'] for i in images]})
        return self.actual_digest

    def prepare_mountpoints(self):
        for name in ('dist', 'node_modules'):
            path = self.workspace.root / name
            if is_link(path) or (path.exists() and not path.is_dir()):
                raise PermissionError(f'容器挂载点不是普通目录：{name}')
            path.mkdir(exist_ok=True)

    async def start(self, source_manifest):
        self.prepare_mountpoints()
        await self.docker('network', 'create', '--internal', '--label', 'tracefix.run='+self.run_id, self.network)
        await self.docker('run', '-d', '--name', self.name, '--network', self.network, '--network-alias', 'app',
            '--label', 'tracefix.run='+self.run_id, '--user', container_user(),
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=128', '--memory=1g', '--cpus=2',
            '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=128m,mode=1777',
            '--tmpfs', '/app/dist:rw,nosuid,size=128m,mode=1777',
            '--mount', bind_mount(self.workspace.root),
            '--mount', 'type=volume,dst=/app/node_modules,readonly',
            '-e', 'NODE_PATH=/deps/node_modules', '-e', 'TRACEFIX_SOURCE='+source_manifest,
            self.profile.image, *self.profile.commands['start'])
        return await self.health()

    async def command(self, name):
        if name not in self.profile.commands or name == 'start':
            raise PermissionError("未知的命令模板")
        result = await self.docker('exec', self.name, *self.profile.commands[name], check=False)
        return result

    async def health(self):
        url = f'http://127.0.0.1:{self.profile.port}{self.profile.health_path}'
        # Static host-owned program; the URL is passed as an argv value.
        code = "let ok=false;for(let i=0;i<30;i++){try{const r=await fetch(process.argv[1]);if(r.ok){ok=true;break}}catch{}await new Promise(r=>setTimeout(r,500))}if(!ok)process.exit(1);console.log('健康检查通过')"
        return await self.docker('exec', self.name, 'node', '--input-type=module', '-e', code, url, check=False)

    async def version(self):
        url = f'http://127.0.0.1:{self.profile.port}{self.profile.version_path}'
        r = await self.docker('exec', self.name, 'node', '--input-type=module', '-e',
            "console.log(await (await fetch(process.argv[1])).text())", url)
        return json.loads(r['output'])['source_manifest']

    async def rebuild(self):
        result = await self.command('build')
        if result['passed']:
            # A new server process is required after a server-side patch.
            await self.docker('restart', self.name)
            result['health'] = await self.health()
            result['passed'] = result['passed'] and result['health']['passed']
        return result

    async def close(self):
        await self.docker('rm', '-f', '-v', self.name, check=False)
        await self.docker('rm', '-f', self.name+'-browser', check=False)
        await self.docker('network', 'rm', self.network, check=False)

    def browser_command(self):
        return ['docker', 'run', '--rm', '-i', '--init', '--network', self.network,
                '--name', self.name+'-browser', '--user', 'pwuser', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=1g', '--cpus=2',
                '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m,mode=1777',
                self.profile.browser_image, '--headless', '--isolated', '--no-sandbox',
                '--browser', 'chromium', '--viewport-size', '1280x800', '--image-responses', 'allow',
                '--snapshot-mode', 'full', '--block-service-workers',
                '--allowed-origins', ';'.join(self.profile.allowed_origins)]
