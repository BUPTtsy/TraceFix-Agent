from __future__ import annotations

import json
import inspect
import os
from copy import deepcopy
from pathlib import Path

from tracefix.agents.claude_sdk_adapter import ExecutionResult, SdkExecutionRequest
from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.execution.harness_revision import (RevisionReceipt, cumulative_diff,
    materialize_revision, tracked_files, validate_receipt)
from tracefix.execution.repository import plain_root, run_git
from tracefix.runtime.continuation import derive_run
from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus, digest, new_id
from tracefix.runtime.harness_contracts import (HarnessFeedback, HarnessJob, HarnessSession,
    HarnessWorkItem, InitializationManifest, SessionHandoff, TaskPlan, VerificationCommand,
    VerificationRecord, ExecutionCheckpoint)
from tracefix.storage.harness import HarnessConflict


HARNESS_VERSION = 'harness/1'
POLICY_VERSION = 'repair/1'
# 候选导出首版只接纳 create/modify，删除或改名会使整个候选被拒，需在执行前告知模型。
SDK_SYSTEM_PROMPT = ('按冻结目标执行。无法继续时报告 blocker；不得提问或更改验收。'
    '不得删除、移动或重命名已有文件（含 Bash 的 rm/mv/git mv），否则整个候选会被拒绝；'
    '只修改已有文件内容或新增文件。'
    '调查与审查只委派 readonly 子 Agent。除非已有确凿证据（复现结果、失败的验收或测试输出、精确的源码定位）'
    '证明需要修改且修改范围明确，不得委派可写子 Agent（如 general-purpose）；'
    '确需委派时，在委派说明中列出所依据的证据与允许修改的文件。')


def environment_key(engine, state, config):
    files = engine.source['files']
    dependency_files = {path: value for path, value in files.items()
        if path.endswith(('.lock', 'lock.json', 'requirements.txt', 'pyproject.toml',
                          'package.json', 'Dockerfile', 'compose.yaml'))}
    project = engine.scopes.projects[state.scope_id]
    project_value = (project.model_dump(mode='json')
                      if hasattr(project, 'model_dump') else project)
    profile = engine.profile
    profile_value = (profile.model_dump(mode='json')
                     if hasattr(profile, 'model_dump') else profile)
    return digest({'goal': digest(state.goal), 'project': project_value,
        'profile': profile_value, 'dependencies': dependency_files,
        'image': state.environment_digest, 'policy': POLICY_VERSION,
        'harness': HARNESS_VERSION, 'sdk': config.config_hash})


def initialization_valid(manifest, *, goal_hash, environment_digest):
    return (manifest.goal_hash == goal_hash and manifest.environment_digest == environment_digest
            and manifest.healthy and manifest.policy_version == POLICY_VERSION)


def validate_plan(plan, job, workspace):
    plan = TaskPlan.model_validate(plan)
    if plan.job_id != job.job_id or plan.scope_id != job.scope_id:
        raise PermissionError('初始化计划越过 Job/scope')
    if set(plan.acceptance_refs) != set(job.acceptance_refs):
        raise PermissionError('初始化计划不得改变冻结目标验收')
    for item in plan.work_items:
        if (item.status not in {'PENDING', 'READY'} or item.lease_owner or item.lease_epoch
                or item.revision or item.verification_ref):
            raise PermissionError('Agent 不得预先晋升任务或持有租约')
        if not item.writable_files or not item.allowed_files:
            raise ValueError('增量必须声明明确文件范围')
        for path in item.allowed_files:
            workspace.path(path)
        for path in item.writable_files:
            workspace.path(path, write=True)
    return plan


def command_suggestions(suggestions, profile):
    approved = {tuple(command) for command in profile.commands.values()}
    result = []
    for command in suggestions:
        if not isinstance(command, list) or tuple(command) not in approved:
            raise PermissionError('初始化建议命令不属于宿主已批准模板')
        result.append({'command': command, 'approved_by': 'frozen_profile'})
    return result


def local_repository_config(config):
    return (not config or isinstance(config, dict) and type(config.get('version')) is int
        and config['version'] == 1 and config.get('publish_branch') is False)


def pending_harness_operations(store, state):
    if hasattr(store, 'conn'):
        records = store.conn.execute("SELECT id,scope_id,resources FROM operations "
            "WHERE status IN ('STARTED','UNKNOWN')").fetchall()
    else:
        records = [{'id': identity, **value} for identity, value in store.operations.items()
            if value.get('status') in {'STARTED', 'UNKNOWN'}]
    return [record['id'] for record in records if record['scope_id'] == state.scope_id
        or '*' in (record.get('resources') or ['*'])]


