"""interrupt 场景：在真实 Bash 工具执行期间经生产 Runner 发出 SDK interrupt，并由 Runner 确认写入者停止。

流程：
1. 生产 coder 工具集；prompt 要求模型用一次 Bash 调用（前台、不得 run_in_background）
   先派生后台子进程持续写 bg.txt，再在前台循环写 ticks.txt；
2. on_event 观察到 Bash tool_use 后启动监视任务：宿主 ticks.txt 出现并增长、且该 tool_use 尚无
   tool_result 时，用 asyncio.create_task 并发调用 runner.interrupt(...)（不阻塞事件读取）；
3. Runner.interrupt 发送 docker kill --signal SIGUSR1 → worker → adapter.interrupt → client.interrupt；
   宽限期内容器未退出时 Runner 才会 docker stop / kill。

判定（全部满足才 passed）：
- interrupt 在 Bash 工具执行中发出（发出时 ticks 已增长、该 tool_use 尚无 tool_result）；
- StopReceipt.interrupt_sent 与 writers_stopped 为真，control/stop_receipt.json 存在；
- start() 返回 STOPPED；
- interrupt 返回后宿主 ticks.txt 与 bg.txt 在 ≥2 秒内不再增长；
- docker container inspect 显示 Running=false；
- 停止路径为 SDK interrupt：worker 写回 result.json 且 error_code=SDK_INTERRUPTED，
  并且容器在 Runner 的 SIGUSR1 宽限期内退出（未进入 docker stop/kill 阶段）。
  若只是被 docker stop/kill 强制终止，判 failed。
"""

from __future__ import annotations

import asyncio
import json
import time

from sdk_live.common import (build_request, messages, result_summary, run_execution, runner_for,
                             sdk_workspace, tool_calls, tool_results)

STOP_REQUEST_ID = 'probe-interrupt'
# Runner 等待 SIGUSR1 生效的宽限期；须大于 worker 内 adapter 的 stop_grace_seconds（默认 15）
# 加上 CLI interrupt/disconnect 的耗时，否则 Runner 会过早进入 docker stop 阶段而无法区分停止路径。
RUNNER_GRACE_SECONDS = 45
# 观察到 Bash tool_use 后，最多等待 ticks.txt 开始增长的时间。
TICK_WAIT_SECONDS = 60
# interrupt 返回后观察文件是否继续增长的时长。
QUIET_SECONDS = 3

COMMAND = ('(while true; do echo bg >> bg.txt; sleep 0.5; done) & '
           'for i in $(seq 1 200); do echo tick >> ticks.txt; sleep 0.5; done; echo loop-done')
PROMPT = ('这是一次中断能力探针。只做一件事：调用一次 Bash 工具，在前台执行下面这条命令'
    '（不要设置 run_in_background，不要改写命令，不要拆分成多次调用，timeout 设为 600000）：\n'
    f'{COMMAND}\n'
    '命令会运行较长时间，这是预期行为，请等待它结束。命令结束后在结构化结果的 summary 中写明 Bash 输出，'
    'unresolved 为空数组。')


def _size(path):
    """宿主文件字节数；不存在时为 0。"""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _short(value, limit=300):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


