from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import signal
import sqlite3
import sys
from contextlib import AsyncExitStack, redirect_stderr, redirect_stdout
from types import SimpleNamespace

from pydantic import ValidationError

from tracefix.service.session import Session
from tracefix.connectors import GitHubMCPError
from tracefix.execution.policy import Policy
from tracefix.knowledge.documents import DocumentLibrary, timestamp
from tracefix.rules import RuleLibrary, RuleResolver
from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus
from tracefix.runtime.finding_source import rebind_finding_repair_revision
from tracefix.service.config import ServiceSettings, validate_snapshot
from tracefix.service.contracts import ServiceRequest
from tracefix.service.delivery import DeliveryError, decide_delivery, delivery_fields, open_delivery
from tracefix.service.finding import (RunBoundFindings, finding_repair_state,
                                     strict_acceptance_adapter, validate_acceptance)
from tracefix.service.resources import RunServices
from tracefix.service.results import (ServiceError, TERMINAL_STATUSES, failure_result,
                                     preserve_report_diff, serialize_result)
from tracefix.storage.artifacts import redact


MAX_REQUEST_BYTES = 2_097_152


async def watch_service_stop(documents, request, process_control_id, task, *, interval=0.2, session=None):
    while True:
        try:
            record = await asyncio.to_thread(documents.run, request.run_id)
        except (OSError, sqlite3.Error):
            record = {}
        if (record.get('id') == request.run_id and record.get('taskId') == request.task_id
                and record.get('projectId') == request.repo_id
                and record.get('processControlId') == process_control_id
                and record.get('status') == 'stopping' and record.get('stopRequestedAt')):
            if session is not None and session.engine is not None and session.state().harness_job_id:
                await session.stop()
            else:
                task.cancel()
            return
        await asyncio.sleep(interval)


def claim_console(request, settings):
    documents = DocumentLibrary(settings.console_db)
    try:
        record = documents.run(request.run_id)
    except FileNotFoundError:
        record = None
    if record and (record.get('projectId') != request.repo_id
            or record.get('agentRunId') not in {None, request.run_id}
            or record.get('taskId') not in {None, request.task_id}
            or record.get('status') not in {'queued', 'pending', 'starting', 'running'}):
        raise ServiceError('run_identity_conflict', '共享 Run 索引身份或状态冲突')
    process_control_id = os.getenv('TRACEFIX_PROCESS_CONTROL_ID')
    if process_control_id and (os.getenv('TRACEFIX_CONSOLE_RUN_ID') != request.run_id
            or record is None or record.get('processControlId') != process_control_id):
        raise ServiceError('run_identity_conflict', '进程控制身份不属于当前 Run')
    directory = settings.data_root / 'service-requests' / request.run_id
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ServiceError('run_identity_conflict', '服务 Run 已被启动，不能盲目重放') from None
    documents.update_run(request.run_id, {
        'taskId': request.task_id, 'projectId': request.repo_id, 'agentRunId': request.run_id,
        'managed': True, 'origin': record.get('origin', 'service') if record else 'service',
        'mode': request.mode, 'goal': request.goal, 'executionMode': 'batch',
        'status': 'running', 'phase': 'STARTING', 'pid': os.getpid(), 'finishedAt': None,
        'dataRoot': str(settings.data_root), 'registry': request.repository_snapshot.projects_path,
        'profile': request.repository_snapshot.profile_path,
        'additionalRuleIds': request.additional_rule_ids}, create=record is None)
    return documents, directory


def secret_scrubber(settings):
    secrets = [os.getenv(key) for key in settings.credential_env.values()]
    secrets.extend(os.getenv(key) for key in ('TRACEFIX_API_KEY', 'TRACEFIX_WORKER_API_KEY', 'TRACEFIX_CLAUDE_API_KEY'))

    def scrub(item):
        if isinstance(item, dict):
            return {scrub(str(key)): scrub(content) for key, content in item.items()}
        if isinstance(item, list):
            return [scrub(content) for content in item]
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, '[REDACTED]')
        return item

    return scrub


def safe_result(value, settings):
    scrub = secret_scrubber(settings)
    result = scrub(redact(value))
    # 差异文本只替换部署密钥原值，不做通用脱敏，以保持与补丁摘要一致的原样内容。
    for key in ('diff', 'delivery_diff'):
        if value.get(key) is not None:
            result[key] = scrub(value[key])
    return result


