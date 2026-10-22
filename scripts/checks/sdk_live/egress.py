"""为本地真实 SDK 探针创建受控出口：带策略标签的 internal 网络 + CONNECT 白名单代理容器。

仅用于开发机验证；生产部署须使用经过审计的出口代理。用法：
    python scripts/checks/sdk_live/egress.py --allow https://api.deepseek.com [--network NAME] [--proxy NAME]
输出 JSON：{"network": ..., "egress_proxy": "http://<proxy>:3128", "policy": ...}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'backend/packages/agent/src'))

from tracefix.runtime.contracts import digest  # noqa: E402

HERE = Path(__file__).resolve().parent


def docker(*arguments, check=True):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True, encoding='utf-8')
    if check and result.returncode:
        raise RuntimeError(f'docker {arguments[0]} 失败: {result.stderr.strip()[:300]}')
    return result


def ensure_egress(allowlist, *, network='tf-sdk-egress', proxy='tf-sdk-egress-proxy', image):
    """幂等创建；已有网络的策略与白名单不一致时拒绝，避免静默改写他人环境。"""
    policy = digest(json.dumps(sorted(allowlist)).encode())
    inspected = docker('network', 'inspect', network, check=False)
    if inspected.returncode == 0:
        record = json.loads(inspected.stdout)[0]
        labels = record.get('Labels') or {}
        if (not record.get('Internal') or labels.get('tracefix.egress.enforced') != 'true'
                or labels.get('tracefix.egress.policy') != policy):
            raise RuntimeError(f'已存在的 {network} 与当前白名单不一致，请换 --network 名称或人工确认后删除')
    else:
        docker('network', 'create', '--internal', '--label', 'tracefix.egress.enforced=true',
               '--label', 'tracefix.egress.policy=' + policy, network)
    state = docker('container', 'inspect', proxy, '--format', '{{.State.Running}}', check=False)
    if state.returncode:
        arguments = ['run', '-d', '--name', proxy, '--network', 'bridge', '--read-only', '--cap-drop=ALL',
                     '--security-opt=no-new-privileges', '--user', '1000:1000',
                     '--label', 'tracefix.egress.policy=' + policy,
                     '--mount', f'type=bind,src={HERE.as_posix()},dst=/proxy,readonly',
                     '--entrypoint', '/usr/local/bin/python', image, '-u', '/proxy/egress_proxy.py']
        for origin in allowlist:
            arguments += ['--allow', origin]
        docker(*arguments)
        docker('network', 'connect', network, proxy)
    else:
        labels = json.loads(docker('container', 'inspect', proxy, '--format', '{{json .Config.Labels}}').stdout)
        if (labels or {}).get('tracefix.egress.policy') not in {None, policy}:
            raise RuntimeError(f'已存在的代理 {proxy} 放行列表与当前白名单不一致')
        if state.stdout.strip() != 'true':
            docker('start', proxy)
    return {'network': network, 'egress_proxy': f'http://{proxy}:3128', 'policy': policy}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow', action='append', required=True)
    parser.add_argument('--network', default='tf-sdk-egress')
    parser.add_argument('--proxy', default='tf-sdk-egress-proxy')
    parser.add_argument('--image', required=True, help='worker 镜像完整 ID，复用其 Python 运行代理')
    args = parser.parse_args()
    print(json.dumps(ensure_egress(args.allow, network=args.network, proxy=args.proxy, image=args.image)))


if __name__ == '__main__':
    main()
