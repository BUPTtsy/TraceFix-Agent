from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Literal

from pydantic import Field, ValidationError

from tracefix.execution.candidate_export import SDKWorkspace, _snapshot
from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import Contract
from tracefix.runtime.harness_bootstrap import restore_harness_runtime
from tracefix.runtime.harness_contracts import ExecutionCheckpoint
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.harness import HarnessConflict, PostgresHarnessStore
from tracefix.storage.harness_projection import project_pending
from tracefix.storage.store import PostgresStore


class ResumeRequest(Contract):
    pass


class ControlIdentity(Contract):
    task_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    scope_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    job_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,80}$')
    run_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,80}$')


class ControlRequest(Contract):
    operation: Literal['view', 'stop', 'resume.check']
    fields: ControlIdentity


class HarnessNotFound(LookupError):
    pass


class HarnessControl:
    def __init__(self, store, artifacts, data_root, *, authority=None):
        self.store, self.artifacts = store, artifacts
        self.root = Path(data_root)
        self.authority = authority or PostgresHarnessStore(store, artifacts)

    def job(self, identity):
        if identity.job_id:
            job = self.authority.get_job(identity.job_id, scope_id=identity.scope_id)
        elif hasattr(self.store, 'conn'):
            query = ("SELECT id FROM harness_jobs WHERE scope_id=%s "
                "AND state->>'console_task_id'=%s")
            arguments = [identity.scope_id, identity.task_id]
            if identity.run_id:
                query += (" AND EXISTS (SELECT 1 FROM harness_sessions WHERE "
                    "harness_sessions.scope_id=harness_jobs.scope_id "
                    "AND harness_sessions.state->>'job_id'=harness_jobs.id "
                    "AND harness_sessions.state->>'kind'='initializer' "
                    "AND harness_sessions.state->>'run_id'=%s)")
                arguments.append(identity.run_id)
            rows = self.store.conn.execute(query, arguments).fetchall()
            if not rows:
                raise HarnessNotFound('Task 尚未绑定 SDK Job')
            if len(rows) != 1:
                raise HarnessConflict('Task 未绑定唯一 SDK Job')
            job = self.authority.get_job(rows[0]['id'], scope_id=identity.scope_id)
        else:
            jobs = [job for job in self.authority.jobs.values()
                if (job.scope_id, job.console_task_id) == (identity.scope_id, identity.task_id)]
            if identity.run_id:
                jobs = [job for job in jobs if any(session.kind == 'initializer'
                    and session.run_id == identity.run_id for session in
                    self.authority.list_sessions(job.job_id, scope_id=job.scope_id))]
            if not jobs:
                raise HarnessNotFound('Task 尚未绑定 SDK Job')
            if len(jobs) != 1:
                raise HarnessConflict('Task 未绑定唯一 SDK Job')
            job = jobs[0]
        if job.console_task_id != identity.task_id:
            raise PermissionError('SDK Job 不属于当前 Task')
        if identity.run_id and not any(session.kind == 'initializer' and session.run_id == identity.run_id
                for session in self.authority.list_sessions(job.job_id, scope_id=job.scope_id)):
            raise PermissionError('SDK Job 不属于当前 Console Run')
        return job

    def state(self, job):
        sessions = self.authority.list_sessions(job.job_id, scope_id=job.scope_id)
        current = [session for session in sessions if session.status not in {'COMPLETED', 'FAILED'}]
        session = current[-1] if current else sessions[-1] if sessions else None
        if session is None:
            raise HarnessConflict('SDK Job 缺少 Session')
        state = self.store.load(session.run_id, job.scope_id)
        if state.harness_job_id != job.job_id or state.harness_session_id != session.session_id:
            raise PermissionError('SDK Session 与 Run 身份不一致')
        return state

    def pending(self, state):
        if hasattr(self.store, 'conn'):
            rows = self.store.conn.execute("SELECT id,scope_id,resources FROM operations "
                "WHERE status IN ('STARTED','UNKNOWN')").fetchall()
            return [row['id'] for row in rows if row['scope_id'] == state.scope_id
                or '*' in (row['resources'] or ['*'])]
        return [identity for identity, value in self.store.operations.items()
            if value.get('status') in {'STARTED', 'UNKNOWN'} and
            (value.get('scope_id') == state.scope_id or '*' in (value.get('resources') or ['*']))]

    def block(self, job, blocker):
        current = self.authority.get_job(job.job_id, scope_id=job.scope_id)
        if current.status == 'BLOCKED' and current.blocker == blocker:
            return current
        return self.authority.block_job(current.job_id, blocker, scope_id=current.scope_id,
            expected_revision=current.revision)

    def engine(self, state):
        source = self.artifacts.json(state.scope_id, state.run_id, state.repo_snapshot_ref)
        initializer = next(session for session in self.authority.list_sessions(
            state.harness_job_id, scope_id=state.scope_id) if session.kind == 'initializer')
        job = self.authority.get_job(state.harness_job_id, scope_id=state.scope_id)
        frozen = self.artifacts.json(state.scope_id, initializer.run_id, job.config_ref)
        authorization = frozen['authorization']
        workspace = Workspace(Path(source['workspace_root']), authorization['allowed_files'],
            protected_paths=authorization['protected_paths'])
        workspace.repository_snapshot = source
        return SimpleNamespace(store=self.store, artifacts=self.artifacts, workspace=workspace,
            put=lambda current, value, **options: self.artifacts.put(current.scope_id, current.run_id,
                value, label=options.get('name', 'Harness控制回执')),
            get=lambda current, ref: self.artifacts.json(current.scope_id, current.run_id, ref))

    async def inspect(self, job, state):
        if self.pending(state):
            self.block(job, 'EXTERNAL_OPERATION_UNKNOWN')
            raise HarnessConflict('外部操作尚未核对，保留资源围栏')
        session = self.authority.get_session(state.harness_session_id, scope_id=state.scope_id)
        executions = [execution for execution in self.authority.list_executions(job.job_id,
            scope_id=state.scope_id) if execution.session_id == session.session_id]
        execution = executions[-1] if executions else None
        if execution is None or not execution.checkpoint_ref:
            self.block(job, 'RECOVERY_ARTIFACTS_MISSING')
            raise HarnessConflict('缺少可恢复执行 checkpoint')
        try:
            checkpoint = self.authority._artifact(ExecutionCheckpoint, session, execution.checkpoint_ref)
            if checkpoint.pending_operations:
                self.block(job, 'EXTERNAL_OPERATION_UNKNOWN')
                raise HarnessConflict('执行 checkpoint 中仍有待核对的外部操作')
            if (checkpoint.owner_epoch != job.owner_epoch or checkpoint.writers_alive
                    or not checkpoint.artifacts_consistent or checkpoint.status != 'STOPPED'
                    or checkpoint.execution_id != execution.execution_id):
                raise ValueError('执行 checkpoint 缺少一致的安全停止证据')
            engine = self.engine(state)
            snapshot = self.artifacts.json(state.scope_id, state.run_id, checkpoint.workspace_snapshot_ref)
            workspace = SDKWorkspace.load(Path(execution.workspace_ref), engine.workspace)
            if _snapshot(workspace.source).content_hash != snapshot.get('content_hash'):
                raise ValueError('停止后的实际源码与完整快照不一致')
            runtime = restore_harness_runtime(engine, state, store=self.authority)
        except HarnessConflict:
            raise
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
            blocker = ('RECOVERY_ARTIFACTS_MISSING' if isinstance(error, FileNotFoundError)
                else 'RECOVERY_ARTIFACTS_INCONSISTENT')
            self.block(job, blocker)
            raise HarnessConflict('恢复工件缺失或不一致，须人工核对') from error
        runner = runtime.runner(workspace)
        runner.owner_epoch = execution.owner_epoch
        inspection = await runner.inspect(execution.execution_id)
        if inspection.active or inspection.status != 'STOPPED':
            raise HarnessConflict('原执行者尚未确认结束')
        return execution.checkpoint_ref

    async def dispatch(self, request):
        job = self.job(request.fields)
        state = self.state(job)
        if request.operation == 'stop':
            job = self.authority.request_stop(job.job_id, job.revision, scope_id=job.scope_id)
            try:
                engine = self.engine(state)
                runtime = restore_harness_runtime(engine, state, store=self.authority)
                await runtime.stop(engine, state)
            finally:
                if self.pending(state):
                    self.block(job, 'EXTERNAL_OPERATION_UNKNOWN')
            job = self.authority.get_job(job.job_id, scope_id=job.scope_id)
        blockers = [job.blocker] if job.blocker else []
        can_resume = False
        if job.status == 'STOPPED' and not blockers:
            try:
                await self.inspect(job, state)
                can_resume = True
            except (OSError, ValueError, RuntimeError, PermissionError) as error:
                job = self.authority.get_job(job.job_id, scope_id=job.scope_id)
                blockers = [job.blocker] if job.blocker else [str(error)]
        if request.operation == 'resume.check' and not can_resume:
            raise HarnessConflict('当前 Job 不能恢复：' + '；'.join(blockers or [job.status]))
        project_pending(self.authority, self.root / 'harness-projection.sqlite3', job.job_id, job.scope_id)
        return {'coding_backend': job.backend, 'control_status': job.status,
            'can_stop': job.status in {'CREATED', 'RUNNING'}, 'can_resume': can_resume,
            'resume_blockers': blockers, 'harness_job_id': job.job_id,
            'harness_session_id': state.harness_session_id, 'current_run_id': state.run_id,
            'accepted_commit': job.accepted_commit, 'quality': job.quality,
            'owner_epoch': job.owner_epoch, 'revision': job.revision}