def console_status(result):
    if result['run_status'] == 'WAITING_APPROVAL':
        return 'waiting_approval'
    return result.get('control_status', result['run_status']).lower()


def delivery_index(result, settings, *, phase='FINALIZE'):
    """服务结果写回 console_runs 的字段；待审批时同时写入审批与累计差异。"""
    fields = {'status': console_status(result),
        **{key: result[key] for key in ('coding_backend', 'control_status', 'can_resume',
            'resume_blockers', 'harness_job_id', 'harness_session_id') if key in result},
        'outcome': result['outcome'], 'phase': phase, 'reportRef': result['report_ref'],
        'error': result['error'], 'finishedAt': timestamp() if result.get('terminal') else None,
        'publication': result.get('publication'), 'approval': result.get('approval')}
    if result.get('delivery_diff') is not None:
        fields['delivery_diff'] = secret_scrubber(settings)(result['delivery_diff'])
    return fields


def require_new_internal_run(store, run_id):
    connection = getattr(store, 'conn', None)
    if connection is not None:
        exists = connection.execute('SELECT 1 FROM runs WHERE id=%s', (run_id,)).fetchone() is not None
    else:
        exists = run_id in store.runs
    if exists:
        raise ServiceError('run_identity_conflict', '内部 Run 已存在，不能覆盖或重放')


async def terminal_failure(session, state, code, *, cancelled=False):
    if session.store is None:
        return None
    try:
        latest = session.store.load(state.run_id, state.scope_id)
    except (KeyError, PermissionError):
        latest = state
    latest.run_status = RunStatus.CANCELLED if cancelled else RunStatus.FAILED
    latest.phase = Phase.FINALIZE
    latest.outcome = Outcome.INCONCLUSIVE if cancelled else Outcome.INFRA_FAILURE
    latest.error = '用户已取消' if cancelled else '服务执行失败：' + code
    latest.error_details = {'code': code, 'requires_manual_review': True, 'retryable': False}
    latest.revision += 1
    session.store.save(latest)
    if session.engine is not None:
        try:
            with session.store.writer(latest.run_id):
                await session.engine.finalize(latest, None)
            return session.store.load(latest.run_id, latest.scope_id)
        except Exception:
            pass
    report = {'run_id': latest.run_id, 'scope_id': latest.scope_id, 'run_status': str(latest.run_status),
        'outcome': str(latest.outcome), 'patch_available': False, 'patch_verification': 'none',
        'error_details': latest.error_details, 'reproduction_status': 'not_verified'}
    latest.report_ref = session.artifacts.put(latest.scope_id, latest.run_id, report, label='服务失败报告')
    session.store.save(latest)
    return latest


