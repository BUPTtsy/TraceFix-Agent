import argparse
import asyncio
import json
import os
import signal
from pathlib import Path

from tracefix.agents.claude_sdk_adapter import ClaudeSdkAdapter, ExecutionResult, SdkExecutionRequest
from tracefix.execution.candidate_export import _snapshot

# 容器退出码区分结果类别；FAILED 与未捕获异常同为 1，权威结果仍以 result.json 为准。
EXIT_CODES = {'SUCCEEDED': 0, 'FAILED': 1, 'STOPPED': 3, 'UNKNOWN': 4}
# 宿主可能挂载到 /workspace 下的工具链路径（依赖只读卷、dist tmpfs）。
HOST_MOUNTPOINTS = ('node_modules', 'dist')
# 宿主 asyncio readline 默认上限 64KiB；兼容旧消费者时内联清单的上限，留足余量。
INLINE_ENTRIES_LIMIT = 32 * 1024


def snapshot_skip(workspace):
    """只跳过宿主挂载点；coder 自建的同名普通目录仍完整扫描。"""
    root = Path(workspace)
    return tuple(name for name in HOST_MOUNTPOINTS if os.path.ismount(root / name))


def write_snapshot(workspace, path):
    """完整清单写入 path，返回摘要事件负载，避免超长 stdout 行。

    兼容 sdk_live 检查脚本读取事件 entries：仅在未跳过且清单足够小时内联 entries。
    """
    skip = snapshot_skip(workspace)
    snapshot = _snapshot(Path(workspace), skip=skip)
    Path(path).write_text(json.dumps(snapshot.model_dump(), ensure_ascii=False), encoding='utf-8')
    summary = {'content_hash': snapshot.content_hash, 'entries_count': len(snapshot.entries),
               'skipped': list(skip)}
    if not skip and len(json.dumps(snapshot.entries, ensure_ascii=False)) <= INLINE_ENTRIES_LIMIT:
        summary['entries'] = snapshot.entries
    return summary


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
        value = write_snapshot(request.workspace, target.parent / name)
        emit('sdk.workspace.input' if name == 'linux_input_manifest.json' else 'sdk.workspace.output', value)

    def emit(event, payload):
        print(json.dumps({'type': event, 'data': payload}, ensure_ascii=False, default=str), flush=True)

    snapshot('linux_input_manifest.json')
    stop_requested = asyncio.Event()

    def request_stop(*_args):
        stop_requested.set()

    loop = asyncio.get_running_loop()
    installed_signals = []
    for signum in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None),
                   getattr(signal, 'SIGUSR1', None)):
        if signum is None:
            continue
        try:
            loop.add_signal_handler(signum, request_stop)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(signum, request_stop)
                installed_signals.append(signum)
            except (ValueError, OSError):
                pass
    task = asyncio.create_task(adapter.start(request, emit))
    result = None
    try:
        stop_task = asyncio.create_task(stop_requested.wait())
        done, _pending = await asyncio.wait((task, stop_task), return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not task.done():
            receipt = await adapter.interrupt(request.execution_id, 'signal-stop',
                request.config.stop_grace_seconds)
            if receipt.writers_stopped:
                result = await task
            else:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                result = ExecutionResult(execution_id=request.execution_id, status='UNKNOWN',
                    error_code='SDK_STOP_WRITERS_UNCONFIRMED')
        else:
            result = await task
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
    finally:
        for signum in installed_signals:
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                pass
        snapshot('linux_output_manifest.json')
    if result is None:
        result = ExecutionResult(execution_id=request.execution_id, status='UNKNOWN',
            error_code='SDK_RESULT_MISSING')
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(result.model_dump_json(indent=2), encoding='utf-8')
    os.replace(temporary, target)
    return EXIT_CODES[result.status]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    return asyncio.run(run_worker(args.request, args.result))


if __name__ == '__main__':
    raise SystemExit(main())