class HarnessRuntime:
    def __init__(self, store, runner, config: ClaudeSdkConfig, root: Path, *,
                 owner=None, final_verifier=None, rebind=None, projector=None):
        if config.backend != 'claude_sdk':
            raise ValueError('Harness 必须使用冻结 SDK 配置')
        self.store, self.runner, self.config = store, runner, config
        self.root = plain_root(root)
        self.owner = owner or new_id('howner')
        self.final_verifier = final_verifier or self._final_verification
        self.rebind = rebind
        self.projector = projector

    def project(self, state):
        if self.projector is not None:
            self.projector(self.store, state.harness_job_id, state.scope_id)

    def job(self, state):
        return self.store.get_job(state.harness_job_id, scope_id=state.scope_id)

    def session(self, state):
        return self.store.get_session(state.harness_session_id, scope_id=state.scope_id)

    def _directory(self, job_id):
        if not job_id or any(character in job_id for character in '/\\:') or job_id in {'.', '..'}:
            raise PermissionError('Harness Job 路径无效')
        directory = self.root / job_id
        if any(path.is_symlink() or path.is_junction() for path in (directory, *directory.parents)):
            raise PermissionError('Harness 目录不能经过链接')
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _read(self, state):
        path = self._directory(state.harness_job_id) / 'host-state.json'
        if path.is_symlink() or path.is_junction():
            raise PermissionError('Harness 宿主记录不能经过链接')
        return json.loads(path.read_text(encoding='utf-8'))

    def _write(self, state, value):
        path = self._directory(state.harness_job_id) / 'host-state.json'
        temporary = path.with_suffix('.pending')
        if any(target.is_symlink() or target.is_junction() for target in (path, temporary)):
            raise PermissionError('Harness 宿主记录不能经过链接')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _identity(self, state, session=None):
        session = session or self.session(state)
        return {'scope_id': state.scope_id, 'job_id': state.harness_job_id,
                'session_id': session.session_id, 'run_id': session.run_id}

    def _check(self, engine, state, *, final=False):
        job = self.job(state)
        if (state.mode != 'repair' or digest(state.goal) != job.goal_hash
                or job.config_digest != self.config.config_hash
                or not job.local_commit_authorized):
            raise PermissionError('SDK Job 冻结目标、配置或本地提交授权不匹配')
        allowed = (job.status == 'RUNNING' or final and job.status == 'BLOCKED'
                   and job.blocker in {'FINAL_ACCEPTANCE_FAILED', 'TARGET_BASE_CHANGED'})
        if not allowed or job.stop_request_id:
            raise HarnessConflict('SDK Job 当前不允许启动节点')
        session = self.session(state)
        if session.owner is not None and (session.owner != self.owner
                or session.owner_epoch != job.owner_epoch):
            raise HarnessConflict('SDK Session 执行者或 owner epoch 已失效')
        if not local_repository_config(state.remote_config):
            raise PermissionError('SDK Job 禁止远端 publication')
        engine.scopes.assert_current(engine.context)
        return job

    def attach(self, engine, state, *, console_task_id, local_commit_authorized,
               acceptance_refs=None):
        if state.harness_job_id:
            self._check(engine, state)
            return state
        if state.mode != 'repair' or not local_commit_authorized or not local_repository_config(state.remote_config):
            raise PermissionError('SDK 首版只接受预授权本地提交的 repair 任务')
        if not state.test_spec_ref or not state.test_spec_hash:
            raise ValueError('创建 SDK Job 前必须冻结 TestSpec')
        acceptance_refs = list(acceptance_refs or [state.test_spec_ref])
        if state.test_spec_ref not in acceptance_refs:
            raise PermissionError('长期任务必须保留原始 TestSpec 验收')
        for ref in acceptance_refs:
            engine.get(state, ref)
        snapshot = engine.workspace.require_repository_snapshot(engine.source)
        config_ref = engine.put(state, {'kind': 'harness_configuration',
            'config': self.config.model_dump(mode='json'),
            'authorization': {'local_commit': True, 'allowed_files': engine.workspace.allowed_files,
                              'protected_paths': engine.workspace.protected_paths}}, name='任务冻结配置')
        job = self.store.create_job(HarnessJob(scope_id=state.scope_id,
            repo_id=engine.scopes.projects[state.scope_id].repo_id,
            console_task_id=console_task_id, goal_hash=digest(state.goal),
            origin_base_commit=snapshot['commit'], acceptance_refs=acceptance_refs,
            local_commit_authorized=True, config_ref=config_ref,
            config_digest=self.config.config_hash))
        session = self.store.create_session(HarnessSession(job_id=job.job_id, scope_id=state.scope_id,
            kind='initializer', run_id=state.run_id, input_commit=job.accepted_commit),
            scope_id=state.scope_id, expected_revision=job.revision)
        state = engine.changed(state, harness_job_id=job.job_id,
            harness_session_id=session.session_id, harness_stage='initialize')
        original_files = {path: run_git(engine.workspace.root, 'show',
            engine.workspace.head() + ':' + path).hex() for path in snapshot['files']}
        self._write(state, {'origin_snapshot': snapshot, 'origin_files': original_files,
            'origin_run_id': state.run_id, 'acceptance_refs': acceptance_refs,
            'frozen_spec': engine.get(state, state.test_spec_ref),
            'rule_refs': state.rule_refs, 'lineage': [], 'executions': {}, 'candidates': {}})
        engine.harness = self
        return state

    async def stop(self, engine, state):
        """先持久化停止意图，再向当前 execution 发送中断。"""
        job = self.job(state)
        if job.status not in {'CREATED', 'RUNNING', 'STOPPING'} and not (
                job.status == 'BLOCKED' and job.stop_request_id):
            raise HarnessConflict('当前 Job 不能停止')
        job = self.store.request_stop(job.job_id, job.revision, scope_id=state.scope_id)
        pending = pending_harness_operations(engine.store, state)
        if pending and job.blocker != 'EXTERNAL_OPERATION_UNKNOWN':
            job = self.store.block_job(job.job_id, 'EXTERNAL_OPERATION_UNKNOWN', scope_id=state.scope_id,
                expected_revision=job.revision)
        host = self._read(state)
        executions = self.store.list_executions(job.job_id, scope_id=state.scope_id)
        active = [execution for execution in executions
            if execution.status in {'PREPARED', 'STARTING', 'RUNNING', 'INTERRUPTING', 'UNKNOWN'}]
        selected = active or executions[-1:]
        for execution in selected:
            entry = host.get('executions', {}).get(execution.execution_id)
            if not entry or not Path(entry.get('directory', '')).is_dir():
                continue
            from tracefix.execution.candidate_export import SDKWorkspace
            workspace = SDKWorkspace.load(Path(entry['directory']), engine.workspace)
            runner = self._execution_runner(workspace, execution)
            interrupt = getattr(runner, 'interrupt', None)
            session = self.store.get_session(execution.session_id, scope_id=state.scope_id)
            stop_state = state.model_copy(update={'run_id': session.run_id,
                'harness_session_id': session.session_id})
            if interrupt is not None:
                receipt = interrupt(execution.execution_id, job.stop_request_id,
                    self.config.stop_grace_seconds)
                if inspect.isawaitable(receipt):
                    receipt = await receipt
                receipt_value = receipt.model_dump(mode='json') if hasattr(receipt, 'model_dump') else receipt
                receipt_ref = engine.put(stop_state, receipt_value, name='SDK停止回执')
                host = self._read(state)
                host['executions'][execution.execution_id]['stop_receipt_ref'] = receipt_ref
                self._write(state, host)
            if not await self._writers_stopped(engine, stop_state, execution, runner):
                try:
                    self._update_execution(state, execution, 'INTERRUPTING',
                        stop_request_id=job.stop_request_id)
                except HarnessConflict:
                    pass
                continue
            current = self.store.get_execution(execution.execution_id, scope_id=state.scope_id)
            if current.status == 'STOPPED':
                continue
            try:
                result = ExecutionResult(execution_id=execution.execution_id, status='STOPPED',
                    sdk_session_id=current.sdk_session_id, transcript_ref=current.transcript_ref)
                snapshot = workspace.freeze()
                snapshot_ref = engine.put(stop_state, snapshot.model_dump(), name='停止完整快照')
                boundary = ('node_boundary:' + current.status + ':' + session.status
                    if current.status in {'SUCCEEDED', 'FAILED'} else None)
                pending = pending_harness_operations(engine.store, state)
                checkpoint_execution = current.model_copy(update={'pending_operations': sorted(
                    set(current.pending_operations) | set(pending))})
                checkpoint_ref = self._checkpoint(engine, stop_state, session, checkpoint_execution, workspace,
                    result, snapshot_ref, writers_stopped=True, recovery_reason=boundary)
                job = self.job(state)
                pending = pending_harness_operations(engine.store, state)
                if pending and job.blocker != 'EXTERNAL_OPERATION_UNKNOWN':
                    job = self.store.block_job(job.job_id, 'EXTERNAL_OPERATION_UNKNOWN', scope_id=state.scope_id,
                        expected_revision=job.revision)
                if job.blocker not in {None, 'EXECUTOR_STATE_UNKNOWN'}:
                    host = self._read(state)
                    host['executions'][execution.execution_id]['checkpoint_ref'] = checkpoint_ref
                    self._write(state, host)
                    continue
                if job.status in {'STOPPING', 'BLOCKED'}:
                    self.store.confirm_stopped(job.job_id, checkpoint_ref, scope_id=state.scope_id,
                        expected_revision=job.revision, session_id=session.session_id)
            except (OSError, ValueError):
                job = self.job(state)
                if job.blocker in {None, 'EXECUTOR_STATE_UNKNOWN'}:
                    self.store.block_job(job.job_id, 'CHECKPOINT_FLUSH_FAILED', scope_id=state.scope_id,
                        expected_revision=job.revision)
                raise
        return self.job(state)

    def resume(self, engine, state, inspection_ref, *, owner=None):
        """以新的 owner epoch 恢复已确认停止的同一逻辑 invocation。"""
        job = self.job(state)
        try:
            self.store.claim_resume(job.job_id, job.revision, owner or self.owner,
                scope_id=state.scope_id, inspection_ref=inspection_ref)
        except (OSError, ValueError) as error:
            if not isinstance(error, HarnessConflict):
                current = self.job(state)
                self.store.block_job(job.job_id, 'RECOVERY_ARTIFACTS_MISSING', scope_id=state.scope_id,
                    expected_revision=current.revision)
            raise
        return engine.changed(state, run_status=RunStatus.RUNNING)

    def _update_execution(self, state, execution, status, **fields):
        return self.store.update_execution(execution.execution_id, scope_id=state.scope_id,
            expected_revision=execution.revision, owner=execution.owner,
            owner_epoch=execution.owner_epoch, status=status, **fields)

    def _execution_runner(self, workspace, execution):
        runner = self.runner(workspace) if callable(self.runner) else self.runner
        if hasattr(runner, 'workspace'):
            runner.workspace = workspace
        runner.owner_epoch = execution.owner_epoch
        return runner

    def _emit_owned(self, engine, state, execution, kind, payload):
        current = self.store.get_execution(execution.execution_id, scope_id=state.scope_id)
        job = self.job(state)
        if ((current.owner, current.owner_epoch) != (execution.owner, execution.owner_epoch)
                or job.owner_epoch != execution.owner_epoch
                or current.status not in {'STARTING', 'RUNNING', 'INTERRUPTING'}):
            return
        engine.event(state, kind, {**payload, 'execution_id': execution.execution_id,
            'owner_epoch': execution.owner_epoch})

    async def _writers_stopped(self, engine, state, execution, runner):
        entry = self._read(state).get('executions', {}).get(execution.execution_id, {})
        receipt_ref = entry.get('stop_receipt_ref')
        receipt = engine.get(state, receipt_ref) if receipt_ref else {}
        confirmed = (receipt.get('execution_id') == execution.execution_id
            and receipt.get('stop_request_id') == self.job(state).stop_request_id
            and receipt.get('writers_stopped') is True)
        inspect_execution = getattr(runner, 'inspect', None)
        if inspect_execution is not None:
            inspection = inspect_execution(execution.execution_id)
            if inspect.isawaitable(inspection):
                inspection = await inspection
            if getattr(inspection, 'active', True):
                return False
            if getattr(inspection, 'status', 'UNKNOWN') == 'STOPPED':
                return True
        return confirmed

    def _checkpoint(self, engine, state, session, execution, sdk_workspace, result, snapshot_ref,
                    *, writers_stopped=False, recovery_reason=None):
        transcript_ref = None
        if result.transcript_ref:
            transcript = Path(result.transcript_ref)
            if not transcript.is_file():
                transcript = sdk_workspace.execution_dir / 'state' / 'transcript.jsonl'
            if transcript.is_file() and not transcript.is_symlink():
                transcript_ref = engine.put(state, {'kind': 'sdk_transcript',
                    'execution_id': execution.execution_id,
                    'content': transcript.read_text(encoding='utf-8', errors='replace')},
                    name='SDKtranscript')
        cursor = 0
        if hasattr(self.store, 'outbox_events'):
            try:
                cursor = len(self.store.outbox_events(state.harness_job_id, scope_id=state.scope_id))
            except Exception:
                cursor = 0
        identity = self._identity(state, session)
        checkpoint = ExecutionCheckpoint(**identity, execution_id=execution.execution_id,
            owner_epoch=execution.owner_epoch, status=result.status,
            workspace_snapshot_ref=snapshot_ref, transcript_ref=transcript_ref,
            sdk_session_id=result.sdk_session_id, event_cursor=cursor,
            stop_request_id=self.job(state).stop_request_id,
            pending_operations=list(execution.pending_operations), recovery_reason=recovery_reason,
            writers_alive=not writers_stopped, artifacts_consistent=writers_stopped,
            stop_receipt_ref=self._read(state).get('executions', {}).get(
                execution.execution_id, {}).get('stop_receipt_ref'))
        return engine.put(state, checkpoint.model_dump(mode='json'), name='SDK执行checkpoint')

    async def _execute(self, engine, state, role, prompt, *, output_schema=None):
        from tracefix.execution.candidate_export import SDKWorkspace

        self._check(engine, state)
        session = self.session(state)
        if session.invocation_no == 0:
            session = self.store.allocate_invocation(session.session_id, scope_id=state.scope_id,
                expected_revision=session.revision)
        invocation = f'{session.session_id}:{role}:{session.invocation_no}'
        host = self._read(state)
        invocations = host.setdefault('invocations', {})
        frozen = invocations.get(invocation)
        if frozen is not None:
            if (frozen['config_hash'] != self.config.config_hash
                    or frozen['input_commit'] != session.input_commit
                    or frozen['feedback_ref'] != session.feedback_ref):
                raise HarnessConflict('逻辑调用冻结身份已变化')
            prompt = frozen['prompt']
        else:
            invocations[invocation] = {'prompt': prompt, 'config_hash': self.config.config_hash,
                'input_commit': session.input_commit, 'feedback_ref': session.feedback_ref}
            self._write(state, host)
        input_hash = digest({'prompt': prompt, 'input_commit': session.input_commit,
            'config': self.config.config_hash, 'feedback_ref': session.feedback_ref,
            'invocation': invocation})
        execution = self.store.prepare_execution(session.session_id, role, invocation, input_hash,
            scope_id=state.scope_id, expected_revision=session.revision,
            owner=session.owner or self.owner)
        state = engine.changed(state, sdk_execution_id=execution.execution_id)
        if execution.result_ref and execution.status in {'SUCCEEDED', 'FAILED'}:
            result = ExecutionResult.model_validate(engine.get(state, execution.result_ref))
            return state, execution, result
        if execution.status not in {'PREPARED', 'STARTING'}:
            raise HarnessConflict('已有执行尚未封存；M4 恢复门禁未完成，禁止重复启动')
        if execution.status == 'STARTING':
            checkpoint = (engine.get(state, execution.checkpoint_ref)
                if execution.checkpoint_ref else {})
            if (checkpoint.get('status') != 'STOPPED' or checkpoint.get('writers_alive') is not False
                    or checkpoint.get('artifacts_consistent') is not True
                    or checkpoint.get('owner_epoch', execution.owner_epoch) >= execution.owner_epoch):
                job = self.job(state)
                self.store.block_job(job.job_id, 'EXECUTOR_STATE_UNKNOWN', scope_id=state.scope_id,
                    expected_revision=job.revision)
                raise HarnessConflict('STARTING 执行缺少新 owner 的安全恢复领取凭据')
        directory = self._directory(state.harness_job_id) / 'sessions' / session.session_id / 'executions' / execution.execution_id
        existing = host['executions'].get(execution.execution_id)
        prior = [entry for entry in host['executions'].values()
                 if entry['session_id'] == session.session_id and entry is not existing]
        if existing and Path(existing['directory']).is_dir():
            sdk_workspace = SDKWorkspace.load(Path(existing['directory']), engine.workspace)
            prior_sdk_session = (execution.sdk_session_id if execution.status == 'STARTING'
                else execution.sdk_session_id or existing.get('sdk_session_id'))
        elif prior:
            previous = prior[-1]
            sdk_workspace = SDKWorkspace.load(Path(previous['directory']), engine.workspace)
            sdk_workspace = sdk_workspace.fork_execution(execution.execution_id)
            prior_sdk_session = execution.sdk_session_id or previous.get('sdk_session_id')
        else:
            sdk_workspace = SDKWorkspace.create(engine.workspace, directory)
            prior_sdk_session = execution.sdk_session_id
        arguments = {'execution_id': execution.execution_id, 'session_id': session.session_id,
            'input_hash': input_hash, 'role': role, 'prompt': prompt,
            'workspace': sdk_workspace.source, 'config': self.config,
            'tools': ['Read', 'Glob', 'Grep', 'Agent'] if role == 'initializer'
                else ['Read', 'Glob', 'Grep', 'Edit', 'Write', 'Bash', 'Agent'],
            'system_prompt': SDK_SYSTEM_PROMPT,
            'sdk_session_id': prior_sdk_session, 'feedback_ref': session.feedback_ref,
            'transcript_path': sdk_workspace.execution_dir / 'transcript' / (execution.execution_id + '.jsonl')}
        if output_schema is not None:
            arguments['output_schema'] = output_schema
        request = SdkExecutionRequest(**arguments)
        host['executions'][execution.execution_id] = {'session_id': session.session_id,
            'directory': str(sdk_workspace.execution_dir), 'sdk_session_id': prior_sdk_session,
            'prompt': prompt, 'input_hash': input_hash}
        self._write(state, host)
        runner = self._execution_runner(sdk_workspace, execution)
        resuming = execution.status == 'STARTING'
        if resuming:
            inspect_execution = getattr(runner, 'inspect', None)
            if inspect_execution is not None:
                inspection = inspect_execution(execution.execution_id)
                if inspect.isawaitable(inspection):
                    inspection = await inspection
                if getattr(inspection, 'active', False):
                    raise HarnessConflict('恢复前检测到原 execution 仍有活动写入者')
                if getattr(inspection, 'status', None) == 'UNKNOWN':
                    job = self.job(state)
                    if job.status != 'BLOCKED' or job.blocker != 'EXECUTOR_STATE_UNKNOWN':
                        self.store.block_job(job.job_id, 'EXECUTOR_STATE_UNKNOWN', scope_id=state.scope_id,
                            expected_revision=job.revision)
                    raise HarnessConflict('恢复前无法确认原 execution 已停止')
        if not resuming:
            execution = self._update_execution(state, execution, 'STARTING',
                workspace_ref=str(sdk_workspace.execution_dir), sdk_version=self.config.sdk_version,
                cli_version=self.config.cli_version, model_id=self.config.model_id)
            execution = self._update_execution(state, execution, 'RUNNING')
        else:
            execution = self._update_execution(state, execution, 'RUNNING',
                workspace_ref=str(sdk_workspace.execution_dir))
        engine.event(state, 'sdk.execution.started', {'execution_id': execution.execution_id,
            'logical_invocation_id': invocation, 'owner_epoch': execution.owner_epoch})
        try:
            emit = lambda kind, payload: self._emit_owned(engine, state, execution, kind, payload)
            resume = getattr(runner, 'resume', None) if resuming else None
            if resume is not None:
                parameters = inspect.signature(resume).parameters
                options = {'emit': emit}
                if 'sdk_session_id' in parameters:
                    options['sdk_session_id'] = execution.sdk_session_id
                if 'rebuild' in parameters:
                    options['rebuild'] = not bool(execution.sdk_session_id)
                result = await resume(execution.execution_id, **options)
            else:
                result = await runner.start(request, emit)
        except BaseException:
            current = self.store.get_execution(execution.execution_id, scope_id=state.scope_id)
            if ((current.owner, current.owner_epoch) != (execution.owner, execution.owner_epoch)
                    or current.status in {'STOPPED', 'SUCCEEDED', 'FAILED'}):
                raise
            self._update_execution(state, current, 'UNKNOWN')
            job = self.job(state)
            if job.blocker in {None, 'EXECUTOR_STATE_UNKNOWN'} and job.status != 'BLOCKED':
                self.store.block_job(job.job_id, 'EXECUTOR_STATE_UNKNOWN', scope_id=state.scope_id,
                                     expected_revision=job.revision)
            raise
        result = ExecutionResult.model_validate(result)
        current_execution = self.store.get_execution(execution.execution_id, scope_id=state.scope_id)
        if ((current_execution.owner, current_execution.owner_epoch) != (execution.owner, execution.owner_epoch)
                or current_execution.status == 'STOPPED'):
            raise HarnessConflict('SDK 旧执行者结果已失效')
        execution = current_execution
        current_job = self.job(state)
        if result.status == 'STOPPED' and not current_job.stop_request_id:
            result = result.model_copy(update={'status': 'UNKNOWN', 'error_code': 'SDK_STOP_WITHOUT_INTENT'})
        elif current_job.stop_request_id and result.status in {'SUCCEEDED', 'FAILED'}:
            result = result.model_copy(update={'status': 'STOPPED', 'error_code': 'SDK_STOP_REQUESTED'})
        writers_stopped = await self._writers_stopped(engine, state, execution, runner)
        if result.status == 'STOPPED':
            if not writers_stopped:
                result = result.model_copy(update={'status': 'UNKNOWN',
                    'error_code': 'SDK_STOP_WRITERS_UNCONFIRMED'})
        sdk_workspace = getattr(runner, 'workspace', sdk_workspace)
        try:
            snapshot = sdk_workspace.freeze()
            snapshot_value = snapshot.model_dump() if hasattr(snapshot, 'model_dump') else snapshot
            snapshot_ref = engine.put(state, snapshot_value, name='执行完整快照')
            checkpoint_ref = self._checkpoint(engine, state, session, execution, sdk_workspace, result,
                snapshot_ref, writers_stopped=writers_stopped)
            result_ref = engine.put(state, result.model_dump(mode='json'), name='SDK执行结果')
            execution = self._update_execution(state, execution, result.status,
                result_ref=result_ref, sdk_session_id=result.sdk_session_id,
                observed_usage=result.observed_usage, checkpoint_ref=checkpoint_ref,
                transcript_ref=result.transcript_ref, stop_request_id=current_job.stop_request_id,
                event_cursor=len(self.store.outbox_events(state.harness_job_id, scope_id=state.scope_id)),
                pending_operations=list(result.unresolved) if result.status == 'UNKNOWN' else [])
        except BaseException as error:
            try:
                job = self.job(state)
                self.store.block_job(job.job_id, 'CHECKPOINT_FLUSH_FAILED', scope_id=state.scope_id,
                    expected_revision=job.revision)
            except Exception:
                pass
            raise HarnessConflict('执行 checkpoint 或结果刷盘失败') from error
        host = self._read(state)
        host['executions'][execution.execution_id]['directory'] = str(sdk_workspace.execution_dir)
        host['executions'][execution.execution_id]['sdk_session_id'] = result.sdk_session_id
        self._write(state, host)
        if result.status == 'STOPPED':
            job = self.job(state)
            if job.stop_request_id:
                self.store.confirm_stopped(job.job_id, execution.checkpoint_ref, scope_id=state.scope_id,
                    expected_revision=job.revision, session_id=session.session_id)
            raise HarnessConflict('SDK 执行已停止，等待显式恢复')
        if result.status == 'UNKNOWN':
            job = self.job(state)
            if job.status != 'BLOCKED' or job.blocker != 'EXECUTOR_STATE_UNKNOWN':
                self.store.block_job(job.job_id, 'EXECUTOR_STATE_UNKNOWN', scope_id=state.scope_id,
                                     expected_revision=job.revision)
            raise HarnessConflict('SDK 执行尚未确认结束，拒绝导出或接纳候选')
        return state, execution, result

    def validate_workspace(self, engine, state, snapshot):
        from tracefix.execution.harness_revision import validate_workspace_revision

        host = self._read(state)
        receipts = [RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
            entry['run_id'], entry['ref'])) for entry in host['lineage']]
        return validate_workspace_revision(engine.workspace, snapshot, job=self.job(state),
            project=engine.scopes.projects[state.scope_id], original_snapshot=host['origin_snapshot'],
            receipts=receipts, base_commit=state.patch_base_commit, patch_hash=state.patch_hash,
            branch=state.local_branch or 'tracefix/' + state.run_id)

    def require_current_initialization(self, engine, state):
        job = self.job(state)
        initializer = next((session for session in reversed(self.store.list_sessions(job.job_id,
            scope_id=state.scope_id)) if session.kind == 'initializer' and session.status == 'COMPLETED'), None)
        if initializer is None or not job.initialization_ref:
            raise HarnessConflict('SDK 编码缺少宿主初始化清单')
        manifest = InitializationManifest.model_validate(engine.artifacts.json(state.scope_id,
            initializer.run_id, job.initialization_ref))
        if not initialization_valid(manifest, goal_hash=job.goal_hash,
                environment_digest=environment_key(engine, state, self.config)):
            self.store.block_job(job.job_id, 'INITIALIZATION_STALE', scope_id=state.scope_id,
                                 expected_revision=job.revision)
            raise HarnessConflict('依赖锁、配置或运行镜像变化，初始化清单已失效')

    async def initialize(self, engine, state):
        job = self._check(engine, state)
        key = environment_key(engine, state, self.config)
        if job.initialization_ref:
            initializer = next(session for session in reversed(self.store.list_sessions(job.job_id,
                scope_id=state.scope_id)) if session.kind == 'initializer' and session.status == 'COMPLETED')
            manifest = InitializationManifest.model_validate(engine.artifacts.json(state.scope_id,
                initializer.run_id, job.initialization_ref))
            if initialization_valid(manifest, goal_hash=job.goal_hash, environment_digest=key):
                return await self.claim(engine, state)
        session = self.session(state)
        if session.kind != 'initializer' or session.status != 'RUNNING':
            session = self.store.create_session(HarnessSession(job_id=job.job_id, scope_id=state.scope_id,
                kind='initializer', run_id=state.run_id, input_commit=job.accepted_commit),
                scope_id=state.scope_id, expected_revision=job.revision)
            state = engine.changed(state, harness_session_id=session.session_id, harness_task_id=None)
        writable = []
        for path in engine.source['files']:
            try:
                engine.workspace.path(path, write=True)
            except PermissionError:
                continue
            writable.append(path)
        schema = {'type': 'object', 'properties': {'commands': {'type': 'array', 'items': {
            'type': 'array', 'items': {'type': 'string'}}}, 'work_items': {'type': 'array',
            'items': {'type': 'object', 'properties': {'id': {'type': 'string'},
                'title': {'type': 'string'}, 'depends_on': {'type': 'array', 'items': {'type': 'string'}},
                'acceptance_refs': {'type': 'array', 'items': {'type': 'string'}},
                'allowed_files': {'type': 'array', 'items': {'type': 'string'}},
                'writable_files': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['id', 'title', 'depends_on', 'acceptance_refs', 'allowed_files', 'writable_files'],
                'additionalProperties': False}}}, 'required': ['commands', 'work_items'],
            'additionalProperties': False}
        prompt = json.dumps({'goal': state.goal, 'input_commit': job.accepted_commit,
            'acceptance_refs': job.acceptance_refs, 'allowed_files': list(engine.source['files']),
            'writable_files': writable, 'approved_commands': engine.profile.commands,
            'instruction': '只读检查环境，提出依赖无环的最小增量计划；不得改变冻结验收项。'}, ensure_ascii=False)
        state, execution, result = await self._execute(engine, state, 'initializer', prompt,
                                                     output_schema=schema)
        from tracefix.execution.candidate_export import SDKWorkspace
        sdk_record = self._read(state)['executions'][execution.execution_id]
        sdk_workspace = SDKWorkspace.load(Path(sdk_record['directory']), engine.workspace)
        if sdk_workspace.freeze() != sdk_workspace.input_snapshot:
            raise PermissionError('只读 initializer 修改了源码副本')
        if result.status != 'SUCCEEDED' or result.structured_output is None:
            raise ValueError('initializer 未形成有效结构化计划')
        proposal = result.structured_output
        commands = command_suggestions(proposal['commands'], engine.profile)
        manifest = InitializationManifest(**self._identity(state), goal_hash=job.goal_hash,
            policy_version=POLICY_VERSION, input_commit=job.accepted_commit,
            environment_digest=key, commands=commands, healthy=bool(state.source_aligned),
            versions={'sdk': self.config.sdk_version, 'cli': self.config.cli_version,
                      'worker_image': self.config.worker_image, 'harness': HARNESS_VERSION})
        manifest_ref = engine.put(state, manifest.model_dump(mode='json'), name='初始化清单')
        plan = None
        if not job.plan_ref:
            items = [HarnessWorkItem(harness_task_id=item['id'], job_id=job.job_id,
                scope_id=state.scope_id, **{key: value for key, value in item.items() if key != 'id'})
                for item in proposal['work_items']]
            plan = validate_plan(TaskPlan(**self._identity(state), plan_version=job.plan_version + 1,
                acceptance_refs=job.acceptance_refs, work_items=items), job, engine.workspace)
            plan_ref = engine.put(state, plan.model_dump(mode='json'), name='初始化任务计划')
        session = self.session(state)
        job = self.store.finish_initialization(session.session_id, manifest_ref,
            scope_id=state.scope_id, expected_revision=session.revision)
        if plan is not None:
            self.store.install_plan(job.job_id, plan_ref, scope_id=state.scope_id,
                                    expected_revision=job.revision)
        engine.event(state, 'harness.initialized', {'execution_id': execution.execution_id,
                                                 'initialization_ref': manifest_ref})
        return await self.claim(engine, state)

    async def claim(self, engine, state):
        job = self._check(engine, state)
        items = self.store.list_work_items(job.job_id, scope_id=state.scope_id)
        if items and all(item.status == 'VERIFIED' for item in items):
            if state.run_status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED,
                                    RunStatus.ABNORMAL, RunStatus.SUPERSEDED}:
                return engine.output(state, 'harness_final')
            return engine.output(engine.changed(state, harness_stage='final_acceptance'), 'harness_final')
        active = [session for session in self.store.list_sessions(job.job_id, scope_id=state.scope_id)
                  if session.kind == 'coder' and session.status == 'RUNNING']
        if active:
            session = active[0]
            child = engine.store.load(session.run_id, state.scope_id)
            if engine.workspace.root.as_posix() != engine.artifacts.json(child.scope_id,
                    child.run_id, child.repo_snapshot_ref)['workspace_root']:
                from tracefix.execution.workspace import Workspace
                snapshot = engine.artifacts.json(child.scope_id, child.run_id, child.repo_snapshot_ref)
                workspace = Workspace(Path(snapshot['workspace_root']), engine.workspace.allowed_files,
                    protected_paths=engine.workspace.protected_paths)
                workspace.repository_snapshot = snapshot
                await self._rebind(engine, workspace, snapshot)
            engine.current_run = child.run_id
            return engine.output(child, engine.route(child))
        item = self.store.claim_work_item(job.job_id, job.revision, self.owner, scope_id=state.scope_id)
        job = self.job(state)
        host = self._read(state)
        child = derive_run(state, rule_ids=state.rule_ids)
        workspace, source = engine.workspace, engine.source
        if host['lineage']:
            last = host['lineage'][-1]
            receipt = RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
                last['run_id'], last['ref']))
            previous = None
            if len(host['lineage']) > 1:
                prior = host['lineage'][-2]
                previous = RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
                    prior['run_id'], prior['ref']))
            workspace, source = materialize_revision(receipt,
                self._directory(job.job_id) / 'runs' / child.run_id / 'workspace', job=job,
                project=engine.scopes.projects[state.scope_id],
                original_snapshot=host['origin_snapshot'], previous=previous,
                receipts=[RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
                    entry['run_id'], entry['ref'])) for entry in host['lineage']])
        else:
            from tracefix.execution.workspace import Workspace
            workspace, source = Workspace.export(engine.scopes, engine.context, job.accepted_commit,
                self._directory(job.job_id) / 'runs' / child.run_id / 'workspace')
        child.harness_job_id = job.job_id
        child.harness_task_id = item.harness_task_id
        child.harness_stage = 'prepare_increment'
        child.sdk_execution_id = None
        child.source_manifest = digest(source)
        child.repo_snapshot_ref = engine.artifacts.put(child.scope_id, child.run_id, source)
        child.test_spec_ref = engine.artifacts.put(child.scope_id, child.run_id, host['frozen_spec'])
        child.test_spec_hash = digest(host['frozen_spec'])
        from tracefix.runtime.harness_finding import carry_finding_acceptance
        try:
            child = carry_finding_acceptance(engine, state, child, source)
        except Exception:
            current = self.job(state)
            if current.status == 'RUNNING':
                self.store.block_job(current.job_id, 'FINDING_ACCEPTANCE_BLOCKED',
                    scope_id=state.scope_id, expected_revision=current.revision)
            raise
        await self._rebind(engine, workspace, source)
        session = self.store.create_session(HarnessSession(job_id=job.job_id, scope_id=state.scope_id,
            kind='coder', harness_task_id=item.harness_task_id, run_id=child.run_id,
            input_commit=job.accepted_commit), scope_id=state.scope_id, expected_revision=job.revision)
        child.harness_session_id = session.session_id
        if state.agent_instructions_ref:
            child.agent_instructions_ref = engine.artifacts.put(child.scope_id, child.run_id,
                engine.artifacts.read(state.scope_id, state.run_id, state.agent_instructions_ref), 'txt')
        engine.claim_harness_writer(child.run_id)
        engine.store.save(child)
        engine.current_run = child.run_id
        engine.event(child, 'harness.task.claimed', {'harness_task_id': item.harness_task_id,
            'input_commit': session.input_commit, 'owner_epoch': session.owner_epoch})
        return engine.output(child, 'prepare')

    async def _rebind(self, engine, workspace, source):
        await engine.browser.close()
        await engine.runner.close()
        if self.rebind is not None:
            await self.rebind(engine, workspace, source)
            return
        from tracefix.execution.browser import MCPBrowser
        from tracefix.execution.policy import Policy
        from tracefix.execution.runner import DockerRunner
        if not isinstance(engine.runner, DockerRunner):
            raise RuntimeError('SDK Harness 需要宿主提供 Runner/browser 重建工厂')
        engine.workspace, engine.source = workspace, source
        engine.runner = DockerRunner(engine.profile, workspace, new_id('henv'))
        engine.browser = MCPBrowser(engine.runner.browser_command(), Policy(engine.profile.allowed_origins))

    async def code(self, engine, state):
        from tracefix.execution.candidate_export import SDKWorkspace, export_candidate, materialize_candidate

        self._check(engine, state)
        self.require_current_initialization(engine, state)
        if not state.source_aligned or not state.test_spec_ref:
            raise PermissionError('SDK 编码前必须通过源码与冻结验收准备')
        engine.require_repair_rule_coverage(state)
        if not state.reproduced and not state.initial_check_result_refs:
            raise PermissionError('SDK 编码前必须保留原问题检测/复现证据')
        session = self.session(state)
        item = self.store.get_work_item(session.harness_task_id, scope_id=state.scope_id)
        feedback = engine.get(state, session.feedback_ref) if session.feedback_ref else None
        if feedback:
            verification = engine.get(state, feedback['verification_ref'])
            observations = []
            for command in verification['commands']:
                evidence = engine.get(state, command['evidence_ref'])
                observations.extend(engine.get(state, ref) for ref in evidence.get('receipt_refs', []))
            feedback = {**feedback, 'verification': verification, 'observations': observations}
        prompt = json.dumps({'goal': state.goal, 'task': item.model_dump(mode='json'),
            'input_commit': session.input_commit, 'test_spec': engine.spec(state).model_dump(mode='json'),
            'rule_refs': state.rule_refs, 'feedback': feedback,
            'instruction': '仅推进当前增量；原生工具编辑副本。宿主独立执行验收。'}, ensure_ascii=False)
        state, execution, result = await self._execute(engine, state, 'coder', prompt)
        host = self._read(state)
        info = host['executions'][execution.execution_id]
        sdk_workspace = SDKWorkspace.load(Path(info['directory']), engine.workspace)
        proposal = export_candidate(sdk_workspace, engine.workspace,
            summary=result.summary or 'SDK 实际文件候选', evidence_refs=state.evidence_refs,
            rule_refs=state.rule_refs)
        if not {edit.path for edit in proposal.edits} <= set(item.writable_files):
            raise PermissionError('SDK 候选越过当前增量文件授权')
        current = host['candidates'].get(session.session_id)
        if not current or current['execution_id'] != execution.execution_id:
            target = engine.workspace.root.with_name('candidate-' + execution.execution_id)
            fresh = materialize_candidate(sdk_workspace, proposal, target)
            await engine.browser.close()
            await engine.runner.close()
            stable = engine.workspace.root
            archive = stable.with_name('archived-' + new_id('candidate'))
            os.replace(stable, archive)
            try:
                os.replace(fresh.root, stable)
            except BaseException:
                os.replace(archive, stable)
                raise
            fresh.root = stable
            fresh.repository_snapshot = deepcopy(engine.source)
            await self._rebind(engine, fresh, engine.source)
            source_hash = state.source_manifest
            environment = await engine.runner.inspect_images()
            if environment != state.environment_digest:
                raise PermissionError('SDK 验证副本运行镜像发生变化')
            command_mode = engine.spec(state).mode != 'gui'
            engine.runner.command_only = command_mode
            health = await (engine.runner.start_commands(source_hash) if command_mode
                            else engine.runner.start(source_hash))
            if health.get('passed') is not True:
                raise RuntimeError('SDK 候选验证环境启动失败')
            if not command_mode:
                await engine.browser.open()
            host['candidates'][session.session_id] = {'execution_id': execution.execution_id,
                'archive': str(archive), 'workspace': str(stable)}
            self._write(state, host)
        patch_ref = engine.put(state, proposal.model_dump(mode='json'), name='SDK实际候选')
        state = engine.changed(state, phase=Phase.PATCH, patch_ref=patch_ref,
            patch_base_commit=engine.source['workspace_repository']['head'],
            harness_stage='candidate', validation_refs=[], validation_index=0,
            check_result_refs=[], behavior_check_refs=[])
        return engine.output(state, 'patch')

    def bind_candidate(self, engine, state):
        session = self.session(state)
        self.store.bind_candidate(session.session_id, scope_id=state.scope_id,
            expected_revision=session.revision, owner=session.owner, owner_epoch=session.owner_epoch,
            source_manifest=state.source_manifest, patch_hash=state.patch_hash,
            environment_digest=state.environment_digest, test_spec_hash=state.test_spec_hash,
            check_plan_hash=state.check_plan_hash or engine.verification_context(state)['plan_hash'])

    def has_verified_predecessor(self, engine, state):
        job = self.job(state)
        session = self.session(state)
        if (session.kind != 'coder' or session.run_id != state.run_id
                or session.input_commit == job.origin_base_commit):
            return False
        host = self._read(state)
        entries = host['lineage']
        match = next((index for index, entry in enumerate(entries)
                      if entry['commit'] == session.input_commit), None)
        if match is None or digest(host['frozen_spec']) != state.test_spec_hash:
            return False
        previous = None
        for entry in entries[:match + 1]:
            receipt = RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
                entry['run_id'], entry['ref']))
            accepted = self.store.get_session(receipt.session_id, scope_id=state.scope_id)
            if (accepted.status != 'COMPLETED' or accepted.candidate_commit != receipt.candidate_commit
                    or accepted.verification_ref != receipt.verification_ref):
                return False
            validate_receipt(receipt, job=job, project=engine.scopes.projects[state.scope_id], previous=previous)
            previous = receipt
        return previous is not None

    def verification_record(self, engine, state, *, candidate_commit, passed,
                            acceptance_refs, patch_hash=None, target_commit=None):
        from tracefix.runtime.harness_contracts import VerificationEvidence

        session = self.session(state)
        identity = self._identity(state)
        binding = {'candidate_commit': candidate_commit, 'source_manifest': state.source_manifest,
            'patch_hash': patch_hash or state.patch_hash, 'environment_digest': state.environment_digest,
            'test_spec_hash': state.test_spec_hash,
            'check_plan_hash': state.check_plan_hash or engine.verification_context(state)['plan_hash']}
        commands = []
        for ref in state.validation_refs:
            wrapper = engine.get(state, ref)
            result = engine.get(state, wrapper['artifact_ref'])
            aggregate = 'command_results' in result
            results = result['command_results'] if aggregate else [result]
            if aggregate and (type(results) is not list or not results):
                raise PermissionError('回归验收缺少实际子命令结果')
            for command_result in results:
                if type(command_result) is not dict:
                    raise PermissionError('实际命令结果必须是对象')
                if type(command_result.get('exit_code')) is not int:
                    if aggregate or 'executed_argv' in command_result:
                        raise PermissionError('实际命令结果缺少整数退出码')
                    continue
                command = command_result.get('executed_argv')
                if 'executed_argv' not in command_result and not aggregate:
                    if wrapper['kind'] not in {'static', 'unit', 'build'}:
                        if wrapper['kind'] == 'health':
                            continue
                        raise PermissionError('验收缺少实际执行命令参数')
                    command = engine.profile.commands.get(wrapper['kind'])
                if (type(command) is not list or not command
                        or any(type(arg) is not str or not arg for arg in command)):
                    raise PermissionError('实际执行命令参数无效')
                receipt_refs = [ref, wrapper['artifact_ref']]
                if aggregate:
                    receipt_refs.append(engine.put(state, command_result, name='实际回归子命令结果'))
                evidence = VerificationEvidence(**identity, **binding, command=command,
                    exit_code=command_result['exit_code'], receipt_refs=receipt_refs)
                evidence_ref = engine.put(state, evidence.model_dump(mode='json'), name='宿主验收命令回执')
                commands.append(VerificationCommand(command=command, exit_code=command_result['exit_code'],
                                                   evidence_ref=evidence_ref))
        if not commands:
            raise PermissionError('验收缺少实际命令退出码')
        return VerificationRecord(**identity, **binding, input_commit=session.input_commit,
            acceptance_refs=acceptance_refs, commands=commands, passed=passed,
            reproduction_refs=list(state.evidence_refs), regression_refs=list(state.validation_refs),
            target_commit=target_commit)

    def failed_feedback(self, engine, state):
        session = self.session(state)
        execution = self.store.get_execution(state.sdk_execution_id, scope_id=state.scope_id)
        item = self.store.get_work_item(session.harness_task_id, scope_id=state.scope_id)
        host = self._read(state)
        feedback_key = digest({'execution': execution.execution_id, 'patch': state.patch_hash,
            'validation': state.validation_refs, 'checks': state.check_result_refs})
        existing = host.setdefault('feedback', {}).get(feedback_key)
        if existing and session.feedback_ref == existing:
            return state
        record = self.verification_record(engine, state, candidate_commit=engine.workspace.head(),
                                          passed=False, acceptance_refs=item.acceptance_refs)
        verification_ref = engine.put(state, record.model_dump(mode='json'), name='失败候选验收')
        feedback = HarnessFeedback(**self._identity(state), execution_id=execution.execution_id,
            candidate_commit=engine.workspace.head(), source_manifest=state.source_manifest,
            verification_ref=verification_ref, summary='宿主确定性验证失败，读取失败命令与证据修正候选')
        feedback_ref = engine.put(state, feedback.model_dump(mode='json'), name='候选验收反馈')
        self.store.allocate_invocation(session.session_id, scope_id=state.scope_id,
            expected_revision=session.revision, feedback_ref=feedback_ref)
        host['feedback'][feedback_key] = feedback_ref
        self._write(state, host)
        engine.event(state, 'harness.gate.decided', {'passed': False, 'feedback_ref': feedback_ref})
        return state

    async def review(self, engine, state):
        self._check(engine, state)
        if not await engine.runtime_verification_passed(state):
            state = self.failed_feedback(engine, state)
            return engine.output(engine.changed(state, phase=Phase.DIAGNOSE,
                outcome=None, harness_stage='coding'), 'harness_code')
        return engine.output(engine.changed(state, phase=Phase.FINALIZE,
            run_status=RunStatus.RUNNING, outcome=Outcome.FIX_VERIFIED,
            harness_stage='handoff'), 'finalize')

    async def accept(self, engine, state):
        job = self._check(engine, state)
        session = self.session(state)
        if session.status == 'COMPLETED':
            self._recover_lineage(engine, state, session)
            return engine.output(state, 'harness_claim')
        if (state.outcome != Outcome.FIX_VERIFIED or not state.local_branch
                or not engine.verification_passed(state)):
            raise PermissionError('增量尚无通过独立验收的本地候选提交')
        self.bind_candidate(engine, state)
        session = self.session(state)
        item = self.store.get_work_item(session.harness_task_id, scope_id=state.scope_id)
        candidate_commit = engine.workspace.head()
        verification = self.verification_record(engine, state, candidate_commit=candidate_commit,
            passed=True, acceptance_refs=item.acceptance_refs)
        verification_ref = engine.put(state, verification.model_dump(mode='json'), name='增量验收记录')
        handoff = SessionHandoff(**self._identity(state), input_commit=session.input_commit,
            candidate_commit=candidate_commit, source_manifest=state.source_manifest,
            patch_hash=state.patch_hash, verification_ref=verification_ref,
            completed=[item.harness_task_id], environment_digest=state.environment_digest)
        handoff_ref = engine.put(state, handoff.model_dump(mode='json'), name='增量会话交接')
        host = self._read(state)
        previous = host['lineage'][-1] if host['lineage'] else None
        receipt = RevisionReceipt(**self._identity(state), origin_base_commit=job.origin_base_commit,
            input_commit=session.input_commit, candidate_commit=candidate_commit,
            candidate_repository=str(engine.workspace.root), export_base_commit=state.patch_base_commit,
            input_tree=run_git(engine.workspace.root, 'rev-parse', state.patch_base_commit + '^{tree}').decode().strip(),
            candidate_tree=run_git(engine.workspace.root, 'rev-parse', candidate_commit + '^{tree}').decode().strip(),
            input_files=tracked_files(engine.workspace.root, state.patch_base_commit),
            candidate_files=tracked_files(engine.workspace.root, candidate_commit), patch_hash=state.patch_hash,
            verification_ref=verification_ref, previous_receipt_ref=previous['ref'] if previous else None,
            previous_run_id=previous['run_id'] if previous else None,
            registered_config_hash=digest(engine.scopes.projects[state.scope_id].model_dump(mode='json')))
        prior_receipt = RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
            previous['run_id'], previous['ref'])) if previous else None
        validate_receipt(receipt, job=job, project=engine.scopes.projects[state.scope_id], previous=prior_receipt)
        receipt_ref = engine.put(state, receipt.model_dump(mode='json'), name='已验收提交回执')
        host['pending_accept'] = {'run_id': state.run_id, 'ref': receipt_ref,
            'commit': candidate_commit, 'session_id': session.session_id}
        self._write(state, host)
        self.store.accept_increment(session.session_id, session.revision, verification_ref,
            candidate_commit, handoff_ref, scope_id=state.scope_id,
            owner=session.owner, owner_epoch=session.owner_epoch)
        host['lineage'].append({'run_id': state.run_id, 'ref': receipt_ref, 'commit': candidate_commit})
        host.pop('pending_accept', None)
        self._write(state, host)
        engine.event(state, 'harness.increment.accepted', {'candidate_commit': candidate_commit,
            'handoff_ref': handoff_ref, 'revision_ref': receipt_ref})
        return engine.output(state, 'harness_claim')

    def _recover_lineage(self, engine, state, session):
        host = self._read(state)
        pending = host.get('pending_accept')
        if not pending:
            if not any(entry['commit'] == session.candidate_commit for entry in host['lineage']):
                raise HarnessConflict('接纳回执缺失，拒绝从旧基准开始下一增量')
            return
        if pending['session_id'] != session.session_id or pending['commit'] != session.candidate_commit:
            raise HarnessConflict('待接纳回执与权威状态不一致')
        receipt = RevisionReceipt.model_validate(engine.artifacts.json(state.scope_id,
            pending['run_id'], pending['ref']))
        if receipt.verification_ref != session.verification_ref:
            raise PermissionError('接纳恢复验收引用不匹配')
        if not any(entry['commit'] == pending['commit'] for entry in host['lineage']):
            host['lineage'].append({key: pending[key] for key in ('run_id', 'ref', 'commit')})
        host.pop('pending_accept', None)
        self._write(state, host)

    async def final(self, engine, state):
        job = self.job(state)
        if job.status == 'COMPLETED' or (job.status == 'BLOCKED' and
                job.blocker in {'FINAL_ACCEPTANCE_FAILED', 'TARGET_BASE_CHANGED'}):
            target = run_git(engine.scopes.projects[state.scope_id].root,
                             'rev-parse', 'HEAD').decode().strip()
            job = self.store.invalidate_target(job.job_id, target, scope_id=state.scope_id,
                                               expected_revision=job.revision)
            self.project(state)
            if job.status == 'COMPLETED':
                return engine.output(state, 'end')
        self._check(engine, state, final=True)
        host = self._read(state)
        diff = cumulative_diff({path: bytes.fromhex(value) for path, value in host['origin_files'].items()},
                               engine.workspace.root, job.accepted_commit)
        diff_ref = engine.put(state, diff, 'diff', name='长期任务累计差异')
        try:
            record = await self.final_verifier(engine, state, job, diff_ref, digest(diff.encode()))
            record = VerificationRecord.model_validate(record)
            if (record.patch_hash != digest(diff.encode()) or record.candidate_commit != job.accepted_commit
                    or not record.target_commit):
                raise PermissionError('最终验收没有绑定实际累计内容与目标分支')
            ref = engine.put(state, record.model_dump(mode='json'), name='最终累计验收')
            job = self.store.finalize_job(job.job_id, ref, scope_id=state.scope_id,
                expected_revision=job.revision, session_id=state.harness_session_id)
        except Exception as error:
            current_job = self.job(state)
            self.store.block_job(job.job_id, 'FINAL_ACCEPTANCE_FAILED', scope_id=state.scope_id,
                                 expected_revision=current_job.revision)
            engine.event(state, 'harness.gate.decided', {'passed': False,
                'blocker': 'FINAL_ACCEPTANCE_FAILED', 'error_type': type(error).__name__,
                'cumulative_diff_ref': diff_ref})
            self.project(state)
            return engine.output(state, 'end')
        engine.event(state, 'harness.gate.decided', {'passed': job.status == 'COMPLETED',
            'final_verification_ref': ref, 'cumulative_diff_ref': diff_ref, 'quality': job.quality})
        engine.put(state, {'job_id': job.job_id, 'status': job.status, 'quality': job.quality,
            'origin_base_commit': job.origin_base_commit, 'accepted_commit': job.accepted_commit,
            'cumulative_diff_ref': diff_ref, 'verification_ref': ref,
            'increments': host['lineage'], 'blocker': job.blocker}, name='长期任务最终报告')
        self.project(state)
        return engine.output(state, 'end')

    async def _final_verification(self, engine, state, job, diff_ref, patch_hash):
        project = engine.scopes.projects[state.scope_id]
        target = run_git(project.root, 'rev-parse', 'HEAD').decode().strip()
        if engine.workspace.head() != job.accepted_commit:
            raise PermissionError('最终验收工作区不是已接纳提交')
        original_spec = self._read(state)['frozen_spec']
        if engine.spec(state).model_dump(mode='json') != original_spec:
            raise PermissionError('最终验收不得削弱原始 TestSpec')
        environment = await engine.runner.inspect_images()
        if environment != state.environment_digest:
            raise PermissionError('最终验收环境发生漂移')
        original = engine.store.load(state.run_id, state.scope_id)
        host = self._read(state)
        attempt = new_id('final')
        host.setdefault('final_attempts', []).append({'attempt_id': attempt,
            'candidate_commit': job.accepted_commit, 'target_commit': target})
        self._write(state, host)
        previous_attempt = getattr(engine, '_harness_final_attempt', None)
        engine._harness_final_attempt = attempt
        command_mode = engine.spec(state).mode != 'gui'
        engine.runner.command_only = command_mode
        candidate_files = tracked_files(engine.workspace.root, job.accepted_commit)
        current = state.model_copy(deep=True, update={'phase': Phase.VERIFY,
            'revision': original.revision + 1,
            'run_status': RunStatus.RUNNING, 'harness_stage': 'final_acceptance',
            'validation_refs': [], 'validation_index': 0, 'replay_index': 0,
            'behavior_check_refs': [], 'check_result_refs': [],
            'check_suite_stage': None, 'check_suite_completed': False,
            'finding_verification_ref': None, 'outcome': None})
        try:
            engine.store.save(current)
            health = await (engine.runner.start_commands(state.source_manifest) if command_mode
                            else engine.runner.start(state.source_manifest))
            if health.get('passed') is not True:
                raise RuntimeError('最终累计验收环境启动失败')
            if not command_mode:
                await engine.browser.open()
            for _step in range(1000):
                if current.check_suite_stage and not current.check_suite_completed:
                    output = await engine.check_suite(current, 'check_suite')
                else:
                    output = await engine.verify(current, 'verify')
                current = RunState.model_validate(output['data'])
                if current.phase != Phase.VERIFY:
                    break
            passed = (current.phase == Phase.REVIEW and await engine.runtime_verification_passed(current))
            if tracked_files(engine.workspace.root, job.accepted_commit) != candidate_files:
                raise PermissionError('最终验收候选提交发生变化')
            if run_git(project.root, 'rev-parse', 'HEAD').decode().strip() != target:
                passed = False
            record = self.verification_record(engine, current, candidate_commit=job.accepted_commit,
                passed=passed, acceptance_refs=job.acceptance_refs, patch_hash=patch_hash,
                target_commit=target)
            record.input_commit = job.origin_base_commit
            return record
        finally:
            try:
                await engine.browser.close()
            finally:
                try:
                    await engine.runner.close()
                finally:
                    engine._harness_final_attempt = previous_attempt
                    latest = engine.store.load(state.run_id, state.scope_id)
                    restored = original.model_copy(deep=True,
                        update={'revision': latest.revision + 1,
                            'budget': latest.budget.model_copy(deep=True),
                            'model_exchange_refs': list(dict.fromkeys(
                                original.model_exchange_refs + latest.model_exchange_refs)),
                            'reasoning_refs': list(dict.fromkeys(
                                original.reasoning_refs + latest.reasoning_refs))})
                    engine.store.save(restored)
                    engine.current_run = restored.run_id
