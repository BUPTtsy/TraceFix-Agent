"""checkpoint_scope 场景：对主 Agent Edit、Bash、子 Agent 修改检查完整快照恢复范围。

第 1 部分（生产机制，必须通过）：
  经生产 AgentExecutionRunner 让模型完成三类修改——
  主 Agent 用 Edit 改 src/a.py；一次 Bash 改 src/b.py、新建 src/new_by_bash.txt、把 src/old.txt 改名为
  src/renamed.txt（目录操作）；委派 general-purpose 子 Agent 用 Edit 改 src/c.py。
  宿主侧判定（全部满足）：
  - 三类修改均由预期主体通过成功的工具回执完成（Edit 主 Agent / Bash 主 Agent / 子 Agent Edit|Write）；
  - baseline（input 目录）执行后仍等于 input_snapshot（check_input 通过）；
  - freeze() 输出快照与 input_snapshot 的差异覆盖全部预期路径，且 output_manifest.json 与之一致；
  - Linux 边界清单（Runner 在容器内采集）给出同样的差异；
  - 按 input_snapshot 用 baseline 复原 source 后，_snapshot(source) == input_snapshot。
第 2 部分（尽力而为）：
  生产接口不暴露 rewind。用与 build_command 相同的隔离参数另起一个 --rm 容器，运行只读挂载的
  checkpoint_rewind_worker.py：选项由生产 adapter._options 构造，只追加 replay-user-messages；
  完成同样三类修改后 rewind_files 回到首条用户消息，由宿主逐项比较哪些修改被回滚、哪些残留。
状态：第 1 部分通过 → passed（第 2 部分未能实测时 evidence 标 sdk_rewind=not_measured）；否则 failed。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
from pathlib import Path

from tracefix.execution.agent_runner import _mount
from tracefix.execution.candidate_export import SDKCandidateRejected, _snapshot
from tracefix.execution.platforms import container_user, subprocess_options, terminate_tree

from sdk_live.common import (build_request, result_summary, run_execution, runner_for, sdk_workspace,
                             tool_calls, tool_results)

FILES = {'src/a.py': 'A = 1\n', 'src/b.py': 'B = 1\n', 'src/c.py': 'C = 1\n',
         'src/old.txt': 'old file\n', '.gitignore': 'build/\n'}
BASH_COMMAND = ('sed -i "s/B = 1/B = 2/" src/b.py && printf "created by bash\\n" > src/new_by_bash.txt '
                '&& mv src/old.txt src/renamed.txt')
PROMPT = ('这是一次文件检查点范围探针，严格按顺序执行，每一步只能使用指定工具：'
    '1) 用 Read 读取 src/a.py，然后用 Edit 工具把 `A = 1` 改为 `A = 2`（必须用 Edit，不得用 Bash 或 Write）；'
    f'2) 只调用一次 Bash 工具，原样执行命令：`{BASH_COMMAND}`（这一步不得用 Edit/Write 代替）；'
    '3) 调用 Agent 工具委派子 Agent，subagent_type 必须设为 "general-purpose"（不得使用 readonly），'
    '要求子 Agent 先 Read src/c.py，再用 Edit 工具把 `C = 1` 改为 `C = 2`；主 Agent 自己不得读写 src/c.py；'
    '4) 不要做任何其他修改，也不要撤销已做的修改。'
    '最后在结构化结果 summary 中逐步写明是否成功，unresolved 列出失败的步骤（全部成功则为空数组）。')
# 每类修改预期影响的路径（相对 source）。
EXPECTED = {
    'main_edit': {'changed': ['src/a.py']},
    'bash': {'changed': ['src/b.py'], 'added': ['src/new_by_bash.txt', 'src/renamed.txt'],
             'removed': ['src/old.txt']},
    'subagent': {'changed': ['src/c.py']},
}
EXPECTED_CONTENT = {'src/a.py': 'A = 2', 'src/b.py': 'B = 2', 'src/c.py': 'C = 2',
                    'src/new_by_bash.txt': 'created by bash', 'src/renamed.txt': 'old file'}
AGENT_TOOLS = {'Agent', 'Task'}
WRITE_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
REWIND_TIMEOUT = 480
WORKER = Path(__file__).resolve().parent / 'checkpoint_rewind_worker.py'


def _diff(before: dict, after: dict):
    """快照差异：新增、删除、变更的相对路径。"""
    return {'added': sorted(after.keys() - before.keys()),
            'removed': sorted(before.keys() - after.keys()),
            'changed': sorted(key for key in before.keys() & after.keys() if before[key] != after[key])}


def _expected_paths():
    return {kind: sorted({path for paths in groups.values() for path in paths})
            for kind, groups in EXPECTED.items()}


def _covers(diff):
    """每个预期路径是否以预期方式出现在差异中；返回 (是否全覆盖, 缺失列表, 额外列表)。"""
    missing = [f'{bucket}:{path}' for groups in EXPECTED.values() for bucket, paths in groups.items()
               for path in paths if path not in diff[bucket]]
    expected = {(bucket, path) for groups in EXPECTED.values() for bucket, paths in groups.items()
                for path in paths}
    extra = [f'{bucket}:{path}' for bucket, paths in diff.items() for path in paths
             if (bucket, path) not in expected]
    return not missing, missing, extra


def _short(content, limit=200):
    if isinstance(content, list):
        content = ' '.join(str(item.get('text', item)) if isinstance(item, dict) else str(item)
                           for item in content)
    return ('' if content is None else str(content))[:limit]


def _touches(call, path):
    """工具调用输入是否指向某个文件（file_path 或命令文本）。"""
    value = call['input'] or {}
    target = str(value.get('file_path') or value.get('notebook_path') or '')
    if target:
        return target.replace('\\', '/').endswith(path)
    return path in str(value.get('command') or '')


def _attribution(events):
    """按工具回执判断三类修改的执行主体；子 Agent 调用以 parent_tool_use_id 识别。"""
    calls, results = tool_calls(events), tool_results(events)

    def ok(call):
        outcome = results.get(call['id'])
        return outcome is not None and outcome['is_error'] is not True

    agent_calls = [call for call in calls if call['name'] in AGENT_TOOLS and call['parent'] is None]
    agent_ids = {call['id'] for call in agent_calls}
    main = [call for call in calls if call['parent'] is None]
    sub = [call for call in calls if call['parent'] in agent_ids]
    main_edit = [call for call in main if call['name'] == 'Edit' and _touches(call, 'src/a.py')]
    bash = [call for call in main if call['name'] == 'Bash' and _touches(call, 'src/b.py')]
    sub_write = [call for call in sub if call['name'] in WRITE_TOOLS and _touches(call, 'src/c.py')]
    # 主 Agent 不得自己改 c.py，否则无法把 c.py 的变化归因于子 Agent。
    main_on_c = [call['name'] for call in main
                 if call['name'] in WRITE_TOOLS | {'Bash'} and _touches(call, 'src/c.py')]
    # a.py 不得被 Bash/Write 改动，否则主 Agent Edit 归因不纯。
    other_on_a = [call['name'] for call in calls
                  if call['name'] in {'Bash', 'Write'} and _touches(call, 'src/a.py')]
    record = lambda items: [{'name': c['name'], 'ok': ok(c), 'parent': c['parent'],
                             'result': _short((results.get(c['id']) or {}).get('content'), 160)}
                            for c in items]
    return {
        'main_edit_ok': any(ok(call) for call in main_edit),
        'bash_ok': any(ok(call) for call in bash),
        'subagent_type': [(call['input'] or {}).get('subagent_type') for call in agent_calls],
        'subagent_spawned': any(ok(call) for call in agent_calls),
        'subagent_write_ok': any(ok(call) for call in sub_write),
        'main_touched_c': main_on_c,
        'non_edit_touched_a': other_on_a,
        'calls': {'main_edit': record(main_edit), 'bash': record(bash), 'agent': record(agent_calls),
                  'subagent_write': record(sub_write)},
        'subagent_tool_names': sorted({call['name'] for call in sub}),
        'all_tool_names': [f"{c['name']}{'@sub' if c['parent'] else ''}" for c in calls],
    }


def _content_ok(root: Path):
    found = {}
    for relative, marker in EXPECTED_CONTENT.items():
        path = root / relative
        found[relative] = path.is_file() and marker in path.read_text(encoding='utf-8', errors='replace')
    found['src/old.txt_absent'] = not (root / 'src/old.txt').exists()
    return found


def restore_from_baseline(workspace):
    """不依赖 SDK checkpoint：按 input_snapshot 用 baseline（input 冻结副本）复原 source。

    先删除快照外的新增条目（深层优先），再按快照重建目录、写回内容不一致或缺失的文件并恢复模式。
    """
    workspace.check_input()  # baseline 必须仍等于 input_snapshot，才能作为恢复来源
    expected = workspace.input_snapshot.entries
    current = _snapshot(workspace.source).entries
    actions = []
    for relative in sorted(current.keys() - expected.keys(), key=lambda item: item.count('/'), reverse=True):
        path = workspace.source / relative
        if not path.exists() and not path.is_symlink():
            continue
        if current[relative]['kind'] == 'directory':
            shutil.rmtree(path)
        else:
            path.unlink()
        actions.append('delete:' + relative)
    for relative in sorted(expected, key=lambda item: (item.count('/'), item)):
        entry, path = expected[relative], workspace.source / relative
        if entry['kind'] == 'directory':
            if path.exists() and not path.is_dir():
                path.unlink()
            if not path.exists():
                path.mkdir()
                actions.append('mkdir:' + relative)
            continue
        if entry['kind'] != 'file':
            raise SDKCandidateRejected('冻结输入包含非普通文件，无法恢复', relative)
        if current.get(relative) == entry:
            continue
        if path.is_dir():
            shutil.rmtree(path)
        data = (workspace.baseline / relative).read_bytes()
        if path.exists():
            path.chmod(stat.S_IMODE(path.lstat().st_mode) | stat.S_IWRITE)
        path.write_bytes(data)
        path.chmod(entry['mode'])
        actions.append('restore:' + relative)
    return actions


def _linux_diff(execution_dir: Path):
    """Runner 在独立容器中采集的 Linux 视角输入/输出清单。"""
    paths = [execution_dir / 'linux_input_manifest.json', execution_dir / 'linux_output_manifest.json']
    if not all(path.is_file() for path in paths):
        return None
    start, end = (json.loads(path.read_text(encoding='utf-8'))['entries'] for path in paths)
    return _diff(start, end)


async def host_snapshot_part(ctx):
    execution_id, workspace = sdk_workspace(ctx, FILES)
    request = build_request(ctx, execution_id, workspace, PROMPT)
    result, events, _runner = await run_execution(ctx, workspace, request)
    attribution = _attribution(events)

    # 输出快照：Runner 在 finally 中已调用 freeze()，再次 freeze() 应得到同一结果。
    output = workspace.freeze()
    manifest = json.loads((workspace.execution_dir / 'output_manifest.json').read_text(encoding='utf-8'))
    diff = _diff(workspace.input_snapshot.entries, output.entries)
    covered, missing, extra = _covers(diff)
    linux = _linux_diff(workspace.execution_dir)
    linux_covered = linux is not None and _covers(linux)[0]
    content = _content_ok(workspace.source)
    try:
        workspace.check_input()
        baseline_intact = True
    except SDKCandidateRejected:
        baseline_intact = False
    baseline_original = all((workspace.baseline / relative).read_text(encoding='utf-8') == text
                            for relative, text in FILES.items())
    # 生产导出会拒绝删除/改名；这里只记录边界检查的实际反应，不作为本场景判定条件。
    try:
        workspace.check_linux_boundary()
        boundary = 'accepted'
    except SDKCandidateRejected as error:
        boundary = f'rejected: {error.reason} {error.path}'.strip()

    # 留存修改后的副本作为证据，再按 baseline 复原 source。
    shutil.copytree(workspace.source, workspace.execution_dir / 'post_run_source')
    actions = restore_from_baseline(workspace)
    restored = _snapshot(workspace.source)
    restored_equal = restored == workspace.input_snapshot

    happened = {
        'main_edit': attribution['main_edit_ok'] and content['src/a.py']
                     and not attribution['non_edit_touched_a'],
        'bash': attribution['bash_ok'] and content['src/b.py'] and content['src/new_by_bash.txt']
                and content['src/renamed.txt'] and content['src/old.txt_absent'],
        'subagent': attribution['subagent_write_ok'] and content['src/c.py']
                    and not attribution['main_touched_c'],
    }
    checks = {
        'execution_not_unknown': result.status in {'SUCCEEDED', 'FAILED', 'STOPPED'},
        'main_edit_happened': happened['main_edit'],
        'bash_happened': happened['bash'],
        'subagent_edit_happened': happened['subagent'],
        'baseline_intact': baseline_intact and baseline_original,
        'output_manifest_matches_freeze': manifest == output.model_dump(),
        'snapshot_diff_covers_all': covered,
        'linux_diff_covers_all': linux_covered,
        'restored_equals_input_snapshot': restored_equal,
    }
    return {
        'passed': all(checks.values()),
        'checks': checks,
        'happened': happened,
        'attribution': attribution,
        'content_markers': content,
        'snapshot_diff': diff,
        'missing_from_diff': missing,
        'extra_in_diff': extra,
        'linux_diff': linux,
        'production_boundary_check': boundary,
        'restore_actions': actions,
        'restored_hash_equal': restored.content_hash == workspace.input_snapshot.content_hash,
        'result': result_summary(result),
        'unresolved': result.unresolved,
        'execution_dir': str(workspace.execution_dir),
        'transcript_ref': str(workspace.execution_dir / 'transcript' / 'worker.jsonl'),
    }


def _rewind_command(ctx, workspace, runner, container):
    """与 AgentExecutionRunner.build_command 相同的隔离参数；仅入口换成只读挂载的 rewind 探针。"""
    return ['docker', 'run', '--rm', '--name', container, '--network', ctx.network,
        '--label', 'tracefix.probe=checkpoint_scope',
        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--user', container_user(), '--pids-limit', '256', '--workdir', '/workspace',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
        '--mount', _mount(workspace.source, '/workspace'),
        '--mount', _mount(workspace.execution_dir / 'control', '/control', readonly=True),
        '--mount', _mount(workspace.execution_dir / 'state', '/state'),
        '--mount', _mount(workspace.execution_dir / 'sdk-config', '/sdk-config'),
        '--mount', _mount(WORKER, '/probe/checkpoint_rewind_worker.py', readonly=True),
        # 密钥只以 --env NAME 形式从子进程环境继承，不出现在命令行。
        '--env', 'ANTHROPIC_API_KEY',
        '--env', 'TRACEFIX_SDK_WORKER=1', '--env', 'HOME=/sdk-config',
        '--env', 'CLAUDE_CONFIG_DIR=/sdk-config', '--env', 'PYTHONUNBUFFERED=1',
        '--env', 'HTTP_PROXY=' + runner.egress_proxy, '--env', 'HTTPS_PROXY=' + runner.egress_proxy,
        '--entrypoint', '/usr/local/bin/python', ctx.config.worker_image,
        '/probe/checkpoint_rewind_worker.py', '--request', '/control/rewind-request.json',
        '--result', '/state/rewind-result.json']


def _state(entries, relative):
    """只比较类型与内容摘要：宿主（Windows）与容器（Linux）视角的 mode/nlink 不可直接比较。"""
    entry = entries.get(relative)
    return None if entry is None else (entry['kind'], entry['digest'])


def _classify(before, changed, rewound):
    """逐项比较：修改是否发生、rewind 后是否回到修改前状态。"""
    items = {}
    for kind, groups in EXPECTED.items():
        for bucket, paths in groups.items():
            for relative in paths:
                original, middle, final = (_state(before, relative), _state(changed, relative),
                                           _state(rewound, relative))
                items[f'{kind}:{bucket}:{relative}'] = {
                    'modified': middle != original,
                    'rolled_back': final == original,
                    'after_rewind': 'absent' if final is None else 'same_as_input' if final == original
                                    else 'same_as_modified' if final == middle else 'other',
                }
    return items


async def sdk_rewind_part(ctx):
    execution_id, workspace = sdk_workspace(ctx, FILES)
    runner = runner_for(ctx, workspace)
    request = build_request(ctx, execution_id, workspace, PROMPT)
    await runner._preflight(request)  # 复用生产网络/镜像/出口策略校验
    payload = request.model_dump(mode='json')
    payload.update(workspace='/workspace', transcript_path='/state/transcript.jsonl')
    (workspace.execution_dir / 'control' / 'rewind-request.json').write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    container = 'tf-sdk-rw-' + execution_id.lower()
    argv = _rewind_command(ctx, workspace, runner, container)
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=runner._environment(request), **subprocess_options())
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), REWIND_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        await runner._docker('stop', '--time', '10', container, check=False)
        await terminate_tree(process)
        return {'sdk_rewind': 'not_measured', 'reason': 'rewind 容器超时，已停止',
                'execution_dir': str(workspace.execution_dir)}
    (workspace.execution_dir / 'transcript' / 'rewind-worker.jsonl').write_bytes(stdout)
    (workspace.execution_dir / 'transcript' / 'rewind-worker.stderr').write_bytes(stderr)
    result_path = workspace.execution_dir / 'state' / 'rewind-result.json'
    if not result_path.is_file():
        return {'sdk_rewind': 'not_measured', 'reason': 'rewind 探针未写出结果',
                'exit_code': process.returncode, 'stderr_tail': stderr.decode('utf-8', 'replace')[-400:],
                'execution_dir': str(workspace.execution_dir)}
    report = json.loads(result_path.read_text(encoding='utf-8'))
    events = [json.loads(line) for line in stdout.decode('utf-8', 'replace').splitlines()
              if line.startswith('{')]
    attribution = _attribution(events)
    stages = report.get('stages') or {}
    host_after = _snapshot(workspace.source).entries
    evidence = {'execution_dir': str(workspace.execution_dir), 'exit_code': process.returncode,
                'options': report.get('options'), 'checkpoint_uuid_found': bool(report.get('checkpoint_uuid')),
                'terminal': report.get('terminal'), 'dry_run': report.get('dry_run'),
                'dry_run_error': report.get('dry_run_error'), 'rewind_response': report.get('rewind'),
                'rewind_error': report.get('rewind_error'), 'error': report.get('error'),
                'attribution': {key: attribution[key] for key in ('main_edit_ok', 'bash_ok', 'subagent_type',
                    'subagent_spawned', 'subagent_write_ok', 'main_touched_c', 'subagent_tool_names')},
                'transcript_ref': str(workspace.execution_dir / 'transcript' / 'rewind-worker.jsonl')}
    if 'after_rewind' not in stages or 'after_changes' not in stages or report.get('rewind_error'):
        evidence.update(sdk_rewind='not_measured',
                        reason=report.get('rewind_error') or report.get('error') or 'rewind 未执行到快照阶段')
        return evidence
    before = workspace.input_snapshot.entries
    linux_items = _classify(stages['before'], stages['after_changes'], stages['after_rewind'])
    host_items = _classify(before, stages['after_changes'], host_after)
    measured = {kind: [value for key, value in linux_items.items() if key.startswith(kind + ':')]
                for kind in EXPECTED}
    summary = {kind: {'modified': all(item['modified'] for item in values),
                      'rolled_back': all(item['rolled_back'] for item in values),
                      'partially_rolled_back': any(item['rolled_back'] for item in values)}
               for kind, values in measured.items()}
    # 契约：Edit/Write 被追踪应回滚；Bash、子 Agent 编辑、目录操作不保证回滚。
    untracked_but_rolled_back = [kind for kind in ('bash', 'subagent')
                                 if summary[kind]['modified'] and summary[kind]['partially_rolled_back']]
    tracked_not_rolled_back = not summary['main_edit']['rolled_back'] and summary['main_edit']['modified']
    evidence.update(
        sdk_rewind='measured',
        all_changes_happened=all(item['modified'] for item in linux_items.values()),
        per_path_linux=linux_items,
        per_path_host_after_exit=host_items,
        per_kind=summary,
        linux_and_host_agree={key: linux_items[key]['rolled_back'] == host_items[key]['rolled_back']
                              for key in linux_items},
        restored_equals_input_snapshot=_snapshot(workspace.source) == workspace.input_snapshot,
        contract_consistent=not tracked_not_rolled_back and not untracked_but_rolled_back,
        untracked_but_rolled_back=untracked_but_rolled_back,
        tracked_edit_not_rolled_back=tracked_not_rolled_back,
    )
    return evidence


async def run(ctx) -> dict:
    host = await host_snapshot_part(ctx)
    try:
        rewind = await sdk_rewind_part(ctx)
    except Exception as error:
        rewind = {'sdk_rewind': 'not_measured', 'reason': f'{type(error).__name__}: {str(error)[:300]}'}
    return {
        'status': 'passed' if host['passed'] else 'failed',
        'host_snapshot': host,
        'sdk_rewind': rewind.get('sdk_rewind', 'not_measured'),
        'sdk_rewind_evidence': rewind,
        'basis': ('宿主完整快照（input 冻结副本 + input_snapshot）恢复不依赖 SDK checkpoint；'
                  'SDK rewind 仅作范围实测，结论不影响 passed/failed'),
    }
