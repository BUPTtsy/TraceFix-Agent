"""Web 控制台使用的内部运行会话与资源生命周期。"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path

from tracefix.console import dispatch, project_catalog
from tracefix.config import Profile, load_profile, load_projects
from tracefix.execution.browser import MCPBrowser
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.documents import DocumentLibrary, timestamp
from tracefix.knowledge.retrieval import EmbeddingAdapter, Retriever
from tracefix.knowledge.memory import MemoryLibrary
from tracefix.knowledge.scope import ScopeResolver
from tracefix.model.gateway import Gateway
from tracefix.messages import error_message
from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus, TestSpec, digest, new_id
from tracefix.runtime.engine import Engine
from tracefix.runtime.continuation import continuation_state
from tracefix.rules import RuleLibrary, RuleResolver
from tracefix.remote import RemoteConfigStore, effective, prepare_checkout
from tracefix.repository_registry import RepositoryRegistry
from tracefix.storage.artifacts import Artifacts, redact, sanitize
from tracefix.storage.store import PostgresStore


def configured_worker_model():
    """仅实际覆盖时创建 Worker Gateway，未覆盖字段继承 Supervisor 环境配置。"""
    configuration = {
        'base_url': os.getenv('TRACEFIX_WORKER_BASE_URL'),
        'key': os.getenv('TRACEFIX_WORKER_API_KEY'),
        'text_model': os.getenv('TRACEFIX_WORKER_TEXT_MODEL'),
        'vision_model': os.getenv('TRACEFIX_WORKER_VISION_MODEL'),
    }
    if not any(configuration.values()):
        return None
    # 独立配置中显式空视觉模型关闭图片；省略该项仍继承 Supervisor。
    return Gateway(**configuration)


class Session:
    def __init__(self, args, store, saver):
        self.args, self.store, self.saver = args, store, saver
        self.scopes = ScopeResolver(load_projects(Path(args.projects)), Path(args.projects))
        from tracefix.rules.codegraph import prepare_project_index
        self.codegraph_indexes = {identifier: prepare_project_index(self.scopes.projects, identifier, Path(args.data))
                                 for identifier in self.scopes.projects}
        self.scope = args.project
        self.ctx = self.scopes.context(self.scope)
        self.artifacts = Artifacts(Path(args.data)/'artifacts', self.evidence_damaged)
        self.engine, self.task, self.run_id = None, None, None
        self.stop_requested = False
        self.goal, self.mode = args.goal or '', args.mode
        requested_execution_mode = getattr(args, 'execution_mode', None)
        self.execution_mode = requested_execution_mode or 'batch'
        self.history_generation = 0
        self.console_run_id = os.getenv('TRACEFIX_CONSOLE_RUN_ID')
        self.web_console_run_id = self.console_run_id
        self.documents = DocumentLibrary(getattr(args, 'console_db', None))
        self.runtime_stack = None
        self.runtime_services = None
        self.continuation_parent = None
        self.continuation_instruction = None
        self.parent_run_id = getattr(args, 'parent_run', None)
        self.additional_rule_ids = list(getattr(args, 'rule', None) or [])
        self.remote_store = RemoteConfigStore(Path(args.data) / 'remote-config.json')
        session_remote = os.getenv('TRACEFIX_SESSION_REMOTE')
        self.session_remote = {self.scope: json.loads(session_remote)} if session_remote else {}

    def remote_snapshot(self):
        project = self.remote_store.get(self.scope)
        session = self.session_remote.get(self.scope)
        return {'projectId': self.scope, 'project': project, 'session': session,
                'effective': effective(project, session)}

    def set_remote(self, value, scope):
        if scope == 'project':
            self.remote_store.set(self.scope, value)
        else:
            self.session_remote[self.scope] = dict(value)

    def clear_remote(self, scope):
        if scope == 'project':
            self.remote_store.clear(self.scope)
        else:
            self.session_remote.pop(self.scope, None)

    async def ensure_runtime(self):
        if self.store is not None:
            return
        from tracefix.storage.checkpoints import postgres_checkpointer
        dsn = getattr(self.args, 'database_url', None) or os.getenv('TRACEFIX_DATABASE_URL', '')
        if not dsn:
            raise ValueError('请配置 TRACEFIX_DATABASE_URL 才能执行 Test / Repair；项目、知识库和 Chat 可独立使用')
        resources = AsyncExitStack()
        try:
            store = PostgresStore(dsn)
            resources.callback(store.close)
            store.setup()
            saver = await resources.enter_async_context(postgres_checkpointer(dsn))
            await self.runtime_stack.enter_async_context(resources)
        except BaseException:
            await resources.aclose()
            raise
        self.store, self.saver = store, saver

    def select_project(self, project_id):
        if self.active():
            raise ValueError('活动 Run 期间不能切换项目')
        scopes = ScopeResolver(load_projects(Path(self.args.projects)), Path(self.args.projects))
        context = scopes.context(project_id)
        project = next(record for record in project_catalog(self.args.projects) if record['id'] == project_id)
        self.scopes, self.ctx, self.scope = scopes, context, project_id
        self.args.project = project_id
        self.args.profile = project['profile']
        self.args.url = None
        self.args.spec = None
        self.goal, self.run_id, self.engine, self.console_run_id = '', None, None, None
        self.history_generation += 1

    def project_profile(self):
        if not self.args.profile:
            project = next(record for record in project_catalog(self.args.projects) if record['id'] == self.scope)
            if not project['profile']:
                raise ValueError('当前项目缺少有效 Profile')
            self.args.profile = project['profile']
        profile = load_profile(Path(self.args.profile))
        if profile.project != self.scope:
            raise PermissionError('Repo Profile 与当前项目不匹配；请提供该项目的 Profile')
        return profile

    def begin_console_run(self, state=None):
        existing = next((record for record in self.documents.runs(self.scope)
                         if state is not None and record.get('agentRunId') == state.run_id), None)
        if existing:
            self.console_run_id = existing['id']
            return
        if self.web_console_run_id:
            record = self.documents.run(self.web_console_run_id)
            if record['projectId'] != self.scope:
                raise PermissionError('Web 运行记录不属于当前项目')
            self.console_run_id, self.web_console_run_id = record['id'], None
            return
        self.console_run_id = new_id('console')
        fields = {'projectId': self.scope, 'goal': state.goal if state else self.goal,
                  'mode': state.mode if state else self.mode, 'status': str(state.run_status).lower() if state else 'running',
                  'executionMode': state.execution_mode if state else self.execution_mode,
                  'phase': str(state.phase) if state else 'STARTING', 'origin': 'service', 'pid': os.getpid(),
                  'dataRoot': str(Path(self.args.data).resolve()), 'finishedAt': None,
                  'profile': self.args.profile, 'registry': str(Path(self.args.projects).resolve())}
        if state:
            fields['agentRunId'] = state.run_id
        if self.continuation_parent:
            fields['parentRunId'] = self.continuation_parent
            fields['continuationInstruction'] = self.continuation_instruction
        if self.parent_run_id:
            fields['parentRunId'] = self.parent_run_id
        if self.additional_rule_ids:
            fields['additionalRuleIds'] = self.additional_rule_ids
        self.documents.update_run(self.console_run_id, redact(fields), create=True)

    def publish_console(self, event=None, error=None):
        if not self.console_run_id or not self.run_id:
            return
        state = self.state()
        changes = {'agentRunId': state.run_id, 'goal': state.goal, 'status': str(state.run_status).lower(),
                   'executionMode': state.execution_mode,
                   'repairMode': state.repair_mode, 'repairModeRequested': state.repair_mode_requested,
                   'repairModeReason': state.repair_mode_reason,
                   'phase': str(state.phase), 'outcome': state.outcome, 'reportRef': state.report_ref,
                   'branch': state.local_branch, 'error': error or state.error}
        if state.harness_job_id and self.engine.harness is not None:
            job = self.engine.harness.job(state)
            changes['agentRunId'] = self.run_id
            changes.update(coding_backend='claude_sdk', harness_job_id=job.job_id,
                current_run_id=state.run_id,
                harness_session_id=state.harness_session_id, control_status=job.status,
                can_resume=False, resume_blockers=[job.blocker] if job.blocker else [],
                accepted_commit=job.accepted_commit, quality=job.quality)
            if job.status in {'STOPPED', 'STOPPING', 'BLOCKED'}:
                changes['status'] = job.status.lower()
        changes.update({'continuationCount': state.continuation_count,
                        'abnormalTermination': state.abnormal_termination,
                        'continuationMarkers': state.continuation_markers,
                        'continuationInstruction': state.continuation_instruction})
        if state.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL, RunStatus.FAILED, RunStatus.SUPERSEDED} or error:
            changes['finishedAt'] = timestamp()
        else:
            changes['finishedAt'] = None
        if error and not state.harness_job_id:
            changes['status'] = str(state.run_status).lower()
        changes['pid'] = os.getpid() if state.run_status in {
            RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.WAITING_INPUT, RunStatus.WAITING_APPROVAL} and not error else None
        knowledge = event.get('payload') if event and event['type'] == 'knowledge.selected' else None
        self.documents.update_run(self.console_run_id, redact(changes), knowledge=knowledge)

    def transfer_console_run(self, previous, child):
        """Keep Web process ownership attached to a derived Run."""
        if not self.console_run_id:
            self.begin_console_run(child)
            return
        # update_run 会在旧记录仍有 stopRequestedAt 时强制状态为 stopping；先清理旧请求。
        self.documents.update_run(self.console_run_id, {
            'stopRequestedAt': None, 'stopPreviousStatus': None, 'stopError': None,
        })
        self.documents.update_run(self.console_run_id, redact({
            'agentRunId': child.run_id,
            'goal': child.goal,
            'status': str(child.run_status).lower(),
            'executionMode': child.execution_mode,
            'phase': str(child.phase),
            'outcome': None,
            'reportRef': None,
            'branch': child.local_branch,
            'error': None,
            'finishedAt': None,
            'pid': os.getpid(),
            'stopRequestedAt': None,
            'stopPreviousStatus': None,
            'stopError': None,
            'parentRunId': child.parent_run_id or previous.run_id,
            'continuationInstruction': child.continuation_instruction,
            'continuationCount': child.continuation_count,
            'continuationMarkers': child.continuation_markers,
            'abnormalTermination': child.abnormal_termination,
        }))

    def evidence_damaged(self, report):
        """旧证据副本损坏不能静默跳过：写入 Web 事件流留痕。"""
        if self.engine and self.run_id:
            self.engine.event(self.state(), 'evidence.damaged', report)

    def notify(self, event):
        if self.console_run_id:
            self.documents.append_event(self.console_run_id, event)
        if event['type'] in {'state.changed', 'knowledge.selected', 'run.finished'}:
            self.publish_console(event)

    def active(self):
        if self.task and not self.task.done():
            return True
        if self.run_id:
            return self.store.load(self.run_id, self.scope).run_status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL, RunStatus.FAILED, RunStatus.SUPERSEDED}
        return False

    def state(self):
        if not self.run_id:
            raise ValueError('当前没有 Run')
        state = self.store.load(self.run_id, self.scope)
        current_run = getattr(getattr(self, 'engine', None), 'current_run', None)
        if getattr(state, 'harness_job_id', None) and current_run and current_run != state.run_id:
            current = self.store.load(current_run, self.scope)
            if current.harness_job_id != state.harness_job_id:
                raise PermissionError('当前增量不属于原 SDK Job')
            return current
        return state

    async def create(self):
        if self.active():
            raise ValueError('当前 Run 尚未结束')
        if not self.goal.strip():
            raise ValueError('测试目标不能为空')
        self.begin_console_run()
        self.run_id, self.engine, self.task = None, None, None
        self.stop_requested = False
        try:
            await self.create_agent()
            self.continuation_parent = None
            self.continuation_instruction = None
        except Exception as error:
            record = self.documents.run(self.console_run_id)
            changes = {'error': error_message(error), 'logs': [sanitize(error_message(error)) + '\n']}
            if record.get('origin') != 'web':
                changes.update(status='failed', finishedAt=timestamp(), pid=None)
            self.documents.update_run(self.console_run_id, redact(changes))
            raise

    async def continue_task(self, run_id, instruction):
        if self.active():
            raise ValueError('当前已有活动 Run')
        records = dispatch('runs', {'projectId': self.scope}, library=self.documents,
            projects_path=self.args.projects, data_root=self.args.data)
        record = next((item for item in records if run_id in {item['id'], item.get('agentRunId')}), None)
        if record is None:
            raise ValueError('当前项目没有此运行记录')
        record = self.documents.run(record['id'])
        if record.get('agentRunId'):
            await self.ensure_runtime()
            previous = self.store.load(record['agentRunId'], self.scope)
            if previous.harness_job_id or previous.coding_backend == 'claude_sdk':
                raise ValueError('SDK Job 不接受补充指令，请通过控制台恢复任务')
            if previous.repo_snapshot_ref:
                self.validate_workspace(previous, data_root=record.get('dataRoot'))
        claim = os.getenv('TRACEFIX_CONTINUATION_ID')
        if claim and self.web_console_run_id == record['id']:
            if record.get('continuationId') != claim or record['status'] not in {'running', 'stopping'}:
                raise ValueError('Web 续执行请求已失效')
            instruction = record['continuationInstruction']
        else:
            record = dispatch('run.continue', {'id': record['id'], 'instruction': instruction},
                library=self.documents, projects_path=self.args.projects, data_root=self.args.data)
            instruction = record['continuationInstruction']
        self.console_run_id = record['id']
        self.web_console_run_id = None
        self.run_id, self.engine, self.task = None, None, None
        self.stop_requested = False
        self.goal, self.mode = record['goal'], record['mode']
        self.args.data = record.get('dataRoot') or self.args.data
        self.args.profile = record.get('profile') or self.args.profile
        try:
            self.artifacts = Artifacts(Path(self.args.data) / 'artifacts', self.evidence_damaged)
            await self.ensure_runtime()
            previous = self.store.load(record['agentRunId'], self.scope) if record.get('agentRunId') else None
            requested_execution_mode = getattr(self.args, 'execution_mode', None)
            if requested_execution_mode:
                self.execution_mode = requested_execution_mode
            elif getattr(self.args, 'continue_run', None):
                self.execution_mode = 'batch'
            else:
                self.execution_mode = record.get('executionMode') or (
                    previous.execution_mode if previous else self.execution_mode)
            continued = None
            if previous:
                markers = record['continuationMarkers']
                markers[-1].update(previous_revision=previous.revision,
                    previous_state_ref=self.artifacts.put(self.scope, previous.run_id,
                        redact(previous.model_dump(mode='json')), label='继续执行前状态'),
                    previous_usage=previous.budget.model_dump())
                self.documents.update_run(record['id'], {'continuationMarkers': markers})
                continued = continuation_state(previous, instruction, markers,
                    process_ended=markers[-1]['previous_status'] in {'failed', 'cancelled'})
                continued.execution_mode = self.execution_mode
            await self.create_agent(state=continued, reuse_workspace=previous is not None)
        except Exception as error:
            self.documents.update_run(record['id'], redact({'status': 'failed', 'phase': 'FINALIZE',
                'error': error_message(error), 'finishedAt': timestamp(), 'pid': None}))
            raise

    async def create_agent(self, state=None, reuse_workspace=False, *, frozen_snapshot=None, start=True):
        await self.ensure_runtime()
        if not os.getenv('TRACEFIX_API_KEY'):
            raise ValueError('请设置 TRACEFIX_API_KEY 环境变量')
        project = self.scopes.projects[self.scope]
        managed = bool(project.repository_config and project.repository_config.get('version') == 1)
        profile = (Profile.model_validate(project.repository_config['profile'])
                   if managed else self.project_profile())
        if managed and not reuse_workspace and not project.repository_config['enabled']:
            raise PermissionError('仓库已禁用，不能启动新 Run')
        remote = state.remote_config if reuse_workspace else None if managed else effective(self.remote_store.get(self.scope), self.session_remote.get(self.scope))
        if remote and not reuse_workspace:
            project = self.scopes.projects[self.scope]
            destination = Path(remote.get('destination') or project.root)
            if not destination.is_absolute():
                destination = Path.cwd() / destination
            result = prepare_checkout({**remote, 'source_commit': profile.source_commit}, destination)
            project.root = Path(result['destination'])
        url = state.url if reuse_workspace else self.args.url or profile.url
        Policy(profile.allowed_origins).url(url)
        if state is None:
            state = RunState(run_id=(project.repository_config.get('frozen_run_id') if managed else None) or new_id('run'),
                             scope_id=self.scope, goal=self.goal, url=url, mode=self.mode,
                             execution_mode=self.execution_mode,
                             parent_run_id=self.continuation_parent or self.parent_run_id,
                             continuation_instruction=self.continuation_instruction,
                             remote_config=remote, additional_rule_ids=self.additional_rule_ids)
            record = self.documents.run(self.console_run_id)
            state.continuation_markers = record.get('continuationMarkers', [])
            state.continuation_count = len(state.continuation_markers)
            state.abnormal_termination = bool(state.continuation_count)
            state.continuation_instruction = record.get('continuationInstruction')
        if managed and not reuse_workspace:
            if frozen_snapshot is not None:
                if (frozen_snapshot.get('repository_config') != project.repository_config
                        or project.repository_config.get('frozen_run_id') != state.run_id):
                    raise PermissionError('服务冻结快照与 Run/项目配置不一致')
                snapshot = frozen_snapshot
            else:
                snapshot = RepositoryRegistry(Path(self.args.projects), data_root=Path(self.args.data)).freeze(
                    self.scope, state.run_id)
            state.remote_config = snapshot['repository_config']
            self.args.projects = snapshot['projects_path']
            self.args.profile = snapshot['profile_path']
            self.scopes = ScopeResolver(load_projects(Path(self.args.projects)), Path(self.args.projects))
            self.ctx = self.scopes.context(self.scope)
            profile = Profile.model_validate(snapshot['repository_config']['profile'])
        elif managed and reuse_workspace:
            expected = state.remote_config or {}
            current = project.repository_config
            if (expected.get('repo_id') != current.get('repo_id')
                    or expected.get('config_digest') != current.get('config_digest')
                    or expected.get('commit') != current.get('commit')
                    or expected.get('root') != current.get('root')):
                raise PermissionError('Run 仓库配置快照与恢复配置不一致')
        workspace_path = Path(self.args.data)/'workspaces'/state.run_id
        if reuse_workspace:
            workspace, source = self.validate_workspace(state)
            snapshot = self.artifacts.json(self.scope, state.run_id, state.memory_snapshot_ref)
            if snapshot['epochs'] != [list(entry) for entry in self.ctx.epochs]:
                raise PermissionError('项目权限已变化，请检查原任务的作用域')
        else:
            workspace, source = Workspace.export(self.scopes, self.ctx, profile.source_commit, workspace_path)
            if state.inherited_evidence_refs:
                state.evidence_refs = []
                state.inherited_evidence_refs = []
                state.reproduction_plan_frozen = False
                state.reproduced = False
                state.replay_plan_ref = None
            state.source_manifest = digest(source)
            state.repo_snapshot_ref = self.artifacts.put(self.scope, state.run_id, source, label='源码快照')
            state.memory_snapshot_ref = self.artifacts.put(self.scope, state.run_id, dataclasses.asdict(self.ctx), label='上下文快照')
        instructions_path = self.scopes.projects[self.scope].agent_instructions
        instructions_file = workspace.path(instructions_path)
        if instructions_file.exists() and not reuse_workspace:
            if not instructions_file.is_file() or instructions_file.stat().st_size > 65_536:
                raise ValueError('AGENTS.md 必须是 UTF-8 普通文件，大小不能超过 64 KiB')
            try:
                instructions = sanitize(instructions_file.read_bytes().decode('utf-8'))
            except UnicodeDecodeError as error:
                raise ValueError('AGENTS.md 必须使用 UTF-8 编码') from error
            state.agent_instructions_ref = self.artifacts.put(self.scope, state.run_id, instructions, 'txt', label='项目约束')
            state.agent_instructions_hash = digest(instructions.encode())
            state.agent_instructions_path = instructions_path
        if self.args.spec and not reuse_workspace:
            spec = TestSpec.model_validate_json(Path(self.args.spec).read_text(encoding='utf-8'))
            state.test_spec_hash = digest(spec)
            state.test_spec_ref = self.artifacts.put(self.scope, state.run_id, spec.model_dump(), label='测试规范')
        self.bind(state, profile, workspace, source)
        if not reuse_workspace and state.mode == 'repair':
            from tracefix.agents.claude_sdk_config import load_claude_sdk_config
            requested_backend = getattr(self.args, 'coding_backend', None)
            configuration = load_claude_sdk_config({**os.environ,
                **({'TRACEFIX_CODING_BACKEND': requested_backend} if requested_backend is not None else {})})
            if configuration.backend == 'claude_sdk':
                state.coding_backend = 'claude_sdk'
                state.coding_config_ref = self.artifacts.put(self.scope, state.run_id,
                    configuration.model_dump(mode='json'), label='SDK冻结配置')
        self.store.save(state)
        self.publish_console()
        if start:
            self.task = asyncio.create_task(self.drive(state))
        return state

    def validate_workspace(self, state, workspace=None, source=None, data_root=None):
        source = source if source is not None else self.artifacts.json(self.scope, state.run_id, state.repo_snapshot_ref)
        config = self.scopes.projects[self.ctx.active_scope].repository_config
        if config:
            if config.get('frozen_run_id') != state.run_id or source.get('repository_config') != config:
                raise PermissionError('仓库配置快照与 Run 身份不一致')
        workspace = workspace or Workspace(Path(data_root or self.args.data)/'workspaces'/state.run_id,
                                           self.scopes.projects[self.scope].allowed_files,
                                           protected_paths=(config or {}).get('protected_paths', []))
        if state.scope_id != self.ctx.active_scope or state.source_manifest != digest(source):
            raise PermissionError('运行状态与源码快照身份不一致')
        from tracefix.runtime.test_edit_approval import verified_test_edit_files
        workspace.approved_test_files = verified_test_edit_files(self.store, self.artifacts, state, workspace)
        workspace.validate_repository(self.scopes, self.ctx, source,
            base_commit=state.patch_base_commit, patch_hash=state.patch_hash,
            branch=state.local_branch or 'tracefix/' + state.run_id)
        return workspace, source

    def bind(self, state, profile, workspace, source):
        if not (state.harness_job_id and source.get('harness_revision')):
            self.validate_workspace(state, workspace, source)
        runner = DockerRunner(profile, workspace, state.run_id)
        if self.runtime_services is not None:
            self.runtime_services.own(runner)
        elif self.runtime_stack is not None:
            self.runtime_stack.push_async_callback(runner.close)
        browser = MCPBrowser(runner.browser_command(), Policy(profile.allowed_origins))
        if self.runtime_services is not None:
            self.runtime_services.own(browser)
        elif self.runtime_stack is not None:
            self.runtime_stack.push_async_callback(browser.close)
        embedding = None
        if os.getenv('TRACEFIX_EMBEDDING_URL'):
            embedding = EmbeddingAdapter(os.environ['TRACEFIX_EMBEDDING_URL'], os.environ['TRACEFIX_EMBEDDING_REVISION'])
        managed = bool(self.scopes.projects[self.scope].repository_config)
        memory = MemoryLibrary(Path(self.args.data) / 'memory.sqlite3', cross_run=False if managed else None)
        retriever = Retriever(self.store, self.scopes, self.ctx, embedding, fallback=memory,
                              cross_run=False if managed else None)
        rule_library = RuleLibrary(self.documents.path)
        rule_resolver = RuleResolver(rule_library)
        self.engine = Engine(self.store, self.artifacts, self.scopes, self.ctx, profile, workspace,
            runner, browser, Gateway(), retriever, source, self.saver, self.notify,
            rule_resolver=rule_resolver, rule_library=rule_library)
        self.engine.worker_model = configured_worker_model()
        self.engine.memory = memory
        self.engine.documents = self.documents
        if self.runtime_services is not None:
            self.engine.runtime_services = self.runtime_services
        self.engine.current_run = state.run_id
        self.run_id = state.run_id

    async def finish_cancelled(self):
        state = self.state()
        if state.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                                RunStatus.FAILED, RunStatus.SUPERSEDED}:
            return
        self.engine.cancel_subagents(state)
        self.engine.control = None
        with self.store.writer(state.run_id):
            state = self.engine.changed(state, phase=Phase.FINALIZE, run_status=RunStatus.RUNNING,
                                        outcome=Outcome.INCONCLUSIVE, error='用户已取消', pending_action=None)
            await self.engine.finalize(state, None)
        self.publish_console()

    async def stop(self, *, grace=5):
        self.stop_requested = True
        state = self.state()
        if state.harness_job_id:
            await self.engine.harness.stop(self.engine, state)
            self.engine.harness.project(state)
            self.publish_console()
            return
        self.engine.control = 'cancel'
        self.engine.cancel_subagents(state)
        task = self.task
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), grace)
            except asyncio.TimeoutError:
                task.cancel()
                await task
        else:
            await self.finish_cancelled()

    async def drive(self, state=None, resume=None):
        try:
            await self.engine.run(state, resume)
            self.publish_console()
            child = self.engine.retarget_child
            if child:
                # L3 在运行时确认父 Run 已被替换后交回子状态；复制父证据并建立独立控制台记录。
                previous = self.state()
                self.engine.copy_retarget_evidence(previous, child)
                self.engine.retarget_child = None
                self.transfer_console_run(previous, child)
                self.goal, self.mode = child.goal, child.mode
                await self.create_agent(state=child)
        except asyncio.CancelledError:
            if not self.stop_requested:
                raise
            await self.finish_cancelled()
        except Exception as e:
            latest = self.state()
            if latest.harness_job_id:
                job = self.engine.harness.job(latest)
                if job.status == 'RUNNING':
                    self.engine.harness.store.block_job(job.job_id, 'HARNESS_NODE_FAILED',
                        scope_id=latest.scope_id, expected_revision=job.revision)
                self.engine.changed(latest, run_status=RunStatus.PAUSED,
                    error=sanitize(error_message(e)),
                    error_details={'source': 'harness', 'requires_manual_review': True})
                self.engine.harness.project(latest)
                self.publish_console(error=error_message(e))
                return
            terminal = latest.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                                            RunStatus.FAILED, RunStatus.SUPERSEDED}
            if latest.execution_mode == 'batch':
                if not terminal:
                    latest = self.engine.finish_error(latest, e)
                    await self.engine.finalize(latest, None)
                self.publish_console(error=error_message(e))
                return
            if not terminal:
                latest.run_status = RunStatus.PAUSED
                latest.error = sanitize(error_message(e))
                latest.error_details = {'requires_manual_review': True, 'source': 'runtime'}
                latest.revision += 1
                self.store.save(latest)
                self.engine.event(latest, 'run.error', {'error': latest.error})
            self.publish_console(error=error_message(e))

    async def resume(self, run_id):
        if self.task and not self.task.done():
            raise ValueError('Run 已在执行')
        await self.ensure_runtime()
        if run_id.startswith('hjob'):
            from tracefix.harness_control import ControlIdentity, HarnessControl
            from tracefix.storage.harness import PostgresHarnessStore
            authority = PostgresHarnessStore(self.store, self.artifacts)
            job = authority.get_job(run_id, scope_id=self.scope)
            control = HarnessControl(self.store, self.artifacts, self.args.data, authority=authority)
            state = control.state(job)
            await self.restore_harness(state)
            return
        s = self.store.load(run_id, self.scope)
        if s.harness_job_id:
            await self.restore_harness(s)
            return
        if s.run_status not in {RunStatus.PAUSED, RunStatus.WAITING_APPROVAL}:
            raise ValueError('只可恢复已暂停的安全边界；意外崩溃的 RUNNING 状态须检查操作回执')
        workspace, source = self.validate_workspace(s)
        self.begin_console_run(s)
        if not self.engine or self.run_id != run_id:
            # A stopped browser cannot promise restoration of an arbitrary pixel state.
            # REVIEW only consumes frozen artifacts and is safe across processes.
            if s.run_status != RunStatus.WAITING_APPROVAL:
                raise ValueError('浏览器进程已丢失；请创建新 Run 从冻结场景重放。审计轨迹仍可查看。')
            old = self.artifacts.json(self.scope, run_id, s.memory_snapshot_ref)
            if old['epochs'] != [list(x) for x in self.ctx.epochs]:
                raise PermissionError('Scope 权限已变化')
            profile = self.project_profile()
            self.bind(s, profile, workspace, source)
            env = await self.engine.runner.inspect_images()
            if env != s.environment_digest:
                raise ValueError('环境版本变化，拒绝恢复')
        if s.run_status != RunStatus.WAITING_APPROVAL:
            self.task = asyncio.create_task(self.drive(resume='resume'))

    async def restore_harness(self, state):
        from tracefix.harness_control import HarnessControl
        from tracefix.runtime.harness_bootstrap import restore_harness_runtime
        from tracefix.storage.harness import PostgresHarnessStore
        authority = PostgresHarnessStore(self.store, self.artifacts)
        control = HarnessControl(self.store, self.artifacts, self.args.data, authority=authority)
        job = authority.get_job(state.harness_job_id, scope_id=self.scope)
        if job.status != 'STOPPED' or job.blocker:
            raise ValueError('仅可恢复无 blocker 的 STOPPED Job')
        state = control.state(job)
        source = self.artifacts.json(self.scope, state.run_id, state.repo_snapshot_ref)
        old = self.artifacts.json(self.scope, state.run_id, state.memory_snapshot_ref)
        if old['epochs'] != [list(entry) for entry in self.ctx.epochs]:
            raise PermissionError('Scope 权限已变化')
        project = self.scopes.projects[self.scope]
        workspace = Workspace(Path(source['workspace_root']), project.allowed_files,
            protected_paths=(project.repository_config or {}).get('protected_paths', []))
        workspace.repository_snapshot = source
        profile = (Profile.model_validate(state.remote_config['profile'])
            if state.remote_config and state.remote_config.get('version') == 1 else self.project_profile())
        self.bind(state, profile, workspace, source)
        runtime = restore_harness_runtime(self.engine, state, store=authority)
        self.engine.harness = runtime
        self.run_id = next(session.run_id for session in authority.list_sessions(
            job.job_id, scope_id=self.scope) if session.kind == 'initializer')
        if source.get('harness_revision'):
            runtime.validate_workspace(self.engine, state, source)
        inspection_ref = await control.inspect(job, state)
        if self.engine.spec(state).mode == 'gui' and state.harness_stage not in {'coding', 'initialize'}:
            raise ValueError('浏览器进程已丢失，此阶段须经安全重放恢复')
        environment = await self.engine.runner.inspect_images()
        if environment != state.environment_digest:
            raise PermissionError('恢复环境版本与冻结身份不一致')
        command_mode = self.engine.spec(state).mode != 'gui'
        self.engine.runner.command_only = command_mode
        health = await (self.engine.runner.start_commands(state.source_manifest) if command_mode
            else self.engine.runner.start(state.source_manifest))
        if health.get('passed') is not True:
            raise RuntimeError('恢复后的沙箱健康检查失败')
        if not command_mode:
            await self.engine.browser.open()
        state = runtime.resume(self.engine, state, inspection_ref)
        self.stop_requested = False
        self.task = asyncio.create_task(self.drive(state, resume='harness'))
