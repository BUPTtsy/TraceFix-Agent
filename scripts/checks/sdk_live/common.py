"""真实 SDK 场景共享夹具：冻结源码、SDK 副本、请求构造、生产 Runner 调用与事件解析。

场景模块约定：
- `async def run(ctx: LiveContext) -> dict`，返回 {'status': 'passed'|'failed'|'not_run', ...证据}。
- 只通过 `sdk_workspace` / `build_request` / `run_execution` 走生产 AgentExecutionRunner；
  需要生产接口未暴露的能力时，在报告中说明并返回 failed/not_run，不得放宽隔离参数。
"""

from __future__ import annotations

import inspect
import json
import os
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from tracefix.agents.claude_sdk_adapter import SdkExecutionRequest
from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.execution.agent_runner import AgentExecutionRunner
from tracefix.execution.candidate_export import SDKWorkspace
from tracefix.execution.repository import file_manifest, repository_binding, safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace, git
from tracefix.runtime.contracts import digest

CODER_TOOLS = ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash', 'Agent']
SYSTEM_PROMPT = '按冻结目标执行。无法继续时报告 blocker；不得提问或更改验收。'
DEFAULT_FILES = {'src/app.py': 'value = 1\n', '.gitignore': 'build/\n'}


@dataclass(frozen=True)
class LiveContext:
    config: ClaudeSdkConfig
    network: str
    egress_proxy: str
    root: Path
    code: str = 'probe'

    def child(self, code: str):
        root = self.root / code
        root.mkdir(parents=True, exist_ok=True)
        return replace(self, root=root, code=code)


def redact(value, config: ClaudeSdkConfig | None = None):
    """移除报告中的模型密钥正文。"""
    encoded = json.dumps(value, ensure_ascii=False, default=str)
    if config is not None and config.credential_ref.startswith('env:'):
        secret = os.environ.get(config.credential_ref[4:], '')
        if secret:
            encoded = encoded.replace(secret, '[REDACTED]')
    return json.loads(encoded)


def authority_workspace(root: Path, files: dict[str, str]):
    """创建带仓库身份快照的冻结源码，与 harness 使用的 Workspace 契约一致。"""
    root.mkdir(parents=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    git(root, 'init', '-q', '--initial-branch=main')
    digests = {relative: digest((root / relative).read_bytes()) for relative in files}
    safe_index_files(root, sorted(digests))
    commit_workspace(root, 'SDK 能力场景冻结源码')
    authority = Workspace(root, sorted({*files, 'src/**', 'tests/**'}))
    authority.prepare_mountpoints(create=True)
    binding = repository_binding(root)
    authority.repository_snapshot = {'repository_version': 1, 'scope_id': 'scope', 'repo_id': 'repo',
        'source_root': str(root), 'source_repository': binding, 'source_tree': binding['tree'],
        'workspace_root': str(root), 'workspace_repository': binding, 'commit': authority.head(),
        'files': digests, 'entries': file_manifest(root), 'subdir': ''}
    return authority


def sdk_workspace(ctx: LiveContext, files: dict[str, str] | None = None):
    """返回 (execution_id, SDKWorkspace)；execution 目录名与 execution_id 相同，Runner 不会再 fork。"""
    execution_id = f'{ctx.code}-{uuid.uuid4().hex[:10]}'
    authority = authority_workspace(ctx.root / ('a-' + execution_id[-10:]), files or DEFAULT_FILES)
    return execution_id, SDKWorkspace.create(authority, ctx.root / execution_id)


def build_request(ctx: LiveContext, execution_id: str, workspace: SDKWorkspace, prompt: str, *,
                  role='coder', tools=None, **updates):
    values = dict(execution_id=execution_id, session_id='session-' + execution_id,
        input_hash=workspace.input_snapshot.content_hash, role=role, prompt=prompt,
        workspace=workspace.source, config=ctx.config, tools=list(tools or CODER_TOOLS),
        system_prompt=SYSTEM_PROMPT)
    values.update(updates)
    return SdkExecutionRequest(**values)


def runner_for(ctx: LiveContext, workspace: SDKWorkspace):
    # 探针需逐条观察工具调用与实时停止时机，显式开启消息流。
    return AgentExecutionRunner(workspace, network=ctx.network, egress_proxy=ctx.egress_proxy,
                                stream_messages=True)


async def run_execution(ctx: LiveContext, workspace: SDKWorkspace, request, *, on_event=None, runner=None):
    """经生产 Runner 启动 worker；返回 (result, events, runner)。on_event 可为协程。"""
    events = []
    runner = runner or runner_for(ctx, workspace)

    async def emit(kind, data):
        events.append({'type': kind, 'data': data})
        if on_event is not None:
            response = on_event(kind, data)
            if inspect.isawaitable(response):
                await response

    result = await runner.start(request, emit)
    return result, events, runner


def messages(events, kind=None):
    """按 SDK 消息类型（AssistantMessage / UserMessage / SystemMessage / ResultMessage）迭代 payload。"""
    for event in events:
        if event.get('type') != 'sdk.message':
            continue
        envelope = event.get('data') or {}
        if kind is None or envelope.get('type') == kind:
            yield envelope.get('payload') or {}


def init_data(events):
    for payload in messages(events, 'SystemMessage'):
        if payload.get('subtype') == 'init':
            return payload.get('data') or {}
    return {}


def tool_calls(events):
    """[{id, name, input, parent}]；parent 为子 Agent 调用所属的 Task/Agent tool_use id。"""
    calls = []
    for payload in messages(events, 'AssistantMessage'):
        for block in payload.get('content') or []:
            if isinstance(block, dict) and {'id', 'name', 'input'} <= block.keys():
                calls.append({'id': block['id'], 'name': block['name'], 'input': block['input'],
                              'parent': payload.get('parent_tool_use_id')})
    return calls


def tool_results(events):
    """{tool_use_id: {is_error, content, parent}}；CLI 成功结果的 is_error 可能是 None，只有 True 表示失败。"""
    results = {}
    for payload in messages(events, 'UserMessage'):
        for block in payload.get('content') or [] if isinstance(payload.get('content'), list) else []:
            if isinstance(block, dict) and block.get('tool_use_id'):
                results[block['tool_use_id']] = {'is_error': block.get('is_error'),
                    'content': block.get('content'), 'parent': payload.get('parent_tool_use_id')}
    return results


def succeeded(call, results):
    outcome = results.get(call['id'])
    return outcome is not None and outcome['is_error'] is not True


def result_summary(result):
    return {'execution_id': result.execution_id, 'status': result.status, 'error_code': result.error_code,
            'sdk_session_id': result.sdk_session_id, 'summary': result.summary}
