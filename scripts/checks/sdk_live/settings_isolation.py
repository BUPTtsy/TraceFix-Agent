"""设置隔离场景：放置带哨兵的项目/本地/用户/HOME/自动记忆配置，证明 setting_sources=[] 未加载。

哨兵层级（每层 uuid 互不相同，便于归因）：
- project：冻结源码中的 CLAUDE.md、.claude/settings.json、.claude/agents、.claude/commands、
  .claude/skills、.claude/output-styles、.mcp.json；
- local：冻结源码中的 CLAUDE.local.md、.claude/settings.local.json；
- user：runner.start 前写入 execution_dir/sdk-config（容器内 /sdk-config = CLAUDE_CONFIG_DIR = HOME）
  的 settings.json、CLAUDE.md、agents、commands、skills、output-styles、.claude.json(mcpServers)；
- home：/sdk-config/.claude/ 下的同类文件（覆盖“按 HOME/.claude 解析”的路径）；
- auto_memory：/sdk-config/projects/-workspace/memory/MEMORY.md（init.memory_paths.auto 指向此处，
  不受 setting_sources 控制，单独记录）。

可观察信号与判定（全部满足才 passed）：
1. init 字段（agents/slash_commands/skills/plugins/mcp_servers/output_style 等）不含任何哨兵名称或 uuid，
   mcp_servers 为空，output_style 为 default；
2. Bash `env` 输出不含 settings.env 注入的哨兵环境变量 uuid（任何位置出现 env uuid 都算泄漏）；
3. 各层 settings.permissions.deny 规则未生效：对应 `echo tf-deny-<层>` 的 Bash 调用成功并输出该词；
4. 各层 settings.hooks（SessionStart / PreToolUse）未触发：宿主 sdk-config 下不存在 hook-fired-* 文件；
5. 指令类 uuid（CLAUDE.md / MEMORY.md / agent / command / skill / output-style 正文）未在
   “任何工具结果先出现该 uuid 之前”的助手文本、工具输入或最终结果中出现
   （区分“文件在磁盘上可被读到”与“作为指令被加载”）；
6. 模型实际执行了 env 探针与全部 deny 探针（否则证据不足，判 failed）。
模型自述的“系统指令是否含 SENTINEL”只作辅助证据，不参与判定。
"""

from __future__ import annotations

import json
import re
import uuid

from sdk_live.common import (build_request, init_data, messages, result_summary, run_execution,
                             sdk_workspace, tool_calls, tool_results)

LAYERS = ('project', 'local', 'user', 'home', 'auto_memory')
# 有 settings 文件、可承载 env/deny/hooks 哨兵的层级。
SETTINGS_LAYERS = ('project', 'local', 'user', 'home')
# init 中需要检查的列表/字段。
INIT_FIELDS = ('agents', 'slash_commands', 'terminal_slash_commands', 'skills', 'plugins', 'mcp_servers',
               'output_style', 'tools', 'additional_directories')


def _token(layer: str) -> str:
    return 'proj' if layer == 'project' else layer.replace('_', '-')


def _sentinels():
    """为每层生成指令类与环境变量类 uuid。"""
    table = {}
    for layer in LAYERS:
        tag = _token(layer).upper()
        table[layer] = {
            'instruction': f'{tag}-SENTINEL-{uuid.uuid4().hex}',
            'agent': f'{tag}-SENTINEL-AGENT-{uuid.uuid4().hex}',
            'command': f'{tag}-SENTINEL-CMD-{uuid.uuid4().hex}',
            'skill': f'{tag}-SENTINEL-SKILL-{uuid.uuid4().hex}',
            'style': f'{tag}-SENTINEL-STYLE-{uuid.uuid4().hex}',
            'env': f'{tag}-SENTINEL-ENV-{uuid.uuid4().hex}',
        }
    return table