async def execute_request(request, settings, *, session_factory=Session,
                          services_factory=RunServices, acceptance_adapter=strict_acceptance_adapter):
    identity = {'task_id': request.task_id, 'run_id': request.run_id, 'repo_id': request.repo_id}
    session, documents, state = None, None, None
    stop_watcher = None
    services = services_factory(request, settings)
    resources = AsyncExitStack()
    result = failure_result(**identity)
    stage = 'snapshot_invalid'
    try:
        if request.approval_decision is not None:
            raise ServiceError('invalid_request', '审批决定只能经交付入口提交')
        snapshot = validate_snapshot(request, settings)
        config = snapshot['repository_config']
        url = request.execution.url or config['profile']['url']
        try:
            Policy(config['allowed_urls']).url(url)
        except (PermissionError, ValueError):
            raise ServiceError('execution_denied', '服务 URL 超出冻结配置') from None
        args = SimpleNamespace(project=request.repo_id, projects=snapshot['projects_path'],
            profile=snapshot['profile_path'], plain=True, goal=request.goal, mode=request.mode,
            execution_mode='batch', data=str(settings.data_root), console_db=str(settings.console_db),
            database_url=settings.database_url, run=True, url=url, spec=None,
            parent_run=None, rule=request.additional_rule_ids, skills='skills')
        stage = 'configuration_missing'
        if not settings.database_url and session_factory is Session:
            raise ServiceError('configuration_missing', '服务缺少独立配置的数据库连接')
        if not os.getenv('TRACEFIX_API_KEY'):
            raise ServiceError('configuration_missing', '服务缺少部署模型凭据')
        session = session_factory(args, None, None)
        session.runtime_stack = resources
        session.runtime_services = services
        session.console_run_id, session.web_console_run_id = request.run_id, None
        session.session_remote = {}
        process_control_id = os.getenv('TRACEFIX_PROCESS_CONTROL_ID')
        if process_control_id:
            stop_watcher = asyncio.create_task(watch_service_stop(DocumentLibrary(settings.console_db), request,
                process_control_id, asyncio.current_task(), session=session))
        stage = 'infrastructure_error'
        await session.ensure_runtime()
        require_new_internal_run(session.store, request.run_id)
        stage = 'run_identity_conflict'
        documents, request_root = claim_console(request, settings)
        state = RunState(run_id=request.run_id, scope_id=request.repo_id, project_id=request.repo_id,
            repo_id=config['owner'] + '/' + config['repo'], job_id=request.task_id,
            goal=request.goal, url=url, mode=request.mode, execution_mode='batch',
            additional_rule_ids=request.additional_rule_ids, remote_config=config)
        accepted = None
        if request.source_run_id:
            stage = 'finding_source_invalid'
            library = RuleLibrary(settings.console_db)
            state = finding_repair_state(request, session, RuleResolver(library), library)
            state.execution_mode, state.job_id = 'batch', request.task_id
            if request.finding_acceptance is not None:
                if acceptance_adapter is None:
                    raise ServiceError('finding_acceptance_unavailable', '尚未接入 C 的严格验收适配')
                accepted = acceptance_adapter(request, state, snapshot)
                if inspect.isawaitable(accepted):
                    accepted = await accepted
                accepted = validate_acceptance(accepted, request, state)
            if (snapshot['commit'] != state.repair_source.source_base_commit
                    and not (accepted and accepted.allow_revision_rebind)):
                raise ServiceError('revision_rebind_required', '改变来源 revision 必须显式重绑并重新冻结验收')
        services.finding_acceptance = accepted
        services.bind_session(session)
        stage = 'infrastructure_error'
        spec = accepted.spec if accepted else request.execution.spec
        if spec is not None:
            spec_path = request_root / 'spec.json'
            spec_path.write_text(spec.model_dump_json(), encoding='utf-8')
            args.spec = str(spec_path)
        session.store.save(state)
        stage = 'github_context_failed'
        await services.prepare()
        stage = 'infrastructure_error'
        state = await session.create_agent(state, frozen_snapshot=snapshot, start=False)
        session.engine.rule_library = RunBoundFindings(session.engine.rule_library, services)
        if state.repair_source:
            original = state
            source = session.artifacts.json(state.scope_id, state.run_id, state.repo_snapshot_ref)
            state = rebind_finding_repair_revision(state, repo_snapshot=source,
                repo_snapshot_ref=state.repo_snapshot_ref, artifacts=session.artifacts)
            for field in ('memory_snapshot_ref', 'agent_instructions_ref', 'agent_instructions_hash',
                          'agent_instructions_path', 'test_spec_ref', 'test_spec_hash', 'job_id'):
                setattr(state, field, getattr(original, field))
            session.store.save(state)
        session.task = asyncio.create_task(session.drive(state))
        await session.task
        latest = session.state()
        if latest.harness_job_id:
            job = session.engine.harness.job(latest)
            result = sdk_result(request, session, latest, job)
        elif latest.run_id != request.run_id or latest.run_status not in TERMINAL_STATUSES:
            raise ServiceError('nonterminal_run', '服务 batch 未到达绑定 Run 的终态')
        else:
            services.require_context_consumed()
            latest = preserve_report_diff(latest, session.artifacts, session.engine.workspace)
            session.store.save(latest)
            result = serialize_result(latest, session.artifacts, task_id=request.task_id, repo_id=request.repo_id)
    except BaseException as error:
        if not isinstance(error, (Exception, asyncio.CancelledError)):
            raise
        cancelled = isinstance(error, asyncio.CancelledError)
        code = ('cancelled' if cancelled else error.code
                if isinstance(error, (ServiceError, GitHubMCPError)) else stage)
        result = failure_result(**identity, code=code, cancelled=cancelled)
        if documents is None and code in {'configuration_missing', 'infrastructure_error'}:
            try:
                documents, _ = claim_console(request, settings)
            except Exception:
                pass
        if session is not None and state is not None and session.engine is not None and session.state().harness_job_id:
            latest = session.state()
            if cancelled:
                try:
                    await session.stop()
                    result = harness_result(request, latest, session.engine.harness.job(latest))
                except Exception:
                    result = block_sdk_result(request, session, 'STOP_UNCONFIRMED')
            else:
                job = session.engine.harness.job(latest)
                result = (harness_result(request, latest, job)
                    if job.status in {'STOPPING', 'STOPPED', 'BLOCKED'}
                    else block_sdk_result(request, session, 'SERVICE_EXECUTION_FAILED'))
        elif session is not None and state is not None and code != 'run_identity_conflict':
            try:
                failed = await terminal_failure(session, state, code, cancelled=cancelled)
                if failed is not None:
                    result = serialize_result(failed, session.artifacts,
                                              task_id=request.task_id, repo_id=request.repo_id)
            except Exception:
                pass
    finally:
        if stop_watcher is not None:
            stop_watcher.cancel()
            try:
                await stop_watcher
            except asyncio.CancelledError:
                pass
        try:
            await services.aclose()
        except Exception:
            services.cleanup_errors.append({'code': 'service_close_failed'})
        if services.cleanup_errors:
            if result.get('coding_backend') == 'claude_sdk':
                result = block_sdk_result(request, session, 'CLEANUP_FAILED')
            else:
                result = failure_result(**identity, code='cleanup_failed')
            if session is not None and state is not None and result.get('coding_backend') != 'claude_sdk':
                try:
                    failed = await terminal_failure(session, state, 'cleanup_failed')
                    failed = preserve_report_diff(failed, session.artifacts, session.engine.workspace)
                    session.store.save(failed)
                    result = serialize_result(failed, session.artifacts,
                                              task_id=request.task_id, repo_id=request.repo_id)
                except Exception:
                    pass
            result['cleanup_errors'] = services.cleanup_errors
        elif session is not None and sdk_completed(result):
            # 交付只在资源清理成功后进行，与最终累计验收结论绑定。
            result = await asyncio.to_thread(delivery_result, result, request, session, settings)
        try:
            await resources.aclose()
        except Exception:
            if delivered(result):
                result.setdefault('cleanup_errors', []).append({'code': 'runtime_close_failed'})
            elif result.get('coding_backend') == 'claude_sdk':
                result = block_sdk_result(request, session, 'RUNTIME_CLOSE_FAILED')
            else:
                result = failure_result(**identity, code='runtime_close_failed')
        if documents is not None:
            try:
                waiting = result['run_status'] == 'WAITING_APPROVAL'
                documents.update_run(request.run_id, {**delivery_index(result, settings,
                    phase='FINALIZE' if result.get('terminal') or waiting else str(session.state().phase)),
                    'pid': None})
            except Exception:
                if delivered(result):
                    result.setdefault('cleanup_errors', []).append({'code': 'run_index_failed'})
                elif result.get('coding_backend') == 'claude_sdk':
                    result = block_sdk_result(request, session, 'RUN_INDEX_FAILED')
                else:
                    result.update(run_status='FAILED', outcome='INFRA_FAILURE', error={'code': 'run_index_failed'})
    return safe_result(result, settings)


