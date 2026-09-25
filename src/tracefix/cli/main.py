from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from tracefix.cli.registry import COMMANDS, parse
from tracefix.cli.render import Renderer
from tracefix.cli.workspace import WorkspaceCommands, command_parser
from tracefix.console import project_catalog
from tracefix.config import load_profile, load_projects
from tracefix.execution.browser import MCPBrowser
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.context import SkillCatalog
from tracefix.knowledge.documents import DocumentLibrary, timestamp
from tracefix.knowledge.retrieval import EmbeddingAdapter, Retriever
from tracefix.knowledge.scope import ScopeResolver
from tracefix.model.gateway import BrowserPolicyRouter, Gateway
from tracefix.model.chat import stream_chat
from tracefix.messages import ChineseArgumentParser, error_message
from tracefix.runtime.contracts import Phase, RunState, RunStatus, TestSpec, digest, new_id
from tracefix.runtime.engine import Engine
from tracefix.runtime.continuation import continuation_state
from tracefix.remote import RemoteConfigStore, effective, prepare_checkout
from tracefix.storage.artifacts import Artifacts, redact, sanitize
from tracefix.storage.presentation import label
from tracefix.storage.store import PostgresStore


class Session:
    def __init__(self, args, store, saver):
        self.args, self.store, self.saver = args, store, saver
        self.scopes = ScopeResolver(load_projects(Path(args.projects)), Path(args.projects))
        self.scope = args.project
        self.ctx = self.scopes.context(self.scope)
        self.render = Renderer(args.plain)
        self.artifacts = Artifacts(Path(args.data)/'artifacts', self.evidence_damaged)
        self.render.artifacts = self.artifacts
        self.render.scope = self.scope
        self.engine, self.task, self.run_id = None, None, None
        self.goal, self.mode = args.goal or '', args.mode
        self.history_generation = 0
        self.console_run_id = os.getenv('TRACEFIX_CONSOLE_RUN_ID')
        self.web_console_run_id = self.console_run_id
        self.documents = DocumentLibrary(getattr(args, 'console_db', None))
        self.workspace_commands = WorkspaceCommands(self)
        self.runtime_stack = None
        self.chat_history = {}
        self.continuation_parent = None
        self.continuation_instruction = None
        self.remote_store = RemoteConfigStore(Path(args.data) / 'remote-config.json')
        self.session_remote = {}

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
        dsn = os.getenv('TRACEFIX_DATABASE_URL', '')
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
        self.render.reset()
        self.render.scope = project_id

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
                  'phase': str(state.phase) if state else 'STARTING', 'origin': 'cli', 'pid': os.getpid(),
                  'dataRoot': str(Path(self.args.data).resolve()), 'finishedAt': None,
                  'profile': self.args.profile, 'registry': str(Path(self.args.projects).resolve())}
        if state:
            fields['agentRunId'] = state.run_id
        if self.continuation_parent:
            fields['parentRunId'] = self.continuation_parent
            fields['continuationInstruction'] = self.continuation_instruction
        self.documents.update_run(self.console_run_id, redact(fields), create=True)

    def publish_console(self, event=None, error=None):
        if not self.console_run_id or not self.run_id:
            return
        state = self.state()
        changes = {'agentRunId': state.run_id, 'goal': state.goal, 'status': str(state.run_status).lower(),
                   'phase': str(state.phase), 'outcome': state.outcome, 'reportRef': state.report_ref,
                   'branch': state.local_branch, 'error': error or state.error}
        changes.update({'continuationCount': state.continuation_count,
                        'abnormalTermination': state.abnormal_termination,
                        'continuationMarkers': state.continuation_markers,
                        'continuationInstruction': state.continuation_instruction})
        if state.run_status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL, RunStatus.FAILED} or error:
            changes['finishedAt'] = timestamp()
        else:
            changes['finishedAt'] = None
        if error:
            changes['status'] = str(state.run_status).lower()
        changes['pid'] = os.getpid() if state.run_status == RunStatus.RUNNING and not error else None
        knowledge = event.get('payload') if event and event['type'] == 'knowledge.selected' else None
        if event:
            record = self.documents.run(self.console_run_id)
            if record.get('origin') == 'cli':
                entry = f"[{label(str(state.phase))}] {label(event['type'])} · {label(str(state.run_status))}"
                if knowledge:
                    entry += ' · ' + ', '.join(f"{document['title']} v{document['version']}" for document in knowledge['documents'])
                changes['logs'] = [*record.get('logs', []), sanitize(entry) + '\n'][-120:]
        self.documents.update_run(self.console_run_id, redact(changes), knowledge=knowledge)

    def evidence_damaged(self, report):
        """旧证据副本损坏不能静默跳过：写入事件流留痕，并当场告知用户。"""
        self.render.text(f"证据告警：{report['reason']}（{report['ref']}）；已改写新副本，旧副本保留待查。")
        if self.engine and self.run_id:
            self.engine.event(self.state(), 'evidence.damaged', report)

    def notify(self, event):
        self.render.event(event)
        if self.console_run_id:
            self.documents.append_event(self.console_run_id, event)
        if event['type'] in {'state.changed', 'knowledge.selected', 'run.finished'}:
            self.publish_console(event)
        if event['type'] == 'knowledge.selected':
            documents = event['payload']['documents']
            self.render.text(f'知识检索：选用 {len(documents)} 篇文档；/memory sources 查看来源。')

    def active(self):
        if self.task and not self.task.done():
            return True
        if self.run_id:
            return self.store.load(self.run_id, self.scope).run_status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL, RunStatus.FAILED}
        return False

    def state(self):
        if not self.run_id:
            raise ValueError('当前没有 Run')
        return self.store.load(self.run_id, self.scope)

    async def create(self):
        if self.active():
            raise ValueError('当前 Run 尚未结束')
        if not self.goal.strip():
            raise ValueError('先输入测试目标，再输入 /run')
        if self.mode == 'chat':
            await self.chat(self.goal)
            return
        self.begin_console_run()
        self.run_id, self.engine, self.task = None, None, None
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
        record = self.workspace_commands.run(run_id)
        claim = os.getenv('TRACEFIX_CONTINUATION_ID')
        if claim and self.web_console_run_id == record['id']:
            if record.get('continuationId') != claim or record['status'] != 'running':
                raise ValueError('Web 续执行请求已失效')
            instruction = record['continuationInstruction']
        else:
            record = self.workspace_commands.call('run.continue', {'id': record['id'], 'instruction': instruction})
            instruction = record['continuationInstruction']
        self.console_run_id = record['id']
        self.web_console_run_id = None
        self.run_id, self.engine, self.task = None, None, None
        self.goal, self.mode = record['goal'], record['mode']
        self.args.data = record.get('dataRoot') or self.args.data
        self.args.profile = record.get('profile') or self.args.profile
        try:
            self.artifacts = Artifacts(Path(self.args.data) / 'artifacts', self.evidence_damaged)
            self.render.artifacts = self.artifacts
            self.render.reset()
            await self.ensure_runtime()
            previous = self.store.load(record['agentRunId'], self.scope) if record.get('agentRunId') else None
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
            await self.create_agent(state=continued, reuse_workspace=previous is not None)
        except Exception as error:
            self.documents.update_run(record['id'], redact({'status': 'failed', 'phase': 'FINALIZE',
                'error': error_message(error), 'finishedAt': timestamp(), 'pid': None}))
            raise

    async def create_agent(self, state=None, reuse_workspace=False):
        await self.ensure_runtime()
        if not os.getenv('TRACEFIX_API_KEY'):
            raise ValueError('请设置 TRACEFIX_API_KEY 环境变量')
        profile = self.project_profile()
        remote = state.remote_config if reuse_workspace else effective(self.remote_store.get(self.scope), self.session_remote.get(self.scope))
        if remote and not reuse_workspace:
            project = self.scopes.projects[self.scope]
            destination = Path(remote.get('destination') or project.root)
            if not destination.is_absolute():
                destination = Path.cwd() / destination
            project.root = destination.resolve()
            prepare_checkout(remote, project.root)
        url = state.url if reuse_workspace else self.args.url or profile.url
        Policy(profile.allowed_origins).url(url)
        if state is None:
            state = RunState(scope_id=self.scope, goal=self.goal, url=url, mode=self.mode,
                             parent_run_id=self.continuation_parent,
                             continuation_instruction=self.continuation_instruction,
                             remote_config=remote)
            record = self.documents.run(self.console_run_id)
            state.continuation_markers = record.get('continuationMarkers', [])
            state.continuation_count = len(state.continuation_markers)
            state.abnormal_termination = bool(state.continuation_count)
            state.continuation_instruction = record.get('continuationInstruction')
        workspace_path = Path(self.args.data)/'workspaces'/state.run_id
        if reuse_workspace:
            source = self.artifacts.json(self.scope, state.run_id, state.repo_snapshot_ref)
            workspace = Workspace(workspace_path, self.scopes.projects[self.scope].allowed_files)
            if not (workspace.root / '.git').is_dir():
                raise FileNotFoundError('原任务工作区不存在，无法继续；历史轨迹和报告仍保留')
            snapshot = self.artifacts.json(self.scope, state.run_id, state.memory_snapshot_ref)
            if snapshot['epochs'] != [list(entry) for entry in self.ctx.epochs]:
                raise PermissionError('项目权限已变化，请检查原任务的作用域')
            diff = workspace.diff()
            if (state.patch_hash and digest(diff.encode()) != state.patch_hash) or (not state.patch_hash and diff):
                raise PermissionError('原任务补丁已变化，请核对工作区后继续')
        else:
            workspace, source = Workspace.export(self.scopes, self.ctx, profile.source_commit, workspace_path)
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
        self.store.save(state)
        self.publish_console()
        self.render.status(state)
        if state.continuation_count:
            self.render.text(f'继续原任务 {state.run_id}：第 {state.continuation_count} 次继续；此前非成功结束已标记，轨迹将追加。')
        self.task = asyncio.create_task(self.drive(state))

    def bind(self, state, profile, workspace, source):
        runner = DockerRunner(profile, workspace, state.run_id)
        browser = MCPBrowser(runner.browser_command(), Policy(profile.allowed_origins))
        embedding = None
        if os.getenv('TRACEFIX_EMBEDDING_URL'):
            embedding = EmbeddingAdapter(os.environ['TRACEFIX_EMBEDDING_URL'], os.environ['TRACEFIX_EMBEDDING_REVISION'])
        retriever = Retriever(self.store, self.scopes, self.ctx, embedding)
        student = None
        if os.getenv('TRACEFIX_STUDENT_URL'):
            student = Gateway(base_url=os.environ['TRACEFIX_STUDENT_URL'], key=os.getenv('TRACEFIX_STUDENT_KEY','local'),
                              text_model=os.environ['TRACEFIX_STUDENT_MODEL'], vision_model=os.environ['TRACEFIX_STUDENT_MODEL'])
        self.engine = Engine(self.store, self.artifacts, self.scopes, self.ctx, profile, workspace,
            runner, browser, BrowserPolicyRouter(Gateway(), student), retriever, source, self.saver, self.notify)
        self.engine.documents = self.documents
        self.engine.current_run = state.run_id
        self.run_id = state.run_id

    async def drive(self, state=None, resume=None):
        try:
            await self.engine.run(state, resume)
            self.publish_console()
            self.render.status(self.state())
        except Exception as e:
            latest = self.state()
            if latest.run_status not in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL, RunStatus.FAILED}:
                latest.run_status = RunStatus.PAUSED
                latest.error = sanitize(error_message(e))
                latest.error_details = {'requires_manual_review': True, 'source': 'runtime'}
                latest.revision += 1
                self.store.save(latest)
                self.engine.event(latest, 'run.error', {'error': latest.error})
            self.publish_console(error=error_message(e))
            self.render.stop(RunStatus.PAUSED)
            self.render.panel('运行已暂停，等待人工核查', error_message(e))

    async def resume(self, run_id):
        if self.task and not self.task.done():
            raise ValueError('Run 已在执行')
        await self.ensure_runtime()
        s = self.store.load(run_id, self.scope)
        if s.run_status not in {RunStatus.PAUSED, RunStatus.WAITING_APPROVAL}:
            raise ValueError('只可恢复已暂停的安全边界；意外崩溃的 RUNNING 状态须检查操作回执')
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
            source = self.artifacts.json(self.scope, run_id, s.repo_snapshot_ref)
            workspace = Workspace(Path(self.args.data)/'workspaces'/run_id, self.scopes.projects[self.scope].allowed_files)
            self.bind(s, profile, workspace, source)
            env = await self.engine.runner.inspect_images()
            if env != s.environment_digest:
                raise ValueError('环境版本变化，拒绝恢复')
        if s.run_status == RunStatus.WAITING_APPROVAL:
            self.render.status(s)
        else:
            self.task = asyncio.create_task(self.drive(resume='resume'))

    async def chat(self, message, use_knowledge=True):
        history = self.chat_history.setdefault(self.scope, [])
        answer, sources = '', []
        try:
            async for event in stream_chat(message, history, self.scope, self.documents, use_knowledge):
                if 'sources' in event:
                    sources = event['sources']
                if 'delta' in event:
                    answer += event['delta']
                    self.render.stream(event['delta'])
        finally:
            self.render.stream('\n')
        history.extend([{'role': 'user', 'content': message}, {'role': 'assistant', 'content': answer}])
        self.chat_history[self.scope] = history[-20:]
        if sources:
            self.render.text('参考来源：' + '；'.join(f"{source['title']} v{source['version']} ({source['id']})" for source in sources))

    async def dispatch(self, text):
        command, args = parse(text)
        if command is None:
            if getattr(self, 'mode', '') == 'chat':
                await self.chat(text)
            elif self.active():
                self.engine.notes.append(text)
                self.engine.event(self.state(), 'interrupt.prompt', {'text': text, 'correction': True})
                self.render.input(text)
                self.render.text('已排队；更改冻结测试目标需新建 Run。')
            elif self.run_id and self.state().run_status == RunStatus.PAUSED:
                self.engine.notes.append(text)
                self.engine.event(self.state(), 'interrupt.prompt', {'text': text, 'effect': 'resume after correction'})
                self.render.input(text)
                self.task = asyncio.create_task(self.drive(resume='resume'))
                self.render.text('已记录纠正提示，正在从安全边界继续。')
            else:
                self.goal = text
                self.render.text('目标已记录。输入 /run 开始。')
            return
        if command == 'help':
            self.render.help(COMMANDS)
        elif command == 'projects':
            self.workspace_commands.project(args)
        elif command == 'remote':
            self.workspace_commands.remote(args)
        elif command == 'continue':
            if len(args) < 2:
                raise ValueError('用法：/continue RUN_ID INSTRUCTION')
            await self.continue_task(args[0], ' '.join(args[1:]))
        elif command == 'runs':
            if args and args[0] == 'continue':
                if len(args) < 3:
                    raise ValueError('用法：/runs continue RUN_ID INSTRUCTION')
                await self.continue_task(args[1], ' '.join(args[2:]))
            else:
                self.workspace_commands.runs(args)
        elif command == 'knowledge':
            await self.workspace_commands.knowledge(args)
        elif command == 'chat':
            if args == ['clear']:
                self.chat_history[self.scope] = []
                self.render.text('已清空当前项目的 Chat 会话。')
            else:
                parser = command_parser('chat')
                parser.add_argument('--no-knowledge', action='store_true')
                parser.add_argument('message', nargs='+')
                request = parser.parse_args(args)
                await self.chat(' '.join(request.message), not request.no_knowledge)
        elif command == 'run':
            await self.create()
        elif command == 'interrupt':
            if not self.active():
                raise ValueError('当前没有运行中的 Run')
            self.engine.control = 'pause'
            self.render.text('已打断，等待纠正')
        elif command == 'mode':
            if len(args)!=1 or args[0] not in {'test','repair','chat'}:
                raise ValueError('用法：/mode test|repair|chat')
            self.mode=args[0]
            self.render.text(f'新 Run 模式：{self.mode}')
        elif command == 'status':
            if self.run_id:
                self.render.status(self.state())
            elif self.console_run_id:
                self.workspace_commands.runs(['show', self.console_run_id])
            else:
                self.render.text(f'当前项目 {self.scope} · {self.mode} · 暂无 Run')
        elif command == 'trace':
            for e in self.store.trace(self.run_id, self.scope):
                self.render.event(e, replay=True)
        elif command == 'diff':
            patches=[e for e in self.store.trace(self.run_id,self.scope) if e['type']=='patch.applied']
            if patches:
                self.render.diff(self.artifacts.read(self.scope,self.run_id,patches[-1]['payload']['diff_ref']).decode())
            else:
                self.render.diff(self.engine.workspace.diff())
        elif command == 'evidence':
            if len(args)!=1:
                raise ValueError('用法：/evidence ID')
            self.artifacts.read(self.scope, self.run_id, args[0])
            self.render.text(self.artifacts._path(self.scope, self.run_id, args[0]))
        elif command == 'model-log':
            state = self.state()
            if len(args) > 1 or args and (not args[0].isdigit() or int(args[0]) < 1):
                raise ValueError('用法：/model-log [N]')
            logical_call = int(args[0]) if args else None
            for ref in state.model_exchange_refs:
                record = self.artifacts.json(self.scope, self.run_id, ref)
                if logical_call is None or record.get('logical_call') == logical_call:
                    kind = next((name for name in ('request', 'response', 'error') if name in record), 'record')
                    self.render.text(f"第 {record.get('logical_call', '?')} 次调用 · {label(record.get('phase', '?'))} · "
                                     f"{record.get('schema', '?')} · 第 {record.get('attempt', '?')} 次尝试 · "
                                     f"{label(kind)}: {self.artifacts._path(self.scope, self.run_id, ref)}")
        elif command == 'report':
            events = self.store.trace(self.run_id, self.scope)
            for e in reversed(events):
                if e['type']=='run.finished':
                    self.render.text(self.artifacts._path(self.scope,self.run_id,e['payload']['html_ref']))
                    return
            self.render.text('报告将在收尾后生成。')
        elif command == 'scope':
            self.workspace_commands.project(args)
        elif command == 'memory':
            if args and args[0] == 'search':
                await self.workspace_commands.knowledge(args)
                return
            if args and args[0] == 'sources':
                self.workspace_commands.runs(args if len(args) > 1 else ['sources', self.console_run_id or ''])
                return
            if args:
                raise ValueError('用法：/memory [search QUERY|sources [RUN_ID]]')
            self.render.panel('作用域 / 记忆',json.dumps(dataclasses.asdict(self.ctx),ensure_ascii=False,indent=2))
            if self.console_run_id:
                self.workspace_commands.runs(['sources', self.console_run_id])
        elif command == 'context':
            s=self.state()
            knowledge = self.documents.run(self.console_run_id).get('knowledge', []) if self.console_run_id else []
            usage = getattr(s, 'usage', None) or s.budget
            self.render.panel('上下文',json.dumps({'spec_ref':s.test_spec_ref,'observation_ref':s.observation_ref,'memory_ref':s.memory_snapshot_ref,'knowledge':knowledge,'usage':usage.model_dump()},ensure_ascii=False,indent=2))
        elif command == 'skills':
            self.render.panel('流程 Skill',json.dumps(SkillCatalog(Path(self.args.skills)).index(),ensure_ascii=False,indent=2))
        elif command in {'pause','cancel'}:
            s=self.state()
            if s.run_status == RunStatus.WAITING_APPROVAL and command=='cancel':
                self.store.decide_approval(s.approval_ref,s,'reject')
                self.task=asyncio.create_task(self.drive(resume='reject'))
            elif s.run_status==RunStatus.PAUSED and command=='cancel':
                self.task=asyncio.create_task(self.drive(resume='cancel'))
            else:
                self.engine.control=command
                self.render.text('已请求在安全边界'+('暂停' if command=='pause' else '取消'))
        elif command == 'resume':
            if len(args)!=1:
                raise ValueError('用法：/resume RUN_ID')
            await self.resume(args[0])
        elif command in {'approve','reject'}:
            if len(args)!=1:
                raise ValueError(f'用法：/{command} ID')
            s=self.state()
            if s.run_status != RunStatus.WAITING_APPROVAL:
                raise ValueError('当前没有审批等待')
            self.store.decide_approval(args[0],s,command)
            self.task=asyncio.create_task(self.drive(resume=command))
        elif command == 'quit':
            if self.active():
                await self.dispatch('/cancel')
                if self.task:
                    await self.task
            raise EOFError

    async def interact(self):
        from prompt_toolkit.patch_stdout import patch_stdout
        from tracefix.cli.input import make_prompt, read_plain

        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        interactive = terminal and not self.render.plain
        generation=-1
        self.render.welcome(self.scope, self.mode, os.getenv('TRACEFIX_TEXT_MODEL', ''))
        with patch_stdout(raw=True):
            while True:
                if interactive and generation!=self.history_generation:
                    history=Path(self.args.data)/'history'/self.scope
                    prompt=make_prompt(self.render, lambda: self.scope, lambda: self.mode, history)
                    generation=self.history_generation
                try:
                    if terminal and not interactive:
                        self.render.console.print('TraceFix > ', end='')
                    text=await prompt.prompt_async() if interactive else await read_plain()
                    if text.strip():
                        await self.dispatch(text.strip())
                except KeyboardInterrupt:
                    if self.active():
                        await self.dispatch('/pause')
                except EOFError:
                    if self.active():
                        await self.dispatch('/cancel')
                        if self.task: await self.task
                    self.render.stop()
                    break
                except Exception as e:
                    self.render.panel('输入', error_message(e))


