"""保存 SDK 能力证据；真实服务或专项证据缺失时门禁保持未通过。"""

import argparse
import asyncio
import importlib.metadata
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend/packages/agent/src'))

from tracefix.agents.claude_sdk_adapter import SdkExecutionRequest
from tracefix.agents.claude_sdk_config import CLI_VERSION, SDK_VERSION, load_claude_sdk_config


CAPABILITIES = ('session_id', 'native_read', 'native_edit', 'native_bash', 'structured_output',
    'explicit_tool_restriction', 'readonly_subagent', 'interrupt', 'cross_process_resume',
    'controlled_mcp', 'settings_isolation', 'checkpoint_scope')


def tool_names(value):
    names = set()
    if isinstance(value, dict):
        if value.get('type') == 'tool_use' or ('id' in value and 'input' in value and 'name' in value):
            names.add(value.get('name', ''))
        for item in value.values():
            names.update(tool_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(tool_names(item))
    return names


def successful_tools(value):
    calls, completed = {}, set()

    def walk(item):
        if isinstance(item, dict):
            if 'id' in item and 'input' in item and 'name' in item:
                calls[item['id']] = item['name']
            if item.get('tool_use_id') and item.get('is_error') is False:
                completed.add(item['tool_use_id'])
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return {calls[identity] for identity in completed if identity in calls}


async def live_probe(args, report):
    from tracefix.execution.agent_runner import AgentExecutionRunner
    from tracefix.execution.candidate_export import SDKWorkspace, WorkspaceSnapshot
    if not args.request or not args.execution_dir:
        raise ValueError('--live 必须提供宿主冻结的 --request 和 --execution-dir')
    request = SdkExecutionRequest.model_validate_json(Path(args.request).read_text(encoding='utf-8'))
    root = Path(args.execution_dir).resolve()
    metadata = json.loads((root / 'input_manifest.json').read_text(encoding='utf-8'))
    snapshot = WorkspaceSnapshot(**metadata['input_snapshot'])
    workspace = SDKWorkspace(root, root / 'source', root / 'input', snapshot,
        metadata['authority_snapshot'], metadata['allowed_files'], metadata['protected_paths'],
        tuple(metadata.get('exclusions', [])))
    events = []
    runner = AgentExecutionRunner(workspace, network=args.network, egress_proxy=args.egress_proxy)
    result = await runner.start(request, lambda event, data: events.append({'type': event, 'data': data}))
    report['execution'] = {'execution_id': result.execution_id, 'status': result.status,
        'error_code': result.error_code, 'sdk_session_id': result.sdk_session_id,
        'transcript_ref': str(root / 'transcript' / 'worker.jsonl')}
    observed = tool_names(events)
    succeeded = successful_tools(events)
    checks = report['capabilities']
    checks['session_id'] = {'status': 'passed' if result.sdk_session_id else 'failed',
        'observed': bool(result.sdk_session_id)}
    for capability, name in [('native_read', 'Read'), ('native_edit', 'Edit'), ('native_bash', 'Bash')]:
        checks[capability] = {'status': 'passed' if name in succeeded else 'failed',
            'observed_tool': name if name in observed else None,
            'successful_tool_receipt': name in succeeded}
    checks['structured_output'] = {'status': 'passed' if result.structured_output is not None else 'failed'}
    report['observed_tool_names'] = sorted(observed)
    report['remaining_live_scenarios'] = {
        'explicit_tool_restriction': '须单独请求未暴露的 Write/AskUserQuestion 并确认工具不存在或被拒绝',
        'readonly_subagent': '须验证子 Agent 无 Bash/Edit/Write，并由宿主检查哨兵文件不变',
        'interrupt': '须在真实工具执行中发送 SDK interrupt，并另由 Runner 确认所有写入者停止',
        'cross_process_resume': '须退出首个 worker 后加载同一配置目录与 SDK session，再验证实际文件连续',
        'controlled_mcp': '须部署允许/拒绝两个 MCP 并校验宿主端策略与操作回执',
        'settings_isolation': '须放置带哨兵的用户/项目配置，证明 setting_sources=[] 未加载',
        'checkpoint_scope': '须对主 Agent Edit、Bash、子 Agent 修改执行 rewind，检查完整快照恢复范围'}
    for name, reason in report['remaining_live_scenarios'].items():
        checks[name] = {'status': 'not_run', 'reason': reason}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='artifacts/harness-migration/M0-capabilities.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--request')
    parser.add_argument('--execution-dir')
    parser.add_argument('--network', default='')
    parser.add_argument('--egress-proxy', default='')
    args = parser.parse_args()
    report = {'schema_version': 'tracefix.sdk-capabilities/1',
        'checked_at': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
        'python_version': platform.python_version(), 'required_sdk_version': SDK_VERSION,
        'required_cli_version': CLI_VERSION, 'live_requested': args.live,
        'capabilities': {name: {'status': 'not_run', 'reason': '尚未执行真实受控 SDK 场景'}
                         for name in CAPABILITIES}, 'gate_passed': False,
        'version_evidence': {'pypi': f'https://pypi.org/pypi/claude-agent-sdk/{SDK_VERSION}/json',
            'cli': f'https://github.com/anthropics/claude-agent-sdk-python/blob/v{SDK_VERSION}/src/claude_agent_sdk/_cli_version.py'},
        'checkpoint_contract': {'tracked': ['Write', 'Edit', 'NotebookEdit'],
            'not_fully_tracked': ['Bash', 'subagent edits', 'directory operations'],
            'requires_full_workspace_snapshot': True,
            'source': 'https://platform.claude.com/docs/en/agent-sdk/file-checkpointing'}}
    try:
        report['installed_sdk_version'] = importlib.metadata.version('claude-agent-sdk')
    except importlib.metadata.PackageNotFoundError:
        report['installed_sdk_version'] = None
    try:
        configuration = load_claude_sdk_config()
        report['configured_backend'] = configuration.backend
        report['configuration_valid'] = configuration.backend == 'claude_sdk'
    except ValueError:
        report['configuration_valid'] = False
        report['configuration_error'] = 'SDK 模型、凭据引用、固定镜像或受控出口配置缺失/无效'
    if args.live:
        try:
            asyncio.run(live_probe(args, report))
        except Exception as error:
            report['live_error_type'] = type(error).__name__
            report['live_error'] = '真实 SDK 场景未完成；检查冻结请求、worker 镜像、凭据、出口与 Docker 服务'
    else:
        for item in report['capabilities'].values():
            item['reason'] = '未启用 --live；离线测试不能代替真实 SDK 能力门禁'
    report['gate_passed'] = all(item['status'] == 'passed' for item in report['capabilities'].values())
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(target), 'gate_passed': report['gate_passed']}, ensure_ascii=False))
    return 0 if report['gate_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
