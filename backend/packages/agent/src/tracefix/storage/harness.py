from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager

from tracefix.runtime.contracts import new_id
from tracefix.runtime.harness_contracts import (
    ExecutionAttempt, ExecutionCheckpoint, HarnessEvent, HarnessFeedback, HarnessJob, HarnessSession,
    HarnessWorkItem, InitializationManifest, SdkExecution, SessionHandoff,
    TaskPlan, VerificationRecord, VerificationEvidence,
)


class HarnessConflict(ValueError):
    pass


class MemoryHarnessStore:
    def __init__(self, artifacts, clock=time.time):
        self.artifacts = artifacts
        self.clock = clock
        self.jobs, self.items, self.sessions, self.executions = {}, {}, {}, {}
        self.outbox, self.projections = {}, {}
        self._lock = threading.RLock()

    @contextmanager
    def _atomic(self, job_id, scope_id):
        with self._lock:
            names = ('jobs', 'items', 'sessions', 'executions', 'outbox', 'projections')
            previous = copy.deepcopy([getattr(self, name) for name in names])
            try:
                if job_id in self.jobs and self.jobs[job_id].scope_id != scope_id:
                    raise PermissionError('Job 不属于当前 scope')
                yield
            except BaseException:
                for name, value in zip(names, previous):
                    setattr(self, name, value)
                raise

    @staticmethod
    def _cas(record, expected_revision):
        if record.revision != expected_revision:
            raise HarnessConflict('revision 已过期')

    def _job(self, job_id, scope_id):
        job = self.jobs.get(job_id)
        if job is None or job.scope_id != scope_id:
            raise PermissionError('当前 scope 中不存在 Job')
        return job

    def _record(self, collection, identity, scope_id):
        record = collection.get(identity)
        if record is None or record.scope_id != scope_id:
            raise PermissionError('当前 scope 中不存在记录')
        return record

    def _event(self, job, kind, payload=None):
        events = self.outbox.setdefault(job.job_id, [])
        event = HarnessEvent(job_id=job.job_id, scope_id=job.scope_id,
            seq=len(events) + 1, owner_epoch=job.owner_epoch, type=kind,
            revision=job.revision, payload={**(payload or {}), 'job': job.model_dump(mode='json')})
        events.append(event)
        return event

    def _artifact(self, model, session, ref):
        value = model.model_validate(self.artifacts.json(session.scope_id, session.run_id, ref))
        for field in ('scope_id', 'job_id', 'session_id', 'run_id'):
            if getattr(value, field) != getattr(session, field):
                raise PermissionError('工件身份不匹配')
        return value

    def _verification_evidence(self, session, verification):
        for command in verification.commands:
            evidence = self._artifact(VerificationEvidence, session, command.evidence_ref)
            for field in ('candidate_commit', 'source_manifest', 'patch_hash', 'environment_digest',
                          'test_spec_hash', 'check_plan_hash'):
                if getattr(evidence, field) != getattr(verification, field):
                    raise HarnessConflict('实际命令回执与验证内容身份不一致')
            if evidence.command != command.command or evidence.exit_code != command.exit_code:
                raise HarnessConflict('实际命令回执与验证退出码不一致')
            for ref in evidence.receipt_refs:
                self.artifacts.read(session.scope_id, session.run_id, ref)

    def create_job(self, job):
        job = HarnessJob.model_validate(job.model_dump())
        with self._atomic(job.job_id, job.scope_id):
            if job.job_id in self.jobs:
                raise HarnessConflict('Job 已存在')
            if job.accepted_commit != job.origin_base_commit or job.revision or job.status != 'CREATED':
                raise HarnessConflict('新 Job 必须使用原始基准')
            self.jobs[job.job_id] = job
            self._event(job, 'harness.created')
            return job.model_copy(deep=True)

    def get_job(self, job_id, *, scope_id):
        with self._atomic(job_id, scope_id):
            return self._job(job_id, scope_id).model_copy(deep=True)

    def _lookup(self, collection_name, identity, scope_id):
        with self._lock:
            record = self._record(getattr(self, collection_name), identity, scope_id)
            job_id = record.job_id
        with self._atomic(job_id, scope_id):
            return self._record(getattr(self, collection_name), identity, scope_id).model_copy(deep=True)

    def get_session(self, session_id, *, scope_id):
        return self._lookup('sessions', session_id, scope_id)

    def get_work_item(self, task_id, *, scope_id):
        return self._lookup('items', task_id, scope_id)

    def get_execution(self, execution_id, *, scope_id):
        return self._lookup('executions', execution_id, scope_id)

    def _list(self, name, job_id, scope_id):
        with self._atomic(job_id, scope_id):
            self._job(job_id, scope_id)
            return [record.model_copy(deep=True) for record in getattr(self, name).values() if record.job_id == job_id]

    def list_work_items(self, job_id, *, scope_id):
        return self._list('items', job_id, scope_id)

    def list_sessions(self, job_id, *, scope_id):
        return self._list('sessions', job_id, scope_id)

    def list_executions(self, job_id, *, scope_id):
        return self._list('executions', job_id, scope_id)

    def create_session(self, session, *, scope_id, expected_revision):
        with self._atomic(session.job_id, scope_id):
            job = self._job(session.job_id, scope_id)
            self._cas(job, expected_revision)
            if session.scope_id != scope_id or session.session_id in self.sessions:
                raise HarnessConflict('Session 身份无效或已存在')
            if job.stop_request_id or job.status in {'STOPPING', 'STOPPED', 'COMPLETED', 'BLOCKED', 'FAILED'}:
                raise HarnessConflict('当前 Job 不允许新 Session')
            if session.input_commit != job.accepted_commit:
                raise HarnessConflict('Session 输入必须是最新 accepted commit')
            if session.revision or session.status != 'CREATED' or session.invocation_no:
                raise HarnessConflict('新 Session 不能预设执行或完成状态')
            if any(current.job_id == job.job_id and current.status not in {'COMPLETED', 'FAILED'}
                   for current in self.sessions.values()):
                raise HarnessConflict('Job 已有活动 Session')
            if session.kind == 'coder':
                item = self._record(self.items, session.harness_task_id, scope_id)
                if item.job_id != job.job_id or item.status != 'LEASED':
                    raise HarnessConflict('coder 必须绑定已领取任务')
                session = session.model_copy(update={'owner': item.lease_owner, 'owner_epoch': item.lease_epoch}, deep=True)
            session = session.model_copy(update={'status': 'RUNNING'}, deep=True)
            self.sessions[session.session_id] = session
            job.status = 'RUNNING'
            job.revision += 1
            self._event(job, 'harness.session.created', {'session_id': session.session_id})
            return session.model_copy(deep=True)

    def finish_initialization(self, session_id, initialization_ref, *, scope_id, expected_revision):
        current = self.get_session(session_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            session = self.sessions[session_id]
            self._cas(session, expected_revision)
            job = self._job(session.job_id, scope_id)
            manifest = self._artifact(InitializationManifest, session, initialization_ref)
            if session.kind != 'initializer' or session.status != 'RUNNING' or job.stop_request_id:
                raise HarnessConflict('初始化状态无效')
            if manifest.goal_hash != job.goal_hash or manifest.input_commit != session.input_commit or not manifest.healthy:
                raise HarnessConflict('初始化身份或健康门禁失败')
            job.initialization_ref = initialization_ref
            job.revision += 1
            session.status = 'COMPLETED'
            session.revision += 1
            self._event(job, 'harness.initialized', {'initialization_ref': initialization_ref})
            return job.model_copy(deep=True)

    def block_job(self, job_id, blocker, *, scope_id, expected_revision):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            if job.status in {'COMPLETED', 'FAILED'}:
                raise HarnessConflict('终态 Job 不能改写')
            job.status, job.blocker, job.revision = 'BLOCKED', blocker, job.revision + 1
            self._event(job, 'harness.blocked', {'blocker': blocker})
            return job.model_copy(deep=True)

    def invalidate_target(self, job_id, current_target_commit, *, scope_id, expected_revision):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            if not current_target_commit:
                raise HarnessConflict('目标分支提交不能为空')
            if job.quality != 'MERGE_READY' or job.final_target_commit == current_target_commit:
                if (job.blocker in {'FINAL_ACCEPTANCE_FAILED', 'TARGET_BASE_CHANGED'}
                        and job.required_target_commit != current_target_commit):
                    job.required_target_commit, job.revision = current_target_commit, job.revision + 1
                    self._event(job, 'harness.target.changed', {'current_target_commit': current_target_commit})
                return job.model_copy(deep=True)
            job.quality, job.status, job.blocker = 'INCREMENT_VERIFIED', 'BLOCKED', 'TARGET_BASE_CHANGED'
            job.required_target_commit = current_target_commit
            job.revision += 1
            self._event(job, 'harness.target.invalidated', {'verified_target_commit': job.final_target_commit,
                'current_target_commit': current_target_commit})
            return job.model_copy(deep=True)

    def finalize_job(self, job_id, verification_ref, *, scope_id, expected_revision, session_id):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            if job.status == 'COMPLETED' and job.final_verification_ref == verification_ref:
                return job.model_copy(deep=True)
            self._cas(job, expected_revision)
            if job.status not in {'RUNNING', 'BLOCKED'}:
                raise HarnessConflict('当前生命周期不允许最终验收')
            if job.status == 'BLOCKED' and job.blocker not in {'FINAL_ACCEPTANCE_FAILED', 'TARGET_BASE_CHANGED'}:
                raise HarnessConflict('非最终验收阻塞不能直接重试')
            session = self._record(self.sessions, session_id, scope_id)
            if session.job_id != job_id or session.status != 'COMPLETED' or job.stop_request_id:
                raise HarnessConflict('最终验收 Session 无效')
            items = [item for item in self.items.values() if item.job_id == job_id]
            if not items or any(item.status != 'VERIFIED' for item in items):
                raise HarnessConflict('仍有未通过增量')
            verification = self._artifact(VerificationRecord, session, verification_ref)
            if verification.candidate_commit != job.accepted_commit or set(verification.acceptance_refs) != set(job.acceptance_refs):
                raise HarnessConflict('最终验收内容或目标不一致')
            if verification.input_commit != job.origin_base_commit or not verification.target_commit:
                raise HarnessConflict('最终验收必须绑定初始基准及目标分支提交')
            if job.required_target_commit and verification.target_commit != job.required_target_commit:
                raise HarnessConflict('目标分支已推进，旧目标验收不再有效')
            if not all(getattr(verification, field) for field in
                       ('source_manifest', 'patch_hash', 'environment_digest', 'test_spec_hash', 'check_plan_hash')):
                raise HarnessConflict('最终验收缺少累计内容身份')
            if not verification.commands or verification_ref == session.verification_ref:
                raise HarnessConflict('最终验收没有实际命令')
            self._verification_evidence(session, verification)
            passed = verification.passed and all(command.exit_code == 0 for command in verification.commands)
            job.final_verification_ref = verification_ref
            job.final_source_manifest = verification.source_manifest
            job.final_patch_hash = verification.patch_hash
            job.final_target_commit = verification.target_commit
            job.required_target_commit = verification.target_commit
            job.status = 'COMPLETED' if passed else 'BLOCKED'
            job.blocker = None if passed else 'FINAL_ACCEPTANCE_FAILED'
            job.quality = 'MERGE_READY' if passed else 'INCREMENT_VERIFIED'
            job.revision += 1
            self._event(job, 'harness.final.acceptance', {'verification_ref': verification_ref, 'passed': passed})
            return job.model_copy(deep=True)

    def install_plan(self, job_id, plan_ref, *, scope_id, expected_revision):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            initializer = next((session for session in self.sessions.values()
                if session.job_id == job_id and session.kind == 'initializer'), None)
            if initializer is None or not job.initialization_ref or job.plan_ref:
                raise HarnessConflict('初始化未完成或计划已冻结')
            plan = self._artifact(TaskPlan, initializer, plan_ref)
            if set(plan.acceptance_refs) != set(job.acceptance_refs) or plan.plan_version != job.plan_version + 1:
                raise HarnessConflict('计划削弱冻结验收项或版本无效')
            for item in plan.work_items:
                if item.status not in {'PENDING', 'READY'} or item.lease_owner or item.revision:
                    raise HarnessConflict('计划不能自报领取或完成状态')
                if item.harness_task_id in self.items:
                    raise HarnessConflict('工作项 ID 已存在')
                self.items[item.harness_task_id] = item.model_copy(update={
                    'status': 'PENDING' if item.depends_on else 'READY'}, deep=True)
            job.plan_ref, job.plan_version = plan_ref, plan.plan_version
            job.revision += 1
            self._event(job, 'harness.plan.installed', {'plan_ref': plan_ref})
            return job.model_copy(deep=True)

    def claim_work_item(self, job_id, expected_revision, owner, *, scope_id, lease_seconds=60):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            if not owner or job.status != 'RUNNING' or job.stop_request_id or not job.plan_ref:
                raise HarnessConflict('Job 无法领取任务')
            items = [item for item in self.items.values() if item.job_id == job_id]
            if any(item.status in {'LEASED', 'CANDIDATE'} for item in items) or any(
                    session.job_id == job_id and session.status not in {'COMPLETED', 'FAILED'}
                    for session in self.sessions.values()):
                raise HarnessConflict('已有写入者；租约过期不足以证明执行者结束')
            ready = next((item for item in items if item.status in {'PENDING', 'READY'}
                and all(self.items[identity].status == 'VERIFIED' for identity in item.depends_on)), None)
            if ready is None:
                raise HarnessConflict('没有依赖已满足的任务')
            job.owner, job.owner_epoch = owner, job.owner_epoch + 1
            job.revision += 1
            ready.status, ready.lease_owner, ready.lease_epoch = 'LEASED', owner, job.owner_epoch
            ready.lease_expires_at, ready.revision = self.clock() + lease_seconds, ready.revision + 1
            self._event(job, 'harness.task.claimed', {'harness_task_id': ready.harness_task_id})
            return ready.model_copy(deep=True)

    def allocate_invocation(self, session_id, *, scope_id, expected_revision, feedback_ref=None):
        current = self.get_session(session_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            session, job = self.sessions[session_id], self.jobs[current.job_id]
            self._cas(session, expected_revision)
            if session.status != 'RUNNING' or job.stop_request_id:
                raise HarnessConflict('Session 无法登记调用')
            if session.invocation_no:
                if not feedback_ref:
                    raise HarnessConflict('已有调用；新调用必须绑定宿主验收反馈')
                feedback = self._artifact(HarnessFeedback, session, feedback_ref)
                execution = self._record(self.executions, feedback.execution_id, scope_id)
                verification = self._artifact(VerificationRecord, session, feedback.verification_ref)
                if execution.session_id != session_id or execution.invocation_no != session.invocation_no or execution.status not in {'SUCCEEDED', 'FAILED'}:
                    raise HarnessConflict('反馈不属于上一成功执行')
                if verification.passed or verification.source_manifest != feedback.source_manifest or feedback.source_manifest != session.source_manifest:
                    raise HarnessConflict('反馈不属于当前失败候选')
                if feedback.candidate_commit is not None and feedback.candidate_commit != verification.candidate_commit:
                    raise HarnessConflict('反馈候选提交不一致')
                self._verification_evidence(session, verification)
            session.invocation_no += 1
            session.feedback_ref, session.revision = feedback_ref, session.revision + 1
            return session.model_copy(deep=True)

    def prepare_execution(self, session_id, node_kind, logical_invocation_id, input_hash, *, scope_id, expected_revision=None, owner=None):
        current = self.get_session(session_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            session, job = self.sessions[session_id], self.jobs[current.job_id]
            for execution in self.executions.values():
                if (execution.session_id, execution.node_kind, execution.logical_invocation_id) == (session_id, node_kind, logical_invocation_id):
                    if execution.input_hash != input_hash:
                        raise HarnessConflict('同一逻辑调用不能更改输入')
                    return execution.model_copy(deep=True)
            if expected_revision is not None:
                self._cas(session, expected_revision)
            if not session.invocation_no or session.status != 'RUNNING' or job.stop_request_id:
                raise HarnessConflict('必须先持久化调用号且 Job 允许启动')
            if owner is not None and session.owner not in {None, owner}:
                raise PermissionError('执行 owner 不匹配')
            if any(execution.session_id == session_id and execution.invocation_no == session.invocation_no
                   for execution in self.executions.values()):
                raise HarnessConflict('调用号已经绑定其他逻辑执行')
            execution = SdkExecution(job_id=job.job_id, scope_id=scope_id, session_id=session_id,
                node_kind=node_kind, logical_invocation_id=logical_invocation_id, input_hash=input_hash,
                invocation_no=session.invocation_no, owner=session.owner or owner, owner_epoch=job.owner_epoch)
            self.executions[execution.execution_id] = execution
            self._event(job, 'sdk.execution.prepared', {'execution_id': execution.execution_id})
            return execution.model_copy(deep=True)

    def update_execution(self, execution_id, *, scope_id, expected_revision, owner, owner_epoch, status, **fields):
        current = self.get_execution(execution_id, scope_id=scope_id)
        transitions = {'PREPARED': {'STARTING', 'FAILED'}, 'STARTING': {'RUNNING', 'FAILED', 'UNKNOWN', 'INTERRUPTING'},
            'RUNNING': {'SUCCEEDED', 'FAILED', 'UNKNOWN', 'INTERRUPTING'}, 'INTERRUPTING': {'STOPPED', 'UNKNOWN'},
            'STOPPED': {'STARTING'}, 'SUCCEEDED': set(), 'FAILED': set(), 'UNKNOWN': set()}
        allowed = {'sdk_session_id', 'sdk_version', 'cli_version', 'model_id', 'workspace_ref', 'result_ref',
            'checkpoint_ref', 'transcript_ref', 'observed_usage'}
        with self._atomic(current.job_id, scope_id):
            execution, job = self.executions[execution_id], self.jobs[current.job_id]
            self._cas(execution, expected_revision)
            if execution.owner != owner or execution.owner_epoch != owner_epoch or owner_epoch != job.owner_epoch:
                raise PermissionError('执行 owner/epoch 已过期')
            if fields.keys() - allowed or status not in transitions[execution.status]:
                raise HarnessConflict('执行字段或状态迁移无效')
            if job.stop_request_id and status in {'STARTING', 'RUNNING', 'SUCCEEDED'}:
                raise HarnessConflict('停止意图已登记')
            updated = SdkExecution.model_validate({**execution.model_dump(), **fields,
                'status': status, 'revision': execution.revision + 1})
            self.executions[execution_id] = updated
            if status == 'UNKNOWN':
                job.status, job.blocker = 'BLOCKED', 'EXECUTOR_STATE_UNKNOWN'
                self.sessions[execution.session_id].status = 'BLOCKED'
                self.sessions[execution.session_id].revision += 1
                job.revision += 1
            self._event(job, 'sdk.execution.' + status.lower(), {'execution_id': execution_id})
            return updated.model_copy(deep=True)

    def bind_candidate(self, session_id, *, scope_id, expected_revision, owner, owner_epoch,
                       source_manifest, patch_hash, environment_digest, test_spec_hash, check_plan_hash):
        current = self.get_session(session_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            session, job = self.sessions[session_id], self.jobs[current.job_id]
            self._cas(session, expected_revision)
            if session.owner != owner or session.owner_epoch != owner_epoch or job.owner_epoch != owner_epoch:
                raise PermissionError('候选 owner/epoch 不匹配')
            if session.kind != 'coder' or session.status != 'RUNNING' or job.stop_request_id:
                raise HarnessConflict('当前状态无法绑定候选')
            if any(execution.session_id == session_id and execution.status not in {'SUCCEEDED', 'FAILED'}
                   for execution in self.executions.values()):
                raise HarnessConflict('执行者尚未封存，不能绑定候选')
            if not all((source_manifest, patch_hash, environment_digest, test_spec_hash, check_plan_hash)):
                raise HarnessConflict('候选缺少冻结内容身份')
            for field, value in {'environment_digest': environment_digest, 'test_spec_hash': test_spec_hash,
                                 'check_plan_hash': check_plan_hash}.items():
                if getattr(session, field) and getattr(session, field) != value:
                    raise HarnessConflict('候选不能改写冻结验收条件')
            session.source_manifest, session.patch_hash = source_manifest, patch_hash
            session.environment_digest, session.test_spec_hash, session.check_plan_hash = environment_digest, test_spec_hash, check_plan_hash
            session.revision += 1
            self.items[session.harness_task_id].status = 'CANDIDATE'
            self.items[session.harness_task_id].revision += 1
            self._event(job, 'harness.candidate.exported', {'session_id': session_id, 'source_manifest': source_manifest})
            return session.model_copy(deep=True)

    def rebuild_execution(self, execution_id, reason, checkpoint_ref, *, scope_id, expected_revision, owner, owner_epoch):
        current = self.get_execution(execution_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            execution, job = self.executions[execution_id], self.jobs[current.job_id]
            self._cas(execution, expected_revision)
            session = self.sessions[execution.session_id]
            checkpoint = self._artifact(ExecutionCheckpoint, session, checkpoint_ref)
            if (execution.owner, execution.owner_epoch) != (owner, owner_epoch) or job.owner_epoch != owner_epoch:
                raise PermissionError('重建 owner/epoch 不匹配')
            if (not reason or execution.status != 'STOPPED' or checkpoint.execution_id != execution_id
                    or checkpoint.writers_alive or not checkpoint.artifacts_consistent or checkpoint.pending_operations
                    or checkpoint.owner_epoch != owner_epoch):
                raise HarnessConflict('重建缺少安全停止证据')
            self.artifacts.read(scope_id, session.run_id, checkpoint.workspace_snapshot_ref)
            execution.attempt_history.append(ExecutionAttempt(attempt_no=execution.attempt_no,
                owner_epoch=execution.owner_epoch, reason=reason, sdk_session_id=execution.sdk_session_id,
                checkpoint_ref=execution.checkpoint_ref))
            execution.attempt_no += 1
            execution.sdk_session_id, execution.checkpoint_ref = None, checkpoint_ref
            execution.revision += 1
            self._event(job, 'sdk.execution.rebuilt', {'execution_id': execution_id, 'reason': reason})
            return execution.model_copy(deep=True)

    def request_stop(self, job_id, expected_revision, *, scope_id):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            if job.stop_request_id:
                return job.model_copy(deep=True)
            self._cas(job, expected_revision)
            if job.status not in {'CREATED', 'RUNNING'}:
                raise HarnessConflict('当前状态不能停止')
            job.stop_request_id, job.status = new_id('stop'), 'STOPPING'
            job.revision += 1
            self._event(job, 'harness.stop.requested', {'stop_request_id': job.stop_request_id})
            return job.model_copy(deep=True)

    def confirm_stopped(self, job_id, checkpoint_ref, *, scope_id, expected_revision, session_id):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            session = self._record(self.sessions, session_id, scope_id)
            checkpoint = self._artifact(ExecutionCheckpoint, session, checkpoint_ref)
            execution = self._record(self.executions, checkpoint.execution_id, scope_id)
            if (session.job_id != job_id or execution.session_id != session_id or checkpoint.owner_epoch != job.owner_epoch
                    or checkpoint.status != 'STOPPED' or checkpoint.writers_alive or not checkpoint.artifacts_consistent
                    or checkpoint.pending_operations or job.status not in {'STOPPING', 'BLOCKED'}):
                raise HarnessConflict('没有可信的完整停止证据')
            if job.blocker not in {None, 'EXECUTOR_STATE_UNKNOWN'}:
                raise HarnessConflict('外部操作 UNKNOWN 必须显式 reconcile')
            self.artifacts.read(scope_id, session.run_id, checkpoint.workspace_snapshot_ref)
            if checkpoint.transcript_ref:
                self.artifacts.read(scope_id, session.run_id, checkpoint.transcript_ref)
            job.status, job.blocker, job.revision = 'STOPPED', None, job.revision + 1
            session.status, session.revision = 'STOPPED', session.revision + 1
            execution.status, execution.checkpoint_ref = 'STOPPED', checkpoint_ref
            execution.revision += 1
            self._event(job, 'sdk.execution.stopped', {'execution_id': execution.execution_id})
            return job.model_copy(deep=True)

    def claim_resume(self, job_id, expected_revision, owner, *, scope_id, inspection_ref):
        with self._atomic(job_id, scope_id):
            job = self._job(job_id, scope_id)
            self._cas(job, expected_revision)
            if job.status != 'STOPPED' or job.blocker or not owner:
                raise HarnessConflict('仅可恢复无 blocker 的 STOPPED Job')
            session = next((session for session in self.sessions.values() if session.job_id == job_id and session.status == 'STOPPED'), None)
            if session is None:
                raise HarnessConflict('缺少停止 Session')
            inspection = self._artifact(ExecutionCheckpoint, session, inspection_ref)
            execution = self._record(self.executions, inspection.execution_id, scope_id)
            if inspection.writers_alive or not inspection.artifacts_consistent or inspection.pending_operations or inspection.owner_epoch != job.owner_epoch:
                raise HarnessConflict('无法证明执行者结束及工件一致')
            if execution.status != 'STOPPED' or execution.session_id != session.session_id:
                raise HarnessConflict('执行未安全停止')
            self.artifacts.read(scope_id, session.run_id, inspection.workspace_snapshot_ref)
            job.owner_epoch, job.owner, job.status = job.owner_epoch + 1, owner, 'RUNNING'
            job.stop_request_id, job.revision = None, job.revision + 1
            session.owner, session.owner_epoch, session.status = owner, job.owner_epoch, 'RUNNING'
            session.revision += 1
            execution.owner, execution.owner_epoch = owner, job.owner_epoch
            execution.revision += 1
            if session.harness_task_id:
                item = self.items[session.harness_task_id]
                item.lease_owner, item.lease_epoch = owner, job.owner_epoch
                item.revision += 1
            self._event(job, 'sdk.execution.recovered', {'execution_id': execution.execution_id})
            return job.model_copy(deep=True)

    def accept_increment(self, session_id, expected_revision, verification_ref, candidate_commit, handoff_ref, *, scope_id, owner, owner_epoch):
        current = self.get_session(session_id, scope_id=scope_id)
        with self._atomic(current.job_id, scope_id):
            session, job = self.sessions[session_id], self.jobs[current.job_id]
            if session.status == 'COMPLETED':
                if (session.verification_ref, session.candidate_commit, session.handoff_ref) != (verification_ref, candidate_commit, handoff_ref):
                    raise HarnessConflict('已接纳 Session 不能替换结果')
                return job.model_copy(deep=True)
            self._cas(session, expected_revision)
            if (session.owner, session.owner_epoch) != (owner, owner_epoch) or owner_epoch != job.owner_epoch:
                raise PermissionError('接纳 owner/epoch 不匹配')
            if not job.local_commit_authorized or job.stop_request_id or session.status != 'RUNNING' or session.kind != 'coder':
                raise HarnessConflict('缺少本地提交授权或状态不可接纳')
            item = self.items[session.harness_task_id]
            if item.status != 'CANDIDATE' or item.lease_owner != owner or item.lease_epoch != owner_epoch:
                raise HarnessConflict('工作项不是当前 owner 的候选')
            if any(execution.session_id == session_id and execution.status not in {'SUCCEEDED', 'FAILED'}
                   for execution in self.executions.values()):
                raise HarnessConflict('执行者尚未封存，不能接纳候选')
            verification = self._artifact(VerificationRecord, session, verification_ref)
            handoff = self._artifact(SessionHandoff, session, handoff_ref)
            if not verification.passed or not verification.commands or any(command.exit_code or not command.command for command in verification.commands):
                raise HarnessConflict('宿主验收未通过或缺少实际命令')
            for field in ('source_manifest', 'patch_hash', 'environment_digest', 'test_spec_hash', 'check_plan_hash'):
                if not getattr(session, field) or getattr(verification, field) != getattr(session, field):
                    raise HarnessConflict('验收与实际候选/冻结条件不一致')
            if set(verification.acceptance_refs) != set(item.acceptance_refs):
                raise HarnessConflict('验收项不匹配')
            if verification.candidate_commit != candidate_commit or verification.input_commit != session.input_commit or session.input_commit != job.accepted_commit:
                raise HarnessConflict('候选提交 lineage 不匹配')
            if (handoff.input_commit, handoff.candidate_commit, handoff.source_manifest, handoff.patch_hash,
                    handoff.verification_ref, handoff.environment_digest) != (session.input_commit, candidate_commit,
                    session.source_manifest, session.patch_hash, verification_ref, session.environment_digest):
                raise HarnessConflict('交接与候选不一致')
            if handoff.pending_operations or handoff.unresolved:
                raise HarnessConflict('交接仍有未决操作或未完成项')
            self._verification_evidence(session, verification)
            for ref in verification.reproduction_refs + verification.regression_refs:
                self.artifacts.read(scope_id, session.run_id, ref)
            item.status, item.verification_ref, item.revision = 'VERIFIED', verification_ref, item.revision + 1
            session.status, session.verification_ref, session.handoff_ref = 'COMPLETED', verification_ref, handoff_ref
            session.candidate_commit, session.revision = candidate_commit, session.revision + 1
            job.accepted_commit, job.quality, job.revision = candidate_commit, 'INCREMENT_VERIFIED', job.revision + 1
            self._event(job, 'harness.increment.accepted', {'session_id': session_id, 'candidate_commit': candidate_commit})
            return job.model_copy(deep=True)

    def outbox_events(self, job_id, *, scope_id, after=0):
        with self._atomic(job_id, scope_id):
            self._job(job_id, scope_id)
            return [event.model_copy(deep=True) for event in self.outbox.get(job_id, []) if event.seq > after]

    def consume_event(self, consumer_id, event, *, scope_id):
        event = HarnessEvent.model_validate(event.model_dump() if isinstance(event, HarnessEvent) else event)
        with self._atomic(event.job_id, scope_id):
            self._job(event.job_id, scope_id)
            if event.scope_id != scope_id:
                raise PermissionError('投影事件跨 scope')
            events = self.outbox.get(event.job_id, [])
            if event.seq < 1 or event.seq > len(events) or events[event.seq - 1] != event:
                raise HarnessConflict('投影事件不属于权威 outbox')
            key = (consumer_id, event.job_id)
            projection = self.projections.get(key, {'last_seq': 0, 'owner_epoch': 0, 'state': {}})
            if event.seq <= projection['last_seq']:
                return copy.deepcopy(projection)
            if event.seq != projection['last_seq'] + 1 or event.owner_epoch < projection['owner_epoch']:
                raise HarnessConflict('投影事件跳序或 epoch 倒退')
            projection = {'last_seq': event.seq, 'owner_epoch': event.owner_epoch, 'state': event.model_dump(mode='json')}
            self.projections[key] = projection
            return copy.deepcopy(projection)


class PostgresHarnessStore(MemoryHarnessStore):
    _tables = {'jobs': ('harness_jobs', HarnessJob, 'job_id'),
        'items': ('harness_work_items', HarnessWorkItem, 'harness_task_id'),
        'sessions': ('harness_sessions', HarnessSession, 'session_id'),
        'executions': ('sdk_executions', SdkExecution, 'execution_id')}

    def __init__(self, store, artifacts, clock=time.time):
        super().__init__(artifacts, clock)
        self.store, self.conn = store, store.conn
        self._lock = store._operation_lock

    def _lookup(self, collection_name, identity, scope_id):
        from psycopg import sql

        table = self._tables[collection_name][0]
        with self._lock:
            row = self.conn.execute(sql.SQL('SELECT job_id FROM {} WHERE id=%s AND scope_id=%s').format(
                sql.Identifier(table)), (identity, scope_id)).fetchone()
            if not row:
                raise PermissionError('当前 scope 中不存在记录')
            with self._atomic(row['job_id'], scope_id):
                return self._record(getattr(self, collection_name), identity, scope_id).model_copy(deep=True)

    @contextmanager
    def _atomic(self, job_id, scope_id):
        import json
        from psycopg import sql

        with self._lock, self.conn.transaction():
            self.conn.execute("SET LOCAL lock_timeout = '5s'")
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':harness:' || %s, 0))", (job_id,))
            self.jobs, self.items, self.sessions, self.executions = {}, {}, {}, {}
            self.outbox, self.projections = {}, {}
            for name, (table, model, identity_field) in self._tables.items():
                rows = self.conn.execute(sql.SQL('SELECT state FROM {} WHERE job_id=%s FOR UPDATE').format(
                    sql.Identifier(table)), (job_id,)).fetchall()
                for row in rows:
                    record = model.model_validate(row['state'])
                    if record.scope_id != scope_id:
                        raise PermissionError('记录不属于当前 scope')
                    getattr(self, name)[getattr(record, identity_field)] = record
            rows = self.conn.execute('SELECT payload FROM harness_outbox WHERE job_id=%s ORDER BY seq', (job_id,)).fetchall()
            self.outbox[job_id] = [HarnessEvent.model_validate(row['payload']) for row in rows]
            for row in self.conn.execute('SELECT consumer_id,state FROM harness_projection_offsets WHERE job_id=%s', (job_id,)):
                self.projections[(row['consumer_id'], job_id)] = row['state']
            yield
            for name, (table, _model, identity_field) in self._tables.items():
                records = list(getattr(self, name).values())
                if name == 'sessions':
                    records.sort(key=lambda record: record.status not in {'COMPLETED', 'FAILED'})
                for record in records:
                    columns = ['id', 'job_id', 'scope_id', 'revision', 'status', 'state']
                    values = [getattr(record, identity_field), record.job_id, record.scope_id,
                        record.revision, record.status, record.model_dump_json()]
                    if name == 'sessions':
                        columns += ['kind', 'harness_task_id']
                        values += [record.kind, record.harness_task_id]
                    elif name == 'items':
                        columns += ['lease_owner', 'lease_epoch']
                        values += [record.lease_owner, record.lease_epoch]
                    elif name == 'executions':
                        columns += ['session_id', 'node_kind', 'logical_invocation_id', 'owner_epoch']
                        values += [record.session_id, record.node_kind, record.logical_invocation_id, record.owner_epoch]
                    placeholders = [sql.SQL('%s::jsonb') if column == 'state' else sql.SQL('%s') for column in columns]
                    updates = [sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(column), sql.Identifier(column)) for column in columns if column != 'id']
                    cursor = self.conn.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT(id) DO UPDATE SET {} '
                        'WHERE {}.job_id=EXCLUDED.job_id AND {}.scope_id=EXCLUDED.scope_id').format(
                        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(placeholders),
                        sql.SQL(',').join(updates), sql.Identifier(table), sql.Identifier(table)), values)
                    if cursor.rowcount != 1:
                        raise PermissionError('记录 ID 已绑定其他 Job/scope')
            for event in self.outbox.get(job_id, []):
                self.conn.execute('INSERT INTO harness_outbox(job_id,scope_id,seq,event_id,owner_epoch,payload) '
                    'VALUES (%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(job_id,seq) DO NOTHING',
                    (job_id, scope_id, event.seq, event.event_id, event.owner_epoch, event.model_dump_json()))
            for (consumer_id, identity), projection in self.projections.items():
                self.conn.execute('INSERT INTO harness_projection_offsets(consumer_id,job_id,last_seq,state) '
                    'VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT(consumer_id,job_id) DO UPDATE SET '
                    'last_seq=EXCLUDED.last_seq,state=EXCLUDED.state',
                    (consumer_id, identity, projection['last_seq'], json.dumps(projection)))