async def application(args):
    async with AsyncExitStack() as resources:
        session = Session(args, None, None)
        session.runtime_stack = resources
        if getattr(args, 'continue_run', None):
            await session.continue_task(args.continue_run, args.instruction)
            if session.task:
                await session.task
        elif args.run:
            await session.create()
            if session.task:
                await session.task
                if session.state().run_status == RunStatus.WAITING_APPROVAL:
                    session.render.text('非交互模式已暂停，未自动批准。用 /resume RUN_ID 进入审批。')
        elif args.command:
            for command in args.command:
                try:
                    await session.dispatch(command)
                except EOFError:
                    break
                if session.task:
                    await session.task
        else:
            await session.interact()


def main():
    if sys.platform == 'win32':
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
    parser=ChineseArgumentParser(description='TraceFix Agent · 证据驱动的 GUI 缺陷修复')
    parser.add_argument('--project',default='bugboard')
    parser.add_argument('--projects',default=os.getenv('TRACEFIX_PROJECTS', 'profiles/projects.yaml'))
    parser.add_argument('--profile',help='指定 Profile；默认按当前项目自动匹配')
    parser.add_argument('--skills',default='skills')
    parser.add_argument('--data',default=os.getenv('TRACEFIX_DATA', '.tracefix'))
    parser.add_argument('--console-db',default=os.getenv('TRACEFIX_CONSOLE_DB'),help='与 Web 共用的文档及运行索引 SQLite 文件')
    parser.add_argument('--url')
    parser.add_argument('--goal')
    parser.add_argument('--mode',choices=['test','repair','chat'],default='test')
    parser.add_argument('--spec')
    parser.add_argument('--plain',action='store_true')
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument('--run',action='store_true',help='执行一次非交互式运行；遇到审批会暂停')
    execution.add_argument('--command',action='append',help='执行斜杠命令后退出；可重复指定，知识管理无需启动后端')
    execution.add_argument('--continue-run', help='继续原任务，保留任务 ID 和完整轨迹')
    parser.add_argument('--instruction', default='', help='本次继续执行的指令')
    parser.add_argument('--doctor',action='store_true')
    parser.add_argument('--preview',action='store_true',help='离线界面预览；使用明确标记的演示数据，不连接外部服务')
    parser.add_argument('--smoke',action='store_true',help='显式使用 Fake 模型/工具的 CI Smoke 测试')
    args=parser.parse_args()
    if args.preview:
        from tracefix.cli.preview import preview
        asyncio.run(preview(args))
        return
    if args.doctor:
        data={'python':sys.version.split()[0], 'docker':bool(shutil.which('docker')), 'git':bool(shutil.which('git')),
              'platform':sys.platform, 'checkpoint_backend':'threaded-postgres' if sys.platform=='win32' else 'async-postgres',
              'api_key_configured':bool(os.getenv('TRACEFIX_API_KEY')), 'database_configured':bool(os.getenv('TRACEFIX_DATABASE_URL'))}
        Renderer(args.plain).panel('环境检查',json.dumps(data,indent=2))
        return
    if args.smoke:
        from tracefix.runtime.smoke import main as smoke
        asyncio.run(smoke(Path(args.data)/'smoke', plain=args.plain))
        return
    try:
        asyncio.run(application(args))
    except (ValueError,RuntimeError,PermissionError,OSError) as e:
        Renderer(args.plain).panel('启动失败', error_message(e))
        raise SystemExit(2)


if __name__=='__main__':
    main()
