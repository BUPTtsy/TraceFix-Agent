"""Python 调用方显式创建 SDK Harness Job 的装配入口。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig, load_claude_sdk_config
from tracefix.execution.agent_runner import AgentExecutionRunner
from tracefix.execution.repository import plain_root
from tracefix.runtime.contracts import digest
from tracefix.runtime.harness import HarnessRuntime, local_repository_config
from tracefix.runtime.harness_finding import validate_finding_service_factory
from tracefix.storage.harness import PostgresHarnessStore
from tracefix.storage.harness_projection import project_pending


def restore_harness_runtime(engine, state, *, store=None, runner_factory=None):
    authority = store or PostgresHarnessStore(engine.store, engine.artifacts)
    job = authority.get_job(state.harness_job_id, scope_id=state.scope_id)
    sessions = authority.list_sessions(job.job_id, scope_id=state.scope_id)
    initializer = next((session for session in sessions if session.kind == 'initializer'), None)
    if initializer is None or not job.config_ref:
        raise PermissionError('SDK Job 缺少冻结配置或初始化 Session')
    frozen = engine.artifacts.json(state.scope_id, initializer.run_id, job.config_ref)
    config = ClaudeSdkConfig.model_validate(frozen['config'])
    if config.backend != job.backend or config.config_hash != job.config_digest:
        raise PermissionError('SDK Job 的冻结配置摘要不匹配')
    root = plain_root(Path(engine.artifacts.root).parent)

    def runner(workspace):
        return (runner_factory or AgentExecutionRunner)(workspace,
            network=os.environ.get('TRACEFIX_SDK_NETWORK', ''),
            egress_proxy=os.environ.get('TRACEFIX_SDK_EGRESS_PROXY', ''))

    def project(current, job_id, scope_id):
        return project_pending(current, root / 'harness-projection.sqlite3', job_id, scope_id)

    return HarnessRuntime(authority, runner, config, root / 'harness', projector=project)


def create_harness_job(engine, state, *, console_task_id, local_commit_authorized,
                       config=None, network=None, egress_proxy=None, projection_path=None,
                       store=None, runner_factory=None, final_verifier=None, rebind=None):
    config = (load_claude_sdk_config() if config is None else
              ClaudeSdkConfig.model_validate(config))
    if config.backend == 'legacy':
        return state
    if (state.mode != 'repair' or not local_repository_config(state.remote_config)
            or not local_commit_authorized):
        raise PermissionError('SDK Harness 只接受已授权本地提交且无远端发布的 repair 任务')
    if not state.test_spec_ref or not state.test_spec_hash:
        raise ValueError('SDK Harness 启动前必须冻结 TestSpec')
    spec = engine.get(state, state.test_spec_ref)
    if digest(spec) != state.test_spec_hash:
        raise PermissionError('SDK Harness 冻结 TestSpec 摘要不匹配')
    if not isinstance(console_task_id, str) or not console_task_id.strip():
        raise ValueError('SDK Harness 必须绑定明确 console Task')
    validate_finding_service_factory(engine, state)
    config.resolve_credential()
    network = os.environ.get('TRACEFIX_SDK_NETWORK', '') if network is None else network
    egress_proxy = os.environ.get('TRACEFIX_SDK_EGRESS_PROXY', '') if egress_proxy is None else egress_proxy
    if not isinstance(network, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', network):
        raise PermissionError('SDK Harness 必须配置受控出口网络')
    proxy = urlsplit(egress_proxy)
    if proxy.scheme not in {'http', 'https'} or not proxy.hostname or proxy.username or proxy.password:
        raise PermissionError('SDK Harness 必须配置无内嵌凭据的受控出口代理')
    if store is None:
        from psycopg import Connection
        connection = getattr(engine.store, 'conn', None)
        if not isinstance(connection, Connection) or connection.closed:
            raise RuntimeError('SDK Harness 生产入口必须连接真实 PostgreSQL 权威存储')
        store = PostgresHarnessStore(engine.store, engine.artifacts)
    root = plain_root(Path(engine.artifacts.root).parent)
    projection = plain_root(Path(projection_path)) if projection_path else root / 'harness-projection.sqlite3'

    def project(authority, job_id, scope_id):
        return project_pending(authority, projection, job_id, scope_id)

    factory = runner_factory or AgentExecutionRunner

    def runner(workspace):
        return factory(workspace, network=network, egress_proxy=egress_proxy)

    runtime = HarnessRuntime(store, runner, config, root / 'harness', projector=project,
                             final_verifier=final_verifier, rebind=rebind)
    state = runtime.attach(engine, state, console_task_id=console_task_id,
                           local_commit_authorized=local_commit_authorized)
    runtime.project(state)
    return state