def harness_result(request, state, job):
    result = failure_result(task_id=request.task_id, run_id=request.run_id, repo_id=request.repo_id,
        code='harness_' + job.status.lower())
    result.update(run_status='PAUSED', outcome=None, terminal=False, coding_backend='claude_sdk',
        control_status=job.status, can_resume=False, resume_blockers=[job.blocker] if job.blocker else [],
        harness_job_id=job.job_id, harness_session_id=state.harness_session_id, current_run_id=state.run_id)
    return result


def sdk_result(request, session, state, job):
    if job.status != 'COMPLETED' or state.run_status not in TERMINAL_STATUSES:
        return harness_result(request, state, job)
    state = preserve_report_diff(state, session.artifacts, session.engine.workspace)
    session.store.save(state)
    result = serialize_result(state, session.artifacts, task_id=request.task_id, repo_id=request.repo_id)
    result.update(run_id=request.run_id, current_run_id=state.run_id, coding_backend='claude_sdk',
        harness_job_id=job.job_id, harness_session_id=state.harness_session_id,
        control_status=job.status, can_resume=False, resume_blockers=[])
    return result


def sdk_completed(result):
    return (result.get('coding_backend') == 'claude_sdk' and result.get('terminal') is True
            and result.get('control_status') == 'COMPLETED' and bool(result.get('harness_job_id')))