def main():
    store = None
    try:
        payload = sys.stdin.buffer.read(1048577)
        if len(payload) > 1048576:
            raise ValueError('控制请求过大')
        request = ControlRequest.model_validate_json(payload)
        dsn = os.environ.get('TRACEFIX_SERVICE_DATABASE_URL') or os.environ.get('TRACEFIX_DATABASE_URL')
        if not dsn:
            raise RuntimeError('缺少 PostgreSQL 权威存储配置')
        root = Path(os.environ.get('TRACEFIX_DATA', '.tracefix')).resolve()
        store = PostgresStore(dsn)
        response = {'ok': True, 'data': asyncio.run(HarnessControl(store, Artifacts(root / 'artifacts'), root).dispatch(request))}
    except (ValidationError, ValueError) as error:
        if isinstance(error, HarnessConflict):
            response = {'ok': False, 'error': {'code': 'HARNESS_CONFLICT', 'message': str(error), 'status': 409}}
        else:
            response = {'ok': False, 'error': {'code': 'INVALID_REQUEST', 'message': '控制请求字段无效', 'status': 400}}
    except HarnessNotFound as error:
        response = {'ok': False, 'error': {'code': 'HARNESS_NOT_FOUND', 'message': str(error), 'status': 404}}
    except PermissionError as error:
        response = {'ok': False, 'error': {'code': 'HARNESS_FORBIDDEN', 'message': str(error), 'status': 403}}
    except Exception:
        response = {'ok': False, 'error': {'code': 'HARNESS_UNAVAILABLE', 'message': '权威任务控制暂不可用', 'status': 503}}
    finally:
        if store is not None:
            store.close()
    print(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    main()
