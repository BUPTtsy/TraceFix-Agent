"""跨进程续跑场景：首个 worker 容器中途退出后，用新宿主 Runner 加载同一配置目录与 SDK session 续跑。

证明方法：
- 第一轮 prompt 给出一个随机口令（只出现在 prompt 中），任务为 A) Edit 把 value = 1 改为 value = 2；
  B) Bash 前台等待 60 秒；C) 把口令写入 src/codeword.txt。宿主在 A 完成、B 进行中时经生产
  AgentExecutionRunner.interrupt 停止首个容器（容器 Exited = 首个 worker 进程结束）。
- 续跑 prompt 由生产代码固定，不含口令；宿主重新 SDKWorkspace.load 并构造新 Runner 调用 resume。
  若续跑后 src/codeword.txt 等于口令，且续跑轮未读取 /control、/state、/sdk-config 等含口令的旁路，
  则口令只能来自 CLI 从 /sdk-config 加载的会话历史，即会话上下文跨进程延续。

判定（全部满足才 passed）：
1. 第一轮确实在 A 完成、C 未执行时被停止：宿主 value = 2、codeword.txt 不存在、源码中无口令；
2. 两轮之间首个容器 State.Running=False（Exited），续跑产生 request-resume-1.json 且其 prompt 不含口令；
3. 续跑容器 Id 与首个容器不同；续跑请求冻结的 sdk_session_id 等于第一轮 session；
   续跑 init 的 session_id 与第一轮一致（不一致时记录，但需会话 jsonl 证明加载历史）；
4. 续跑后 src/codeword.txt 内容等于口令，src/app.py 仍为 value = 2，续跑轮无针对 app.py 的 Edit/Write
   （若有则必须是幂等的 value = 2 且记录）；
5. sdk-config 目录两轮相同，且含第一轮 session 的 jsonl，续跑后该文件增长；
6. 续跑轮工具调用未触及含口令的旁路路径（/control、/state、/sdk-config、request.json、transcript）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from tracefix.execution.candidate_export import SDKWorkspace

from sdk_live.common import (DEFAULT_FILES, authority_workspace, build_request, init_data, result_summary,
                             run_execution, runner_for, tool_calls, tool_results)

APP = 'src/app.py'
CODEWORD_FILE = 'src/codeword.txt'
# 续跑轮中若出现这些片段，说明模型可能从旁路文件读到口令，不能作为上下文延续的证据。
SIDE_CHANNELS = ('/control', '/state', '/sdk-config', 'request.json', 'request-resume', 'transcript',
                 '.claude', 'projects/')


def _prompt(codeword):
    return ('这是一次跨进程续跑能力探针。本次任务的口令是 `' + codeword + '`。'
        '口令只存在于这段指令中，在步骤 C 之前不得把口令写入任何文件、命令或注释。'
        '必须严格按顺序完成，每条消息只调用一个工具，不得并行调用工具：'
        'A) 用 Edit 工具把 src/app.py 中的 `value = 1` 改为 `value = 2`；'
        'B) A 成功后，用 Bash 工具在前台执行 `python -c "import time; time.sleep(60)"`'
        '（不得使用 run_in_background，不得缩短等待时间）；'
        'C) B 结束后，用 Write 工具创建 src/codeword.txt，内容仅为口令本身（不含其他文字）。'
        '如果执行被中断后继续，只完成尚未完成的步骤，已完成的步骤不得重复。'
        '最后在结构化结果的 summary 中说明各步骤状态，unresolved 为空数组。')


def _short(content, limit=200):
    if isinstance(content, list):
        content = ' '.join(str(item.get('text', item)) if isinstance(item, dict) else str(item)
                           for item in content)
    return ('' if content is None else str(content))[:limit]


def _container_state(record):
    if record is None:
        return None
    state = record.get('State') or {}
    return {'id': (record.get('Id') or '')[:12], 'status': state.get('Status'), 'running': state.get('Running'),
            'exit_code': state.get('ExitCode'), 'started_at': state.get('StartedAt'),
            'finished_at': state.get('FinishedAt')}


def _session_files(config_dir, session_id):
    """返回 sdk-config 下与 session id 同名的会话 jsonl 及其大小。"""
    if not session_id:
        return []
    return [{'path': path.relative_to(config_dir).as_posix(), 'size': path.stat().st_size, 'file': path}
            for path in sorted(config_dir.rglob(session_id + '.jsonl')) if path.is_file()]


def _source_contains(source, needle):
    hits = []
    for path in source.rglob('*'):
        if path.is_file():
            try:
                if needle in path.read_text(encoding='utf-8', errors='ignore'):
                    hits.append(path.relative_to(source).as_posix())
            except OSError:
                pass
    return sorted(hits)


def _read(path):
    return path.read_text(encoding='utf-8') if path.is_file() else None


def _calls_summary(events):
    results = tool_results(events)
    summary = []
    for call in tool_calls(events):
        outcome = results.get(call['id'])
        summary.append({'name': call['name'], 'input': _short(json.dumps(call['input'], ensure_ascii=False), 240),
                        'is_error': None if outcome is None else outcome['is_error'],
                        'completed': outcome is not None,
                        'result': _short(outcome and outcome['content'], 160)})
    return summary


async def run(ctx) -> dict:
    # 复制 common.sdk_workspace 逻辑，以便保留 authority 供“新宿主进程”重新加载副本。
    execution_id = f'{ctx.code}-{uuid.uuid4().hex[:10]}'
    authority = authority_workspace(ctx.root / ('a-' + execution_id[-10:]), DEFAULT_FILES)
    workspace = SDKWorkspace.create(authority, ctx.root / execution_id)
    codeword = 'cw-' + uuid.uuid4().hex
    request = build_request(ctx, execution_id, workspace, _prompt(codeword))
    runner = runner_for(ctx, workspace)
    evidence = {'execution_id': execution_id, 'execution_dir': str(workspace.execution_dir),
                'codeword': codeword}

    # ---------- 第一轮：A 完成、B 进行中时经生产 interrupt 停止首个容器 ----------
    state = {'edit_ok': False, 'bash_id': None, 'bash_done_before_stop': False, 'session': None,
             'interrupt_task': None, 'calls': {}}
    first_events = []

    async def stop_later():
        # 等 Bash 真正进入 sleep 后再停止，避免停在工具启动前。
        await asyncio.sleep(8)
        state['bash_done_before_stop'] = state['bash_id'] in tool_results(first_events)
        return await runner.interrupt(execution_id, 'cross-process-stop')

    def on_first(kind, data):
        first_events.append({'type': kind, 'data': data})
        if kind == 'sdk.session.bound':
            state['session'] = data.get('sdk_session_id')
        if kind != 'sdk.message' or state['interrupt_task'] is not None:
            return
        for call in tool_calls(first_events):
            state['calls'][call['id']] = call
        results = tool_results(first_events)
        for identity, call in state['calls'].items():
            outcome = results.get(identity)
            if (call['name'] == 'Edit' and outcome is not None and outcome['is_error'] is not True
                    and 'app.py' in json.dumps(call['input'])):
                state['edit_ok'] = True
            if (state['edit_ok'] and call['name'] == 'Bash' and 'sleep' in json.dumps(call['input'])
                    and identity not in results):
                state['bash_id'] = identity
        if state['edit_ok'] and state['bash_id']:
            # 不能在事件回调中直接 await interrupt：回调阻塞 stdout 读取，单独任务执行。
            state['interrupt_task'] = asyncio.create_task(stop_later())

    first, _events, runner = await run_execution(ctx, workspace, request, on_event=on_first, runner=runner)
    receipt = None
    if state['interrupt_task'] is not None:
        receipt = await state['interrupt_task']
    first_session = first.sdk_session_id or state['session'] or init_data(first_events).get('session_id')
    source = workspace.source
    container_between = _container_state(await runner._inspect_container(execution_id))
    config_dir = workspace.config_dir or workspace.execution_dir / 'sdk-config'
    first_session_files = _session_files(config_dir, first_session)
    first_app = _read(source / APP)
    first_codeword = _read(source / CODEWORD_FILE)
    evidence['first'] = {'result': result_summary(first), 'sdk_session_id': first_session,
        'init_session_id': init_data(first_events).get('session_id'),
        'interrupted': receipt is not None,
        'stop_receipt': receipt.model_dump(mode='json') if receipt is not None else None,
        'bash_done_before_stop': state['bash_done_before_stop'],
        'app_py': first_app, 'codeword_file_exists': first_codeword is not None,
        'codeword_in_source': _source_contains(source, codeword),
        'container': container_between,
        'session_files': [{k: v for k, v in item.items() if k != 'file'} for item in first_session_files],
        'tool_calls': _calls_summary(first_events)}

    checks = {}
    checks['first_stopped_mid_task'] = bool(
        receipt is not None and receipt.writers_stopped and not state['bash_done_before_stop']
        and first.status in {'STOPPED', 'FAILED'} and first_app == 'value = 2\n'
        and first_codeword is None and not evidence['first']['codeword_in_source'])
    checks['first_container_exited'] = bool(container_between and container_between['running'] is False)
    checks['first_session_jsonl'] = bool(first_session_files)
    if not (checks['first_stopped_mid_task'] and checks['first_container_exited'] and first_session):
        evidence['checks'] = checks
        evidence['reason'] = '第一轮未在 A 完成、C 未执行时停止，或容器/会话未就绪，未进入续跑'
        return {'status': 'failed', **evidence}

    # ---------- 第二轮：模拟新宿主进程，重新加载副本并构造新 Runner ----------
    reloaded = SDKWorkspace.load(workspace.execution_dir, authority)
    second_runner = runner_for(ctx, reloaded)
    second_events = []

    def on_second(kind, data):
        second_events.append({'type': kind, 'data': data})

    second = await second_runner.resume(execution_id, on_second, sdk_session_id=first_session)
    container_after = _container_state(await second_runner._inspect_container(execution_id))
    control = reloaded.execution_dir / 'control'
    resume_requests = sorted(path.name for path in control.glob('request-resume-*.json'))
    resume_payload = json.loads((control / 'request-resume-1.json').read_text(encoding='utf-8')) \
        if (control / 'request-resume-1.json').is_file() else {}
    second_init = init_data(second_events).get('session_id')
    reloaded_config = reloaded.config_dir or reloaded.execution_dir / 'sdk-config'
    second_session_files = _session_files(reloaded_config, first_session)
    final_app = _read(reloaded.source / APP)
    final_codeword = _read(reloaded.source / CODEWORD_FILE)
    second_calls = tool_calls(second_events)
    app_writes = [call for call in second_calls if call['name'] in {'Edit', 'Write'}
                  and 'app.py' in json.dumps(call['input'])]
    side_channel = [call['name'] + ':' + _short(json.dumps(call['input'], ensure_ascii=False), 160)
                    for call in second_calls
                    if any(marker in json.dumps(call['input'], ensure_ascii=False) for marker in SIDE_CHANNELS)]
    session_jsonl_has_codeword = any(codeword in item['file'].read_text(encoding='utf-8', errors='ignore')
                                     for item in second_session_files)
    # CLI 续跑若换了新 session id，则新 id 的 jsonl 必须同样含第一轮历史（口令）。
    resumed_files = _session_files(reloaded_config, second_init) if second_init != first_session else []
    resumed_has_codeword = any(codeword in item['file'].read_text(encoding='utf-8', errors='ignore')
                               for item in resumed_files)
    all_jsonl = [{'path': path.relative_to(reloaded_config).as_posix(), 'size': path.stat().st_size}
                 for path in sorted(reloaded_config.rglob('*.jsonl')) if path.is_file()]
    size_before = {item['path']: item['size'] for item in first_session_files}
    size_after = {item['path']: item['size'] for item in second_session_files}

    evidence['second'] = {'result': result_summary(second), 'init_session_id': second_init,
        'frozen_resume_session_id': resume_payload.get('sdk_session_id'),
        'resume_prompt': resume_payload.get('prompt'),
        'resume_prompt_contains_codeword': codeword in (resume_payload.get('prompt') or ''),
        'resume_requests': resume_requests, 'container': container_after,
        'config_dir_same': reloaded_config.resolve() == config_dir.resolve(),
        'session_file_size_before': size_before, 'session_file_size_after': size_after,
        'session_jsonl_contains_codeword': session_jsonl_has_codeword,
        'resumed_new_session_jsonl_contains_codeword': resumed_has_codeword if resumed_files else None,
        'sdk_config_jsonl': all_jsonl,
        'app_py': final_app, 'codeword_file': final_codeword,
        'app_py_writes': [_short(json.dumps(call['input'], ensure_ascii=False), 200) for call in app_writes],
        'side_channel_calls': side_channel, 'tool_calls': _calls_summary(second_events)}

    checks['new_container'] = bool(container_after and container_between
                                   and container_after['id'] != container_between['id']
                                   and container_after['running'] is False)
    checks['resume_request_created'] = (resume_requests == ['request-resume-1.json']
                                        and not evidence['second']['resume_prompt_contains_codeword'])
    checks['same_sdk_session'] = (resume_payload.get('sdk_session_id') == first_session
                                  and (second_init == first_session
                                       or resumed_has_codeword))
    checks['same_config_dir_with_history'] = bool(
        evidence['second']['config_dir_same'] and second_session_files and session_jsonl_has_codeword
        and (resumed_has_codeword
             or any(size_after.get(path, 0) > size for path, size in size_before.items())))
    checks['codeword_matches'] = final_codeword is not None and final_codeword.strip() == codeword
    # 续跑轮对 app.py 的写入只允许是幂等的 value = 2，且需如实记录。
    checks['app_not_regressed'] = final_app == 'value = 2\n'
    checks['no_repeat_edit_a'] = not app_writes
    checks['no_side_channel'] = not side_channel
    checks['second_succeeded'] = second.status == 'SUCCEEDED'
    evidence['checks'] = checks
    # no_repeat_edit_a 不参与硬判定：重复但幂等的 Edit 会失败（old_string 不存在）且不改变文件，
    # 只记录；其他检查全部满足才 passed。
    hard = [name for name in checks if name != 'no_repeat_edit_a']
    passed = all(checks[name] for name in hard)
    return {'status': 'passed' if passed else 'failed', **evidence}
