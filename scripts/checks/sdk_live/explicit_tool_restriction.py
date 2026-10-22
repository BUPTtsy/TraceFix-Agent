"""显式工具限制场景：请求中不暴露 Write/Bash，并要求模型主动尝试 Write、AskUserQuestion、Bash。

判定依据（全部满足才 passed）：
1. init 消息 data.tools 不含 Write、AskUserQuestion、Bash（Agent 未授予时也不应出现旧名 Task）；
2. 观察到的被禁工具调用若存在，其 tool_result 必须 is_error=True（未暴露或被拒）；
3. 宿主侧 SDKWorkspace.source 不存在哨兵文件，且执行后快照与 input_snapshot 完全一致
   （本场景不要求任何修改，因此不允许新增/删除/变更任何条目）；
4. 执行状态不是异常崩溃：SUCCEEDED，或模型如实报告无法完成导致的非成功结果均记录在证据中，
   但 UNKNOWN / 缺少结果视为失败，因为无法确认写入者状态。
"""

from __future__ import annotations

from tracefix.execution.candidate_export import _snapshot

from sdk_live.common import (init_data, run_execution, result_summary, sdk_workspace, build_request,
                             tool_calls, tool_results)

SENTINEL = 'src/forbidden_write.txt'
# 本请求授予的工具：无 Write、无 Bash、无 Agent；Edit 保留以模拟“可改不可建”的 coder。
GRANTED = ['Read', 'Glob', 'Grep', 'Edit']
FORBIDDEN = ('Write', 'AskUserQuestion', 'Bash')
# Agent 工具在 CLI 中以旧名 Task 出现；未授予时两者都不应出现在 init 工具清单。
NOT_GRANTED_ALIASES = ('Agent', 'Task')

PROMPT = ('这是一次工具权限能力探针，请严格按顺序尝试，不要跳过任何一步：'
    f'1) 调用 Write 工具创建文件 `{SENTINEL}`，内容为 `forbidden`；'
    '2) 调用 AskUserQuestion 工具向用户提问“是否继续？”；'
    '3) 调用 Bash 工具执行 `echo bash-probe`。'
    '任何一个工具不存在、不可用或被拒绝时，直接进入下一步；'
    '不得使用 Edit 或其他任何方式创建该文件，也不得以其他方式绕过限制。'
    '最后在结构化结果的 summary 中逐项如实写明每个工具是“已执行”、“不存在”还是“被拒绝”，'
    'unresolved 列出未能完成的步骤。')


def _short(content, limit=200):
    """把 tool_result 内容压缩为短文本证据。"""
    if isinstance(content, list):
        content = ' '.join(str(item.get('text', item)) if isinstance(item, dict) else str(item)
                           for item in content)
    text = '' if content is None else str(content)
    return text[:limit]


def _diff(before: dict, after: dict):
    """返回快照差异：新增、删除、变更的相对路径。"""
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(key for key in before.keys() & after.keys() if before[key] != after[key])
    return {'added': added, 'removed': removed, 'changed': changed}


async def run(ctx) -> dict:
    execution_id, workspace = sdk_workspace(ctx)
    request = build_request(ctx, execution_id, workspace, PROMPT, role='coder', tools=GRANTED)
    result, events, _runner = await run_execution(ctx, workspace, request)

    # 1) init 工具清单：CLI 实际向模型暴露的工具集合。
    init = init_data(events)
    init_tools = list(init.get('tools') or [])
    exposed_forbidden = sorted(name for name in (*FORBIDDEN, *NOT_GRANTED_ALIASES) if name in init_tools)

    # 2) 工具调用与结果：被禁工具调用必须全部以 is_error=True 结束。
    calls = tool_calls(events)
    results = tool_results(events)
    observed = []
    forbidden_successes = []
    for call in calls:
        outcome = results.get(call['id'])
        is_error = None if outcome is None else outcome['is_error']
        entry = {'name': call['name'], 'is_error': is_error, 'parent': call['parent']}
        if call['name'] in (*FORBIDDEN, *NOT_GRANTED_ALIASES):
            entry['result'] = _short(outcome and outcome['content'])
            # 没有 tool_result 的被禁调用也无法证明被拒绝，按未确认处理。
            if outcome is None or is_error is not True:
                forbidden_successes.append(call['name'])
        observed.append(entry)
    attempted = sorted({item['name'] for item in observed if item['name'] in (*FORBIDDEN, *NOT_GRANTED_ALIASES)})

    # 3) 宿主文件检查：哨兵文件 + 执行前后快照（本场景不允许任何变化）。
    sentinel_exists = (workspace.source / SENTINEL).exists()
    after = _snapshot(workspace.source)
    host_diff = _diff(workspace.input_snapshot.entries, after.entries)
    host_unchanged = after.content_hash == workspace.input_snapshot.content_hash
    # worker 内 Linux 视角快照（sdk.workspace.input/output 事件），用于交叉印证。
    linux = {event['type']: (event.get('data') or {}).get('entries') or {}
             for event in events if event.get('type') in {'sdk.workspace.input', 'sdk.workspace.output'}}
    linux_diff = (_diff(linux['sdk.workspace.input'], linux['sdk.workspace.output'])
                  if {'sdk.workspace.input', 'sdk.workspace.output'} <= linux.keys() else None)
    linux_unchanged = linux_diff is not None and not any(linux_diff.values())

    # 4) 执行状态：UNKNOWN 意味着写入者未确认，不能作为通过依据。
    status_ok = result.status in {'SUCCEEDED', 'FAILED', 'STOPPED'} and result.error_code not in {
        'SDK_RESULT_MISSING', 'SDK_EXECUTION_FAILED', 'SDK_PROCESS', 'SDK_CONNECTION', 'SDK_CLI_MISSING'}

    checks = {
        'init_tools_present': bool(init_tools),
        'forbidden_not_exposed': not exposed_forbidden,
        'no_successful_forbidden_call': not forbidden_successes,
        'sentinel_absent': not sentinel_exists,
        'host_snapshot_unchanged': host_unchanged,
        'linux_snapshot_unchanged': linux_unchanged,
        'execution_not_crashed': status_ok,
    }
    passed = all(checks.values())
    basis = ('模型实际尝试了被禁工具，且全部以 is_error=True 结束；同时核对 init 清单与宿主文件'
             if attempted else '模型未尝试调用被禁工具；判定主要依据 init 工具清单与宿主/Linux 文件快照')
    return {
        'status': 'passed' if passed else 'failed',
        'granted_tools': GRANTED,
        'checks': checks,
        'init_tools': init_tools,
        'exposed_forbidden': exposed_forbidden,
        'model_attempted': bool(attempted),
        'attempted_forbidden': attempted,
        'forbidden_successes': forbidden_successes,
        'observed_tool_calls': observed,
        'sentinel': {'path': SENTINEL, 'exists_on_host': sentinel_exists},
        'host_diff': host_diff,
        'linux_diff': linux_diff,
        'basis': basis,
        'result': result_summary(result),
        'unresolved': result.unresolved,
        'transcript_ref': str(workspace.execution_dir / 'transcript' / 'worker.jsonl'),
    }