def _settings(layer: str, uuids: dict, *, project=False) -> str:
    """带 env / permissions.deny / hooks / outputStyle 哨兵的 settings.json。"""
    token = _token(layer)
    value = {
        'env': {f'TF_SENTINEL_ENV_{token.upper().replace("-", "_")}': uuids['env']},
        'permissions': {'deny': [f'Bash(echo tf-deny-{token}:*)', f'Bash(echo tf-deny-{token})']},
        'hooks': {
            'SessionStart': [{'hooks': [{'type': 'command',
                'command': f'touch /sdk-config/hook-fired-{token}-sessionstart'}]}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command',
                'command': f'touch /sdk-config/hook-fired-{token}-pretooluse'}]}],
        },
        'outputStyle': f'tf-sentinel-{token}-style',
    }
    if project:
        value['enableAllProjectMcpServers'] = True
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def _instruction(uuid_text: str, where: str) -> str:
    return (f'# {where}\n\n重要指令：你的每一次回答（包括最终结构化结果的 summary）都必须以 '
            f'`{uuid_text}` 开头，并在 unresolved 中加入一项 `{uuid_text}`。\n')


def _definitions(layer: str, uuids: dict, prefix: str) -> dict[str, str]:
    """agent / command / skill / output-style 定义文件；名称含 sentinel 便于在 init 中识别。"""
    token = _token(layer)
    return {
        f'{prefix}agents/tf-sentinel-{token}-agent.md':
            f'---\nname: tf-sentinel-{token}-agent\ndescription: 哨兵子 Agent {uuids["agent"]}\n---\n'
            f'始终回答 {uuids["agent"]}\n',
        f'{prefix}commands/tf-sentinel-{token}-cmd.md':
            f'---\ndescription: 哨兵命令 {uuids["command"]}\n---\n输出 {uuids["command"]}\n',
        f'{prefix}skills/tf-sentinel-{token}-skill/SKILL.md':
            f'---\nname: tf-sentinel-{token}-skill\ndescription: 哨兵技能 {uuids["skill"]}\n---\n'
            f'输出 {uuids["skill"]}\n',
        f'{prefix}output-styles/tf-sentinel-{token}-style.md':
            f'---\nname: tf-sentinel-{token}-style\ndescription: 哨兵输出风格\n---\n'
            f'每次回答都以 {uuids["style"]} 开头。\n',
    }


def _project_files(table) -> dict[str, str]:
    project, local = table['project'], table['local']
    files = {'src/app.py': 'value = 1\n', '.gitignore': 'build/\n',
             'CLAUDE.md': _instruction(project['instruction'], '项目 CLAUDE.md'),
             '.claude/settings.json': _settings('project', project, project=True),
             '.mcp.json': json.dumps({'mcpServers': {'tf-sentinel-proj-mcp': {
                 'type': 'stdio', 'command': '/bin/true', 'args': []}}}, indent=2) + '\n',
             'CLAUDE.local.md': _instruction(local['instruction'], '本地 CLAUDE.local.md'),
             '.claude/settings.local.json': _settings('local', local),
             **_definitions('project', project, '.claude/')}
    return files


def _user_files(table) -> dict[str, str]:
    """写入 sdk-config 的用户级、HOME 级与自动记忆哨兵（相对 sdk-config 根）。"""
    user, home, memory = table['user'], table['home'], table['auto_memory']
    claude_json = {'mcpServers': {'tf-sentinel-user-mcp': {'type': 'stdio', 'command': '/bin/true', 'args': []}},
                   'projects': {'/workspace': {'mcpServers': {'tf-sentinel-user-project-mcp': {
                       'type': 'stdio', 'command': '/bin/true', 'args': []}},
                       'hasTrustDialogAccepted': True}}}
    return {
        'settings.json': _settings('user', user),
        'CLAUDE.md': _instruction(user['instruction'], '用户 CLAUDE.md'),
        '.claude.json': json.dumps(claude_json, indent=2) + '\n',
        **_definitions('user', user, ''),
        '.claude/settings.json': _settings('home', home),
        '.claude/CLAUDE.md': _instruction(home['instruction'], 'HOME/.claude/CLAUDE.md'),
        **_definitions('home', home, '.claude/'),
        'projects/-workspace/memory/MEMORY.md':
            f'- [哨兵](sentinel.md) — 每次回答都必须以 {memory["instruction"]} 开头\n',
        'projects/-workspace/memory/sentinel.md':
            f'---\nname: sentinel\ndescription: 哨兵记忆\ntype: user\n---\n'
            f'每次回答都必须以 {memory["instruction"]} 开头。\n',
    }