def delivered(result):
    return result.get('coding_backend') == 'claude_sdk' and (
        result.get('approval') is not None or result.get('publication') is not None)


def delivery_result(result, request, session, settings):
    """SDK Job 通过最终累计验收后登记交付：需审批时等待人工决定，否则立即交付。"""
    try:
        record = open_delivery(authority=session.engine.harness.store, artifacts=session.artifacts,
            data_root=settings.data_root, scope_id=request.repo_id, job_id=result['harness_job_id'],
            run_id=request.run_id, repository_config=request.repository_snapshot.repository_config,
            resolve_credential=settings.resolve_credential)
        if record is None:
            return result
        return {**result, **delivery_fields(record, session.artifacts)}
    except Exception as error:
        code = error.code if isinstance(error, DeliveryError) else 'DELIVERY_FAILED'
        return {**result, 'approval': None, 'publication': {'status': 'failed', 'branch': None,
            'commit': None, 'error': {'code': code}}}


def block_sdk_result(request, session, blocker):
    state = session.state()
    runtime = session.engine.harness
    job = runtime.job(state)
    try:
        job = runtime.store.block_job(job.job_id, blocker, scope_id=state.scope_id,
            expected_revision=job.revision)
    except Exception:
        result = harness_result(request, state, job)
        result.update(control_status='BLOCKED', resume_blockers=[blocker, 'BLOCK_PERSIST_FAILED'])
        return result
    return harness_result(request, state, job)


async def restore_run_services(request, settings, session, state, *, services_factory=RunServices):
    services = services_factory(request, settings)
    if state.repair_source:
        from tracefix.runtime.finding_acceptance import FrozenFindingAcceptance
        from tracefix.runtime.harness_finding import _child_service
        if not state.finding_acceptance_ref:
            raise ServiceError('finding_acceptance_invalid', 'SDK 恢复缺少冻结 Finding 验收')
        frozen = FrozenFindingAcceptance(**session.artifacts.json(state.scope_id,
            state.run_id, state.finding_acceptance_ref))
        if frozen.acceptance_hash != state.finding_acceptance_hash:
            raise ServiceError('finding_acceptance_invalid', 'SDK 恢复 Finding 验收摘要不匹配')
        source = session.artifacts.json(state.scope_id, state.run_id, state.repo_snapshot_ref)
        services = _child_service(SimpleNamespace(artifacts=session.artifacts), services, state, source, frozen)
        services.finding_acceptance.reproduction_ref = state.finding_reproduction_ref
        services.finding_acceptance.verification_ref = state.finding_verification_ref
    else:
        services.run_id = state.run_id
        services.bind_session(session)
    try:
        await services.prepare()
    except BaseException:
        try:
            await services.aclose()
        except Exception:
            pass
        raise
    return services


