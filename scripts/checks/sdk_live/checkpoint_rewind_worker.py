"""checkpoint_scope 第 2 部分：容器内直接驱动 ClaudeSDKClient，实测 rewind_files 的恢复范围。

仅由 checkpoint_scope.py 以只读挂载方式放入隔离容器运行，不属于生产链路：
- 选项完全由生产 ClaudeSdkAdapter._options(request) 构造（enable_file_checkpointing=True、
  setting_sources=[]、permission_mode='dontAsk'、_cli_environment 的 base_url/模型别名等），
  只额外追加 extra_args={'replay-user-messages': None}，以便拿到首条用户消息 uuid；
- 完成同一组三类修改后，先 dry_run 再真实调用 rewind_files 回到首条用户消息；
- 在 修改前 / 修改后 / rewind 后 三个时刻记录 Linux 视角完整快照，交给宿主比较。
stdout 每行输出 {'type': 'sdk.message', 'data': envelope}，结果写入 --result。
"""

import argparse
import asyncio
import dataclasses
import json
import os
from pathlib import Path

# 与生产 worker 相同：只保留受控环境变量，避免宿主或镜像中其他变量影响 CLI。
ALLOWED_ENV = {'PATH', 'HOME', 'CLAUDE_CONFIG_DIR', 'TRACEFIX_SDK_WORKER', 'ANTHROPIC_API_KEY',
               'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'LANG'}
RESPONSE_TIMEOUT = 330


def _is_prompt_message(payload):
    """首条用户消息是回放的 prompt，而不是 tool_result 消息。"""
    content = payload.get('content')
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(block, dict) and block.get('tool_use_id') for block in content)
    return False


async def run(request_path, result_path):
    environment = {key: value for key, value in os.environ.items() if key in ALLOWED_ENV}
    os.environ.clear()
    os.environ.update(environment)
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise ValueError('rewind 探针缺少受限模型凭据')

    from tracefix.agents.claude_sdk_adapter import (ClaudeSdkAdapter, SdkExecutionRequest,
                                                    _message_value, _redact)
    from tracefix.execution.candidate_export import _snapshot

    request = SdkExecutionRequest.model_validate_json(Path(request_path).read_text(encoding='utf-8'))
    adapter = ClaudeSdkAdapter()
    sdk = adapter._bindings()  # 同时校验 SDK/CLI 冻结版本
    options = adapter._options(request)
    options = dataclasses.replace(options, enable_file_checkpointing=True,
        extra_args={**(options.extra_args or {}), 'replay-user-messages': None})
    target = Path(result_path)
    transcript = target.parent / 'rewind-transcript.jsonl'
    workspace = Path(request.workspace)
    report = {'options': {'enable_file_checkpointing': options.enable_file_checkpointing,
                          'extra_args': options.extra_args, 'setting_sources': options.setting_sources,
                          'permission_mode': options.permission_mode, 'tools': options.tools,
                          'model': options.model, 'env_keys': sorted((options.env or {}).keys()),
                          'base_url': (options.env or {}).get('ANTHROPIC_BASE_URL')},
              'stages': {'before': _snapshot(workspace).model_dump()['entries']},
              'user_messages': [], 'checkpoint_uuid': None}

    def emit(envelope):
        line = json.dumps({'type': 'sdk.message', 'data': envelope}, ensure_ascii=False, default=str)
        print(line, flush=True)
        with transcript.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')

    client = sdk.ClaudeSDKClient(options=options)
    try:
        await client.connect()
        await client.query(request.prompt)

        async def consume():
            terminal = None
            async for message in client.receive_response():
                payload = _redact(_message_value(message))
                kind = type(message).__name__
                emit({'type': kind, 'payload': payload})
                if kind == 'UserMessage':
                    item = {'uuid': getattr(message, 'uuid', None),
                            'prompt': _is_prompt_message(payload),
                            'parent': payload.get('parent_tool_use_id')}
                    report['user_messages'].append(item)
                    if report['checkpoint_uuid'] is None and item['uuid'] and item['prompt'] \
                            and item['parent'] is None:
                        report['checkpoint_uuid'] = item['uuid']
                if kind == 'ResultMessage':
                    terminal = {'subtype': payload.get('subtype'), 'is_error': payload.get('is_error'),
                                'num_turns': payload.get('num_turns'),
                                'structured_output': payload.get('structured_output')}
            return terminal

        report['terminal'] = await asyncio.wait_for(consume(), RESPONSE_TIMEOUT)
        report['stages']['after_changes'] = _snapshot(workspace).model_dump()['entries']
        checkpoint = report['checkpoint_uuid']
        if not checkpoint:
            report['rewind_error'] = '未收到带 uuid 的首条用户消息，无法调用 rewind_files'
        else:
            # client.rewind_files 内部即调用此控制请求但丢弃响应；这里直接发送以保留 canRewind/filesChanged。
            query = client._query
            try:
                report['dry_run'] = await query._send_control_request(
                    {'subtype': 'rewind_files', 'user_message_id': checkpoint, 'dry_run': True})
            except Exception as error:
                report['dry_run_error'] = f'{type(error).__name__}: {str(error)[:300]}'
            try:
                report['rewind'] = await query._send_control_request(
                    {'subtype': 'rewind_files', 'user_message_id': checkpoint})
            except Exception as error:
                report['rewind_error'] = f'{type(error).__name__}: {str(error)[:300]}'
            report['stages']['after_rewind'] = _snapshot(workspace).model_dump()['entries']
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {str(error)[:500]}'
    finally:
        try:
            await client.disconnect()
        except Exception as error:
            report['disconnect_error'] = type(error).__name__
    report = _redact(report)
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, target)
    return 0 if 'after_rewind' in report['stages'] and 'rewind_error' not in report else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--result', required=True)
    args = parser.parse_args()
    return asyncio.run(run(args.request, args.result))


if __name__ == '__main__':
    raise SystemExit(main())
