import argparse
import asyncio
import json
import os
from pathlib import Path

from tracefix.agents.claude_sdk_adapter import ClaudeSdkAdapter, SdkExecutionRequest
from tracefix.execution.candidate_export import _snapshot


async def run_worker(request_path, result_path):
    if os.environ.get('TRACEFIX_SDK_WORKER') != '1':
        raise RuntimeError('SDK worker 必须由专用容器入口启动')
    allowed = {'PATH', 'HOME', 'CLAUDE_CONFIG_DIR', 'TRACEFIX_SDK_WORKER',
               'ANTHROPIC_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
               'PYTHONPATH', 'PYTHONUNBUFFERED', 'LANG'}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    os.environ.clear()
    os.environ.update(environment)
    request = SdkExecutionRequest.model_validate_json(Path(request_path).read_text(encoding='utf-8'))
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise ValueError('专用 worker 缺少受限模型凭据')
    if not os.environ.get('CLAUDE_CONFIG_DIR'):
        raise ValueError('专用 worker 缺少持久化 SDK 配置目录')
    adapter = ClaudeSdkAdapter()
    target = Path(result_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    def snapshot(name):
        value = _snapshot(request.workspace).model_dump()
        (target.parent / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        emit('sdk.workspace.input' if name == 'linux_input_manifest.json' else 'sdk.workspace.output', value)

    def emit(event, payload):
        print(json.dumps({'type': event, 'data': payload}, ensure_ascii=False, default=str), flush=True)

    snapshot('linux_input_manifest.json')
    try:
        result = await adapter.start(request, emit)
    finally:
        snapshot('linux_output_manifest.json')
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(result.model_dump_json(indent=2), encoding='utf-8')
    os.replace(temporary, target)
    return 0 if result.status == 'SUCCEEDED' else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    return asyncio.run(run_worker(args.request, args.result))


if __name__ == '__main__':
    raise SystemExit(main())