async def execute_resume_request(request, settings):
    from tracefix.harness_control import ControlIdentity, HarnessControl
    from tracefix.storage.harness import PostgresHarnessStore
    resources = AsyncExitStack()
    session, services, state = None, None, None
    result = failure_result(task_id=request.task_id, run_id=request.run_id, repo_id=request.repo_id,
        code='harness_resume_conflict')
    try:
        if request.approval_decision is not None:
            raise ServiceError('invalid_request', '审批决定只能经交付入口提交')
        snapshot = validate_snapshot(request, settings)
        args = SimpleNamespace(project=request.repo_id, projects=snapshot['projects_path'],
            profile=snapshot['profile_path'], plain=True, goal=request.goal, mode=request.mode,
            execution_mode='batch', data=str(settings.data_root), console_db=str(settings.console_db),
            database_url=settings.database_url,
            run=True, url=None, spec=None, parent_run=None, rule=request.additional_rule_ids)
        session = Session(args, None, None)
        session.runtime_stack = resources
        await session.ensure_runtime()
        authority = PostgresHarnessStore(session.store, session.artifacts)
        control = HarnessControl(session.store, session.artifacts, settings.data_root, authority=authority)
        job = control.job(ControlIdentity(task_id=request.task_id, scope_id=request.repo_id,
            run_id=request.run_id))
        state = control.state(job)
        initializer = next((item for item in authority.list_sessions(job.job_id, scope_id=request.repo_id)
            if item.kind == 'initializer'), None)
        if (state.goal != request.goal or state.scope_id != request.repo_id
                or state.mode != request.mode or job.console_task_id != request.task_id
                or state.job_id != request.task_id or initializer is None
                or initializer.run_id != request.run_id):
            raise ServiceError('run_identity_conflict', '恢复请求与冻结目标不一致')
        session.console_run_id = request.run_id
        services = await restore_run_services(request, settings, session, state)
        session.runtime_services = services
        await session.restore_harness(state)
        session.engine.runtime_services = services
        if services.session is not session:
            services.session.engine = session.engine
        library = session.engine.rule_library
        session.engine.rule_library = RunBoundFindings(
            library.library if isinstance(library, RunBoundFindings) else library, services)
        watcher = asyncio.create_task(watch_service_stop(session.documents, request,
            os.environ.get('TRACEFIX_PROCESS_CONTROL_ID'), session.task, session=session))
        try:
            await session.task
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        latest = session.state()
        job = session.engine.harness.job(latest)
        result = sdk_result(request, session, latest, job)
    except (Exception, asyncio.CancelledError) as error:
        if session is not None and session.engine is not None and state is not None:
            if isinstance(error, asyncio.CancelledError):
                try:
                    await session.stop()
                    result = harness_result(request, session.state(), session.engine.harness.job(session.state()))
                except Exception:
                    result = block_sdk_result(request, session, 'STOP_UNCONFIRMED')
            else:
                result = block_sdk_result(request, session, 'SERVICE_RESUME_FAILED')
        else:
            result = failure_result(task_id=request.task_id, run_id=request.run_id,
                repo_id=request.repo_id, code=getattr(error, 'code', 'harness_resume_conflict'))
    finally:
        closers = [services.aclose] if services is not None else []
        if session is not None and session.engine is not None:
            closers.extend([session.engine.browser.close, session.engine.runner.close])
        cleanup_failed = False
        for close in closers:
            try:
                await close()
            except Exception:
                cleanup_failed = True
        if not cleanup_failed and not (services is not None and services.cleanup_errors) and (
                session is not None and session.engine is not None and sdk_completed(result)):
            # 权威存储仍打开时登记交付，随后才关闭运行时资源。
            result = await asyncio.to_thread(delivery_result, result, request, session, settings)
            if delivered(result):
                try:
                    DocumentLibrary(settings.console_db).update_run(request.run_id,
                        delivery_index(result, settings))
                except Exception:
                    result.setdefault('cleanup_errors', []).append({'code': 'run_index_failed'})
        try:
            await resources.aclose()
        except Exception:
            if delivered(result) and not cleanup_failed:
                result.setdefault('cleanup_errors', []).append({'code': 'runtime_close_failed'})
            else:
                cleanup_failed = True
        if cleanup_failed or services is not None and services.cleanup_errors:
            if session is not None and session.engine is not None and state is not None:
                result = block_sdk_result(request, session, 'RUNTIME_CLOSE_FAILED')
            else:
                result = failure_result(task_id=request.task_id, run_id=request.run_id,
                    repo_id=request.repo_id, code='runtime_close_failed')
    return safe_result(result, settings)


