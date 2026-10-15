from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

from tracefix.execution.runner import process
from tracefix.runtime.contracts import TestSpec as RuntimeTestSpec, digest
from tracefix.runtime.playwright_acceptance import PlaywrightAcceptanceRunner


async def run(repository: Path, output: Path) -> dict:
    run_id = 'acceptance-transport-' + uuid.uuid4().hex[:12]
    network = 'tf-' + run_id + '-net'
    name = 'tf-' + run_id
    checkout = output.parent / (output.name + '-checkout')
    checkout.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repository, checkout, ignore=shutil.ignore_patterns('.git', 'node_modules', 'dist'))
    (checkout / 'dist').mkdir()
    async def docker(*arguments, check=True):
        result = await process(['docker', *arguments])
        if check and not result['passed']:
            raise RuntimeError(result['output'])
        return result
    image = json.loads((await docker('image', 'inspect', 'tracefix-browser:1.0'))['output'])[0]['Id']
    async def command(kind):
        if kind != 'reset':
            raise ValueError('仅允许 fixture reset 命令')
        return await docker('exec', name, 'node', 'scripts/reset.mjs', check=False)
    runner = SimpleNamespace(run_id=run_id, network=network, resolved_image_ids={'browser': image}, docker=docker, command=command)
    await docker('network', 'create', '--internal', '--label', 'tracefix.run=' + run_id, network)
    try:
        await docker('run', '-d', '--name', name, '--network', network, '--network-alias', 'app',
            '--label', 'tracefix.run=' + run_id, '--mount', 'type=bind,src=' + checkout.resolve().as_posix() + ',dst=/source,readonly',
            '--workdir', '/source', '--user', '1000:1000', '--tmpfs', '/source/dist:rw,size=128m,mode=1777',
            '--tmpfs', '/tmp:rw,size=128m,mode=1777', '-e', 'NODE_PATH=/app/node_modules',
            'tracefix-bugboard:1.0', 'node', 'scripts/serve.mjs')
        health = await docker('exec', name, 'node', '--input-type=module', '-e',
            "for(let i=0;i<30;i++){try{const r=await fetch('http://127.0.0.1:3000/health');if(r.ok)process.exit(0)}catch{}await new Promise(r=>setTimeout(r,500))}process.exit(1)", check=False)
        if not health['passed']:
            logs = await docker('logs', name, check=False)
            raise RuntimeError(health['output'] + '\n' + logs['output'])
        assertion = {'locator': {'role': 'heading', 'name': 'Task board'}, 'condition': 'visible'}
        spec = RuntimeTestSpec(goal='验证当前 Run 隔离网络内的确定性浏览器传输', assertions=[assertion],
            regression_assertions=[assertion], behavior_scenarios=[{'id': case_id,
                'steps': [{'action': {'kind': 'observe'}, 'assertions': [assertion]}]}
                for case_id in ('transport-heading', 'transport-reset')])
        return await PlaywrightAcceptanceRunner(docker_runner=runner).run(spec, url='http://app:3000',
            artifacts_dir=output, binding={'run_id': run_id, 'repo_id': 'alpha',
            'commit': 'transport-fixture', 'spec_hash': digest(spec)}, include_regression=False)
    finally:
        await docker('stop', name, check=False)
        await docker('network', 'rm', network, check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.repository, args.output))
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'pass' else 1 if result['status'] == 'fail' else 2)