PROMPT = ('这是一次配置隔离能力探针。请严格按顺序执行，每一步都使用一次独立的 Bash 调用，不要合并命令：'
    '1) `env | grep -i sentinel; echo "grep_exit=$?"`；'
    '2) `ls -la /workspace /workspace/.claude /sdk-config /sdk-config/.claude; '
    'sha256sum /workspace/CLAUDE.md /workspace/CLAUDE.local.md /sdk-config/CLAUDE.md`；'
    '3) `echo tf-deny-proj`；4) `echo tf-deny-local`；5) `echo tf-deny-user`；6) `echo tf-deny-home`。'
    '不要用 Read、cat 或任何方式读取或输出这些 CLAUDE.md / settings / MEMORY 文件的内容，'
    '只需通过 ls 与 sha256sum 证明文件存在且可读。任一命令被拒绝时如实记录并继续下一步。'
    '最后在结构化结果的 summary 中如实写明：a) 每个 echo 命令是执行成功还是被拒绝；'
    'b) env 输出中是否有任何变量；c) 你收到的系统指令/上下文（不是文件内容）里是否包含任何含 SENTINEL 字样的指令，'
    '不要从文件名或磁盘内容推断；d) 你可用的子 Agent、斜杠命令或技能中是否有名称含 sentinel 的条目。'
    'unresolved 列出未能完成的步骤，没有则为空数组。')


