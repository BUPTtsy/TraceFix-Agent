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
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
            # CLI 对 Read/Edit 成功结果返回 is_error=None，仅 Bash 显式返回 False；只有 True 表示失败。
            if item.get('tool_use_id') and item.get('is_error') is not True:
                completed.add(item['tool_use_id'])
            for nested in item.values():
                walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return {calls[identity] for identity in completed if identity in calls}


CORE_PROMPT = ('这是一次能力探针。依次完成：1) 用 Read 读取 src/app.py；'
    '2) 用 Edit 把 `value = 1` 改为 `value = 2`；'
    '3) 用 Bash 执行 `cat src/app.py && python -c "import sys; print(sys.version)"`；'
    '4) 在结构化结果的 summary 中写明 Bash 输出的 Python 版本，unresolved 为空数组。')
SCENARIOS = CAPABILITIES[5:]
# 信息类场景：仅经 --scenario 显式运行，结果写入 report['informational']，不计入 gate_passed。
INFORMATIONAL = ('parallel_subagents',)
SOURCE_ROOT = Path(__file__).resolve().parents[2] / 'backend/packages/agent/src/tracefix'
# 生产 SDK 请求不绑定 MCP 时，允许侧不适用；拒绝侧仍须真实通过，否则保持 failed。
NOT_APPLICABLE = {'controlled_mcp': '生产 SDK 路径不绑定 MCP（仅 adapter 定义 mcp_servers 字段），'
    '受控 MCP 允许侧不属于当前门禁；拒绝侧越界绑定与出口预检仍真实验证。'}


def production_binds_mcp():
    """除 adapter 契约外，任何生产源码出现 mcp_servers 即视为已接入 SDK MCP，不再适用 not_applicable。"""
    return any('mcp_servers' in path.read_text(encoding='utf-8', errors='ignore')
               for path in SOURCE_ROOT.rglob('*.py') if path.name != 'claude_sdk_adapter.py')


def live_context(args):
    from sdk_live.common import LiveContext
    root = Path(args.work_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return LiveContext(config=load_claude_sdk_config(), network=args.network,
        egress_proxy=args.egress_proxy, root=root)


async def core_probe(args, report):
    from tracefix.execution.agent_runner import AgentExecutionRunner
    from tracefix.execution.candidate_export import SDKWorkspace, WorkspaceSnapshot
    if args.request or args.execution_dir:
        if not args.request or not args.execution_dir:
            raise ValueError('--request 与 --execution-dir 必须同时提供')
        request = SdkExecutionRequest.model_validate_json(Path(args.request).read_text(encoding='utf-8'))
        root = Path(args.execution_dir).resolve()
        metadata = json.loads((root / 'input_manifest.json').read_text(encoding='utf-8'))
        snapshot = WorkspaceSnapshot(**metadata['input_snapshot'])
        workspace = SDKWorkspace(root, root / 'source', root / 'input', snapshot,
            metadata['authority_snapshot'], metadata['allowed_files'], metadata['protected_paths'],
            tuple(metadata.get('exclusions', [])))
    else:
        # 未提供宿主冻结请求时，用一次性冻结源码夹具走同一生产 Runner。
        from sdk_live.common import build_request, sdk_workspace
        ctx = live_context(args).child('core')
        execution_id, workspace = sdk_workspace(ctx)
        request = build_request(ctx, execution_id, workspace, CORE_PROMPT)
        root = workspace.execution_dir
    events = []
    runner = AgentExecutionRunner(workspace, network=args.network, egress_proxy=args.egress_proxy,
                                  stream_messages=True)
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


async def scenario_probe(args, report, name):
    """运行 sdk_live/<name>.py 的 run(ctx)；单个场景异常不影响其他场景，并如实记为 failed。"""
    import importlib
    try:
        module = importlib.import_module('sdk_live.' + name)
    except ModuleNotFoundError as error:
        if error.name != 'sdk_live.' + name:
            raise
        report['capabilities'][name] = {'status': 'not_run', 'reason': '场景模块未实现'}
        return
    from sdk_live.common import redact
    ctx = live_context(args).child(name)
    try:
        outcome = await module.run(ctx)
    except Exception as error:
        outcome = {'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)[:500]}
    if name in INFORMATIONAL:
        report.setdefault('informational', {})[name] = redact(outcome, ctx.config)
        return
    if outcome.get('status') not in {'passed', 'failed', 'not_run'}:
        outcome = {'status': 'failed', 'reason': '场景返回的状态无效', 'raw': outcome}
    if name in NOT_APPLICABLE and outcome['status'] == 'not_run' and not production_binds_mcp():
        outcome = {**outcome, 'status': 'not_applicable', 'measured_status': 'not_run',
                   'not_applicable_reason': NOT_APPLICABLE[name]}
    report['capabilities'][name] = redact(outcome, ctx.config)


async def live_probe(args, report):
    selected = args.scenario or ['core', *SCENARIOS]
    for name in selected:
        if name == 'core':
            await core_probe(args, report)
        else:
            await scenario_probe(args, report, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='artifacts/harness-migration/M0-capabilities.json')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--request')
    parser.add_argument('--execution-dir')
    parser.add_argument('--network', default='')
    parser.add_argument('--egress-proxy', default='')
    parser.add_argument('--work-dir', default='.tmp-sdk-live/probe',
        help='未提供 --request 时冻结源码夹具与 execution 目录的根（应位于仓库 gitignore 范围内）')
    parser.add_argument('--scenario', action='append', choices=['core', *SCENARIOS, *INFORMATIONAL],
        help='只运行指定场景，可重复；默认运行 core 与全部能力场景，信息类场景须显式指定且不计入门禁')
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
    report['gate_passed'] = all(item['status'] in {'passed', 'not_applicable'}
                                for item in report['capabilities'].values())
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(target), 'gate_passed': report['gate_passed']}, ensure_ascii=False))
    return 0 if report['gate_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