async def run(ctx) -> dict:
    execution_id, workspace = sdk_workspace(ctx)
    request = build_request(ctx, execution_id, workspace, PROMPT)
    runner = runner_for(ctx, workspace)
    ticks, bg = workspace.source / 'ticks.txt', workspace.source / 'bg.txt'
    container = 'tf-sdk-' + execution_id.lower()

    state = {'bash_id': None, 'bash_command': None, 'monitor': None, 'interrupt_task': None,
             'result_ids': set(), 'send': None, 'receipt': None, 'interrupt_error': None,
             'interrupt_elapsed': None, 'after_return': None}

    async def send_interrupt():
        """等待 Bash 真正开始写入后并发发出 interrupt，并记录发出时刻的证据。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TICK_WAIT_SECONDS
        first = None
        while loop.time() < deadline:
            size = _size(ticks)
            if size and first is None:
                first = size
            elif first is not None and size > first:
                break
            await asyncio.sleep(0.2)
        state['send'] = {'ticks_bytes': _size(ticks), 'bg_bytes': _size(bg),
            'ticks_growing': first is not None and _size(ticks) > first,
            'bash_result_seen': state['bash_id'] in state['result_ids'],
            'at': time.time()}
        started = loop.time()
        try:
            receipt = await runner.interrupt(execution_id, STOP_REQUEST_ID, grace_seconds=RUNNER_GRACE_SECONDS)
            state['receipt'] = receipt.model_dump()
        except Exception as error:  # 如实记录，不吞掉为通过
            state['interrupt_error'] = f'{type(error).__name__}: {str(error)[:300]}'
        state['interrupt_elapsed'] = round(loop.time() - started, 2)
        # interrupt 返回后立即采样，随后观察 QUIET_SECONDS 秒是否继续增长。
        first_sample = {'ticks_bytes': _size(ticks), 'bg_bytes': _size(bg)}
        await asyncio.sleep(QUIET_SECONDS)
        state['after_return'] = {'immediately': first_sample,
            f'after_{QUIET_SECONDS}s': {'ticks_bytes': _size(ticks), 'bg_bytes': _size(bg)}}

    def on_event(kind, data):
        if kind != 'sdk.message':
            return
        envelope = data or {}
        payload = envelope.get('payload') or {}
        content = payload.get('content') if isinstance(payload.get('content'), list) else []
        if envelope.get('type') == 'UserMessage':
            state['result_ids'].update(block['tool_use_id'] for block in content
                                       if isinstance(block, dict) and block.get('tool_use_id'))
        if envelope.get('type') != 'AssistantMessage' or state['bash_id']:
            return
        for block in content:
            if isinstance(block, dict) and block.get('name') == 'Bash' and 'id' in block:
                state['bash_id'] = block['id']
                state['bash_command'] = _short((block.get('input') or {}).get('command', ''))
                # 不得在事件回调里 await interrupt，否则会阻塞 Runner 的 stdout 读取。
                state['interrupt_task'] = asyncio.create_task(send_interrupt())
                break

    start_error = None
    result = None
    try:
        result, events, runner = await run_execution(ctx, workspace, request, on_event=on_event, runner=runner)
    except Exception as error:
        start_error = f'{type(error).__name__}: {str(error)[:300]}'
        events = []
    if state['interrupt_task'] is not None:
        await asyncio.gather(state['interrupt_task'], return_exceptions=True)

    # 容器最终状态（Runner 生产 inspect 路径，含 execution 标签校验）。
    record = await runner._inspect_container(execution_id)
    container_state = (record or {}).get('State') or {}
    container_info = {'name': container, 'exists': record is not None,
        'running': container_state.get('Running'), 'exit_code': container_state.get('ExitCode'),
        'oom_killed': container_state.get('OOMKilled'), 'started_at': container_state.get('StartedAt'),
        'finished_at': container_state.get('FinishedAt')}

    # worker 是否写回 result.json（SIGUSR1 → SDK interrupt 路径才会正常写回）。
    result_path = workspace.execution_dir / 'state' / 'result.json'
    worker_result = None
    if result_path.is_file():
        try:
            raw = json.loads(result_path.read_text(encoding='utf-8'))
            worker_result = {'status': raw.get('status'), 'error_code': raw.get('error_code')}
        except (OSError, ValueError) as error:
            worker_result = {'invalid': type(error).__name__}
    receipt_path = workspace.execution_dir / 'control' / 'stop_receipt.json'
    receipt_file = None
    if receipt_path.is_file():
        receipt_file = json.loads(receipt_path.read_text(encoding='utf-8'))

    # transcript 中的 ResultMessage（CLI 终止原因）与 Bash 工具结果。
    terminal = [{'subtype': p.get('subtype'), 'is_error': p.get('is_error'),
                 'terminal_reason': p.get('terminal_reason'), 'num_turns': p.get('num_turns')}
                for p in messages(events, 'ResultMessage')]
    calls = tool_calls(events)
    results = tool_results(events)
    bash_calls = [{'id': c['id'], 'command': _short(c['input'].get('command', '')),
                   'run_in_background': c['input'].get('run_in_background'),
                   'result': None if c['id'] not in results else {
                       'is_error': results[c['id']]['is_error'],
                       'content': _short(results[c['id']]['content'], 200)}}
                  for c in calls if c['name'] == 'Bash']

    # 停止路径判定：容器在 SIGUSR1 宽限期内退出且 worker 写回 SDK_INTERRUPTED → sdk_interrupt；
    # 超出宽限期说明 Runner 进入了 docker stop/kill 阶段。
    elapsed = state['interrupt_elapsed']
    within_grace = elapsed is not None and elapsed < RUNNER_GRACE_SECONDS
    sdk_marked = bool(worker_result and worker_result.get('error_code') == 'SDK_INTERRUPTED')
    if within_grace and sdk_marked:
        stop_path = 'sdk_interrupt'
    elif elapsed is not None and elapsed >= RUNNER_GRACE_SECONDS * 2:
        stop_path = 'docker_kill'
    elif elapsed is not None and elapsed >= RUNNER_GRACE_SECONDS:
        stop_path = 'docker_stop'
    else:
        stop_path = 'unknown'

    send = state['send'] or {}
    after = state['after_return'] or {}
    quiet = after.get(f'after_{QUIET_SECONDS}s') or {}
    immediate = after.get('immediately') or {}
    receipt = state['receipt'] or {}
    checks = {
        'bash_tool_use_observed': state['bash_id'] is not None,
        'interrupt_during_tool_execution': bool(send.get('ticks_growing')) and not send.get('bash_result_seen'),
        'background_writer_started': send.get('bg_bytes', 0) > 0,
        'receipt_interrupt_sent': receipt.get('interrupt_sent') is True,
        'receipt_writers_stopped': receipt.get('writers_stopped') is True,
        'stop_receipt_file_exists': receipt_file is not None,
        'start_returned_stopped': result is not None and result.status == 'STOPPED',
        'ticks_quiet_after_return': bool(quiet) and quiet.get('ticks_bytes') == immediate.get('ticks_bytes'),
        'bg_quiet_after_return': bool(quiet) and quiet.get('bg_bytes') == immediate.get('bg_bytes'),
        'container_not_running': container_info['exists'] and container_info['running'] is False,
        'stopped_via_sdk_interrupt': stop_path == 'sdk_interrupt',
    }
    return {
        'status': 'passed' if all(checks.values()) else 'failed',
        'checks': checks,
        'stop_path': stop_path,
        'runner_grace_seconds': RUNNER_GRACE_SECONDS,
        'interrupt': {'sent_at_evidence': send, 'receipt': receipt or None, 'error': state['interrupt_error'],
                      'elapsed_seconds': elapsed, 'within_sigusr1_grace': within_grace},
        'bytes_after_return': after,
        'final_bytes': {'ticks': _size(ticks), 'bg': _size(bg)},
        'bash_calls': bash_calls,
        'terminal_messages': terminal,
        'worker_result_json': worker_result,
        'stop_receipt_file': receipt_file,
        'container': container_info,
        'start_error': start_error,
        'result': result_summary(result) if result is not None else None,
        'transcript_ref': str(workspace.execution_dir / 'transcript' / 'worker.jsonl'),
    }