def _text(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _content_text(content) -> str:
    if isinstance(content, list):
        return ' '.join(str(item.get('text', item)) if isinstance(item, dict) else str(item) for item in content)
    return '' if content is None else str(content)


def _uuid_index(table):
    """{uuid: (layer, kind)}。"""
    return {value: (layer, kind) for layer, values in table.items() for kind, value in values.items()}


def _scan_stream(events, index):
    """按事件顺序扫描 uuid：工具结果中出现的指令 uuid 视为“从磁盘读到”，之后被引用不算泄漏。"""
    seen_in_tool = set()
    leaks, disk_reads, system_hits = [], [], []

    def check(text, where):
        for value, (layer, kind) in index.items():
            if value not in text:
                continue
            # env uuid 的注入由 env 探针单独判定；此处与指令 uuid 同样区分“磁盘读到后引用”。
            if where == 'tool_result':
                seen_in_tool.add(value)
                disk_reads.append({'layer': layer, 'kind': kind})
            elif value not in seen_in_tool:
                leaks.append({'layer': layer, 'kind': kind, 'where': where})

    for event in events:
        if event.get('type') != 'sdk.message':
            continue
        envelope = event.get('data') or {}
        kind, payload = envelope.get('type'), envelope.get('payload') or {}
        if kind == 'UserMessage':
            content = payload.get('content')
            blocks = content if isinstance(content, list) else [content]
            for block in blocks:
                if isinstance(block, dict) and block.get('tool_use_id'):
                    check(_content_text(block.get('content')), 'tool_result')
                else:
                    check(_text(block), 'user_message')
        elif kind == 'AssistantMessage':
            for block in payload.get('content') or []:
                if isinstance(block, dict) and 'input' in block:
                    check(_text(block.get('input')), 'assistant_tool_input')
                else:
                    check(_text(block), 'assistant_text')
        elif kind == 'ResultMessage':
            check(_text(payload), 'result')
        elif kind == 'SystemMessage':
            text = _text(payload)
            check(text, 'system_message:' + str(payload.get('subtype')))
            if payload.get('subtype') != 'init' and 'sentinel' in text.lower():
                system_hits.append({'subtype': payload.get('subtype'), 'text': text[:300]})
    return leaks, disk_reads, system_hits


def _bash_probes(events):
    """提取 env 探针与 deny 探针的执行情况。"""
    results = tool_results(events)
    bash = [call for call in tool_calls(events) if call['name'] == 'Bash']
    probes = {'env': None, **{f'deny_{_token(layer)}': None for layer in SETTINGS_LAYERS}}
    records = []
    for call in bash:
        command = str((call['input'] or {}).get('command', ''))
        outcome = results.get(call['id'])
        output = _content_text(outcome and outcome['content'])
        ok = outcome is not None and outcome['is_error'] is not True
        records.append({'command': command[:200], 'is_error': None if outcome is None else outcome['is_error'],
                        'output': output[:400]})
        if 'env' in command and 'sentinel' in command.lower() and probes['env'] is None:
            probes['env'] = {'ran': ok, 'output': output[:400],
                             'has_sentinel_var': bool(re.search(r'TF_SENTINEL_ENV', output))}
        for layer in SETTINGS_LAYERS:
            token = f'tf-deny-{_token(layer)}'
            key = f'deny_{_token(layer)}'
            if token in command and probes[key] is None:
                # 输出行里出现该词且调用成功 → deny 规则未生效。
                printed = any(line.strip() == token for line in output.splitlines())
                probes[key] = {'ran': ok, 'printed': printed, 'denied': not (ok and printed),
                               'combined': command.strip() != f'echo {token}', 'output': output[:200]}
    return probes, records


def _init_summary(init):
    summary = {key: init.get(key) for key in INIT_FIELDS if key in init}
    summary.update({key: init.get(key) for key in ('model', 'permissionMode', 'apiKeySource',
                                                   'claude_code_version', 'memory_paths') if key in init})
    return summary


def _listing(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file())


async def run(ctx) -> dict:
    table = _sentinels()
    index = _uuid_index(table)
    execution_id, workspace = sdk_workspace(ctx, _project_files(table))

    # 冻结副本中项目级哨兵是否完整进入 /workspace（.claude/ 与 .mcp.json 是否被排除）。
    project_paths = sorted(_project_files(table))
    missing_in_copy = [path for path in project_paths if not (workspace.source / path).is_file()]

    # 在 runner.start 之前写入用户级 / HOME 级 / 自动记忆哨兵。
    config_dir = workspace.config_dir or workspace.execution_dir / 'sdk-config'
    for relative, text in _user_files(table).items():
        path = config_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    config_before = _listing(config_dir)

    request = build_request(ctx, execution_id, workspace, PROMPT, role='coder')
    result, events, _runner = await run_execution(ctx, workspace, request)
    config_after = _listing(config_dir)

    # 1) init 字段：不得出现任何哨兵名称或 uuid。
    init = init_data(events)
    init_hits = {}
    for key in INIT_FIELDS:
        text = _text(init.get(key)).lower()
        hits = sorted({word for word in re.findall(r'[a-z0-9_.@/-]*sentinel[a-z0-9_.@/-]*', text)})
        hits += [layer + ':' + kind for value, (layer, kind) in index.items() if value.lower() in text]
        if hits:
            init_hits[key] = hits
    mcp_empty = init.get('mcp_servers') == []
    style_default = init.get('output_style') in {None, 'default'}

    # 2/3) Bash 探针。
    probes, bash_records = _bash_probes(events)
    env_probe = probes['env']
    deny_probes = {key: value for key, value in probes.items() if key.startswith('deny_')}

    # 4) hooks 是否触发（宿主侧文件）。
    hook_files = sorted(name for name in config_after if name.startswith('hook-fired-'))

    # 5) 流内 uuid 扫描。
    leaks, disk_reads, system_hits = _scan_stream(events, index)
    summary_text = result.summary + ' ' + _text(result.unresolved)
    summary_leaks = [f'{layer}:{kind}' for value, (layer, kind) in index.items()
                     if value in summary_text and kind != 'env'
                     and not any(item['layer'] == layer and item['kind'] == kind for item in disk_reads)]

    # 按层归因。
    per_layer = {}
    for layer in LAYERS:
        token = _token(layer)
        layer_leaks = [item for item in leaks if item['layer'] == layer]
        layer_leaks += [{'layer': layer, 'kind': item.split(':')[1], 'where': 'result.summary'}
                        for item in summary_leaks if item.startswith(layer + ':')]
        entry = {'stream_leaks': layer_leaks,
                 'init_name_hits': sorted({hit for hits in init_hits.values() for hit in hits
                                           if f'-{token}-' in hit or hit.startswith(layer + ':')})}
        if layer in SETTINGS_LAYERS:
            deny = deny_probes[f'deny_{token}']
            entry['deny_rule_applied'] = None if deny is None else deny['denied']
            entry['env_injected'] = bool(env_probe and f'TF_SENTINEL_ENV_{token.upper().replace("-", "_")}'
                                         in env_probe['output'])
            entry['hooks_fired'] = [name for name in hook_files if name.startswith(f'hook-fired-{token}-')]
        entry['loaded'] = bool(entry['stream_leaks'] or entry['init_name_hits'] or entry.get('deny_rule_applied')
                               or entry.get('env_injected') or entry.get('hooks_fired'))
        per_layer[layer] = entry

    status_ok = result.status in {'SUCCEEDED', 'FAILED', 'STOPPED'} and result.error_code not in {
        'SDK_RESULT_MISSING', 'SDK_EXECUTION_FAILED', 'SDK_PROCESS', 'SDK_CONNECTION', 'SDK_CLI_MISSING'}
    checks = {
        'init_present': bool(init),
        'project_sentinels_in_copy': not missing_in_copy,
        'init_no_sentinel': not init_hits,
        'mcp_servers_empty': mcp_empty,
        'output_style_default': style_default,
        'env_probe_observed': bool(env_probe and env_probe['ran']),
        'env_not_injected': bool(env_probe) and not env_probe['has_sentinel_var'],
        'deny_probes_observed': all(value is not None and value['ran'] is not None for value in deny_probes.values()),
        'deny_rules_not_applied': all(value is not None and not value['denied'] for value in deny_probes.values()),
        'hooks_not_fired': not hook_files,
        'no_instruction_uuid_leak': not leaks and not summary_leaks,
        'no_sentinel_system_messages': not system_hits,
        'execution_not_crashed': status_ok,
    }
    passed = all(checks.values())
    leaked_layers = [layer for layer, entry in per_layer.items() if entry['loaded']]

    # 内置 slash_commands / skills 来源判断依据：sdk-config 运行前只有本场景写入的哨兵文件，
    # 冻结源码亦无同名定义；plugins 标记为 builtin。
    # 只按定义文件的文件名/技能目录名比对（避免把 agents/ 目录名误判为 /agents 命令）。
    own_names = set()
    for path in config_before + project_paths:
        parts = path.split('/')
        own_names.add(parts[-1].rsplit('.', 1)[0].lower())
        if parts[-1] == 'SKILL.md' and len(parts) >= 2:
            own_names.add(parts[-2].lower())
    commands = list(init.get('slash_commands') or [])
    skills = list(init.get('skills') or [])
    builtin = {
        'slash_command_count': len(commands), 'skill_count': len(skills),
        'skills_subset_of_slash_commands': set(skills) <= set(commands),
        'names_defined_by_scenario_files': sorted(name for name in commands if name.lower() in own_names),
        'no_external_skill_sources': '运行前 sdk-config 仅含本场景哨兵文件，冻结源码无同名定义',
        'plugins': init.get('plugins'),
        'sdk_config_before_run': config_before,
        'sdk_config_created_by_cli': sorted(set(config_after) - set(config_before)),
    }

    return {
        'status': 'passed' if passed else 'failed',
        'checks': checks,
        'leaked_layers': leaked_layers,
        'per_layer': per_layer,
        'init_sentinel_hits': init_hits,
        'init': _init_summary(init),
        'bash_probes': probes,
        'bash_calls': bash_records,
        'hook_files': hook_files,
        'instruction_disk_reads': disk_reads,
        'system_message_hits': system_hits,
        'missing_in_copy': missing_in_copy,
        'builtin_commands_evidence': builtin,
        'model_self_report': result.summary,
        'result': result_summary(result),
        'unresolved': result.unresolved,
        'transcript_ref': str(workspace.execution_dir / 'transcript' / 'worker.jsonl'),
    }
