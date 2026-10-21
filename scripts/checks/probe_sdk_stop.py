"""无模型依赖的 Docker 后台写入和宿主进程树停止探针。"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from tracefix.execution.agent_runner import AgentExecutionRunner, _mount
from tracefix.execution.platforms import subprocess_options, terminate_tree


async def probe():
    root = Path(tempfile.mkdtemp(prefix='tracefix-stop-probe-'))
    (root / 'control').mkdir()
    report = {'directory': str(root), 'docker': {}, 'host': {}}
    runner = AgentExecutionRunner(SimpleNamespace(execution_dir=root))
    execution_id = 'probe-' + uuid4().hex
    name = 'tf-sdk-' + execution_id
    try:
        images = await runner._docker('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}')
        image = next((item for item in images.splitlines()
                      if 'tracefix-sdk-worker' in item or 'python' in item), '')
        if not image:
            image = 'python:3.12-alpine'
            await runner._docker('pull', image, timeout=120)
        child_program = ('from pathlib import Path\nimport time\np=Path("/probe/ticks")\n'
            'while True:\n with p.open("a") as stream: stream.write("tick\\n")\n time.sleep(.1)\n')
        program = ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
                   + repr(child_program) + ']); time.sleep(120)')
        await runner._docker('run', '-d', '--name', name, '--network', 'none',
            '--label', 'tracefix.execution=' + execution_id, '--mount', _mount(root, '/probe'),
            '--entrypoint', 'python', image, '-c', program)
        tick = root / 'ticks'
        for _attempt in range(100):
            if tick.exists() and tick.stat().st_size:
                break
            await asyncio.sleep(.1)
        if not tick.exists():
            raise RuntimeError('后台写入未启动')
        receipt = await runner.interrupt(execution_id, 'probe-stop', grace_seconds=.3)
        before = tick.stat().st_size
        await asyncio.sleep(.5)
        after = tick.stat().st_size
        inspection = await runner.inspect(execution_id)
        report['docker'] = {'passed': receipt.writers_stopped and before == after and not inspection.active,
            'receipt': receipt.model_dump(), 'bytes_before': before, 'bytes_after': after,
            'inspection': inspection.model_dump()}
    except Exception as error:
        report['docker'] = {'passed': False, 'error': str(error)}
    finally:
        await runner._docker('rm', '-f', name, check=False)
    host_tick = root / 'host-ticks'
    child = ('from pathlib import Path; import time; p=Path(' + repr(str(host_tick)) + '); '
        'exec("while True:\\n with p.open(\\\"a\\\") as f: f.write(\\\"tick\\\\n\\\")\\n time.sleep(.1)")')
    parent = ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'
        + repr(child) + ']); time.sleep(120)')
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', parent, **subprocess_options())
    try:
        for _attempt in range(100):
            if host_tick.exists() and host_tick.stat().st_size:
                break
            await asyncio.sleep(.1)
        await terminate_tree(process)
        before = host_tick.stat().st_size
        await asyncio.sleep(.5)
        after = host_tick.stat().st_size
        report['host'] = {'passed': before == after and process.returncode is not None,
            'platform': sys.platform, 'bytes_before': before, 'bytes_after': after}
    except Exception as error:
        report['host'] = {'passed': False, 'error': str(error)}
    finally:
        await terminate_tree(process)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    (root / 'report.json').write_text(encoded, encoding='utf-8')
    print(encoded)
    return 0 if all(report[key].get('passed') for key in ('docker', 'host')) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(probe()))