async def execute_delivery_request(request, settings, *, store=None, authority=None):
    """--deliver-stdin：按冻结身份找到 SDK Job，对待审批交付执行人工决定并输出交付结果。"""
    from tracefix.harness_control import ControlIdentity, HarnessControl
    from tracefix.storage.artifacts import Artifacts
    identity = {'task_id': request.task_id, 'run_id': request.run_id, 'repo_id': request.repo_id}
    result = failure_result(**identity, code='delivery_unavailable')
    owned = None
    try:
        decision = request.approval_decision
        if decision is None or request.mode != 'repair':
            raise ServiceError('invalid_request', '交付入口必须携带审批决定')
        snapshot = validate_snapshot(request, settings)
        if store is None:
            if not settings.database_url:
                raise ServiceError('configuration_missing', '服务缺少独立配置的数据库连接')
            from tracefix.storage.store import PostgresStore
            store = owned = PostgresStore(settings.database_url)
        artifacts = Artifacts(settings.data_root / 'artifacts')
        control = HarnessControl(store, artifacts, settings.data_root, authority=authority)
        job = control.job(ControlIdentity(task_id=request.task_id, scope_id=request.repo_id,
            run_id=request.run_id))
        state = control.state(job)
        if (state.goal != request.goal or state.scope_id != request.repo_id or state.mode != request.mode
                or state.job_id != request.task_id or job.console_task_id != request.task_id):
            raise ServiceError('run_identity_conflict', '交付请求与冻结目标不一致')
        documents = DocumentLibrary(settings.console_db)
        try:
            record = documents.run(request.run_id)
        except FileNotFoundError:
            raise ServiceError('run_identity_conflict', '共享 Run 索引不存在') from None
        if (record.get('projectId') != request.repo_id or record.get('agentRunId') not in {None, request.run_id}
                or record.get('taskId') not in {None, request.task_id}):
            raise ServiceError('run_identity_conflict', '共享 Run 索引身份冲突')
        delivery = await asyncio.to_thread(decide_delivery, authority=control.authority,
            artifacts=artifacts, data_root=settings.data_root, scope_id=request.repo_id,
            job_id=job.job_id, run_id=request.run_id, repository_config=snapshot['repository_config'],
            approval_id=decision.approval_id, decision=decision.decision, decided_by=decision.decided_by,
            resolve_credential=settings.resolve_credential)
        try:
            base = serialize_result(state, artifacts, task_id=request.task_id, repo_id=request.repo_id)
        except ServiceError:
            base = {**failure_result(**identity), 'outcome': 'FIX_VERIFIED', 'error': None}
        result = {**base, 'run_id': request.run_id, 'current_run_id': state.run_id,
            'coding_backend': 'claude_sdk', 'harness_job_id': job.job_id,
            'harness_session_id': state.harness_session_id, 'control_status': job.status,
            'can_resume': False, 'resume_blockers': [], **delivery_fields(delivery, artifacts)}
        fields = delivery_index(result, settings)
        if result.get('report_ref') is None:
            fields.pop('reportRef')
        try:
            documents.update_run(request.run_id, fields)
        except Exception:
            result.setdefault('cleanup_errors', []).append({'code': 'run_index_failed'})
    except (DeliveryError, ServiceError) as error:
        result['error'] = {'code': error.code}
    except (LookupError, PermissionError, ValueError):
        result['error'] = {'code': 'run_identity_conflict'}
    except Exception:
        result['error'] = {'code': 'infrastructure_error'}
    finally:
        if owned is not None:
            owned.close()
    return safe_result(result, settings)


async def run_with_signals(request, settings, *, restoring=False):
    task = asyncio.create_task(execute_resume_request(request, settings) if restoring
        else execute_request(request, settings))
    loop = asyncio.get_running_loop()
    previous = {}
    for name in ('SIGTERM', 'SIGINT'):
        selected = getattr(signal, name, None)
        if selected is not None:
            previous[selected] = signal.getsignal(selected)
            signal.signal(selected, lambda *args: loop.call_soon_threadsafe(task.cancel))
    try:
        return await task
    finally:
        for selected, handler in previous.items():
            signal.signal(selected, handler)


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    result = failure_result(code='invalid_request')
    with open(os.devnull, 'w', encoding='utf-8') as discarded:
        with redirect_stdout(discarded), redirect_stderr(discarded):
            try:
                parser = argparse.ArgumentParser()
                inputs = parser.add_mutually_exclusive_group(required=True)
                inputs.add_argument('--request-stdin', action='store_true')
                inputs.add_argument('--resume-stdin', action='store_true')
                inputs.add_argument('--deliver-stdin', action='store_true')
                options = parser.parse_args(argv)
                payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
                if len(payload) > MAX_REQUEST_BYTES:
                    raise ValueError
                request = ServiceRequest.model_validate_json(payload)
                # 审批决定只允许且必须出现在交付入口。
                if (request.approval_decision is not None) != options.deliver_stdin:
                    raise ValueError
                result = failure_result(task_id=request.task_id, run_id=request.run_id,
                                        repo_id=request.repo_id, code='configuration_missing')
                settings = ServiceSettings.from_environment()
                if options.deliver_stdin:
                    operation = execute_delivery_request(request, settings)
                elif options.resume_stdin:
                    operation = run_with_signals(request, settings, restoring=True)
                else:
                    operation = run_with_signals(request, settings)
                result = asyncio.run(operation)
            except (ValidationError, ValueError, SystemExit):
                pass
            except ServiceError as error:
                result['error'] = {'code': error.code}
            except KeyboardInterrupt:
                result = failure_result(code='cancelled', cancelled=True)
            except Exception:
                result['error'] = {'code': 'infrastructure_error'}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result['run_status'] in {'COMPLETED', 'WAITING_APPROVAL'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
