from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import signal
import sys
from contextlib import AsyncExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from tracefix.cli.main import Session
from tracefix.connectors import GitHubMCPError
from tracefix.execution.policy import Policy
from tracefix.knowledge.documents import DocumentLibrary, timestamp
from tracefix.rules import RuleLibrary, RuleResolver
from tracefix.runtime.contracts import Outcome, Phase, RunState, RunStatus, digest
from tracefix.runtime.finding_source import rebind_finding_repair_revision
from tracefix.service.config import ServiceSettings, validate_snapshot
from tracefix.service.contracts import ServiceRequest
from tracefix.service.finding import finding_repair_state, validate_acceptance
from tracefix.service.resources import RunServices
from tracefix.service.results import ServiceError, TERMINAL_STATUSES, failure_result, serialize_result
from tracefix.storage.artifacts import redact


MAX_REQUEST_BYTES = 2_097_152


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


def safe_result(value, settings):
    secrets = [os.getenv(key) for key in settings.credential_env.values()]
    secrets.extend(os.getenv(key) for key in ('TRACEFIX_API_KEY', 'TRACEFIX_WORKER_API_KEY'))

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

    return scrub(redact(value))


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
                          services_factory=RunServices, acceptance_adapter=None):
    identity = {'task_id': request.task_id, 'run_id': request.run_id, 'repo_id': request.repo_id}
    session, documents, state = None, None, None
    services = services_factory(request, settings)
    resources = AsyncExitStack()
    result = failure_result(**identity)
    stage = 'snapshot_invalid'
    try:
        snapshot = validate_snapshot(request, settings)
        config = snapshot['repository_config']
        url = request.execution.url or config['profile']['url']
        Policy(config['allowed_urls']).url(url)
        args = SimpleNamespace(project=request.repo_id, projects=snapshot['projects_path'],
            profile=snapshot['profile_path'], plain=True, goal=request.goal, mode=request.mode,
            execution_mode='batch', data=str(settings.data_root), console_db=str(settings.console_db),
            database_url=settings.database_url, run=True, url=url, spec=None,
            parent_run=None, rule=request.additional_rule_ids, skills='skills')
        stage = 'configuration_missing'
        if not settings.database_url and session_factory is Session:
            raise ServiceError('configuration_missing', '服务缺少独立配置的数据库连接')
        session = session_factory(args, None, None)
        session.runtime_stack = resources
        session.runtime_services = services
        session.console_run_id, session.web_console_run_id = request.run_id, None
        session.session_remote = {}
        stage = 'infrastructure_error'
        await session.ensure_runtime()
        try:
            session.store.load(request.run_id, request.repo_id)
        except (KeyError, PermissionError):
            pass
        else:
            raise ServiceError('run_identity_conflict', '内部 Run 已存在，不能覆盖或重放')
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
        if latest.run_id != request.run_id or latest.run_status not in TERMINAL_STATUSES:
            raise ServiceError('nonterminal_run', '服务 batch 未到达绑定 Run 的终态')
        result = serialize_result(latest, session.artifacts, task_id=request.task_id, repo_id=request.repo_id)
    except BaseException as error:
        if not isinstance(error, (Exception, asyncio.CancelledError)):
            raise
        cancelled = isinstance(error, asyncio.CancelledError)
        code = ('cancelled' if cancelled else error.code
                if isinstance(error, (ServiceError, GitHubMCPError)) else stage)
        result = failure_result(**identity, code=code, cancelled=cancelled)
        if session is not None and state is not None and code != 'run_identity_conflict':
            try:
                failed = await terminal_failure(session, state, code, cancelled=cancelled)
                if failed is not None:
                    result = serialize_result(failed, session.artifacts,
                                              task_id=request.task_id, repo_id=request.repo_id)
            except Exception:
                pass
    finally:
        await services.aclose()
        try:
            await resources.aclose()
        except Exception:
            services.cleanup_errors.append({'code': 'runtime_close_failed'})
        if services.cleanup_errors:
            result.update(run_status='FAILED', outcome='INFRA_FAILURE',
                          error={'code': 'cleanup_failed'}, cleanup_errors=services.cleanup_errors)
        if documents is not None:
            try:
                documents.update_run(request.run_id, {'status': result['run_status'].lower(),
                    'outcome': result['outcome'], 'phase': 'FINALIZE', 'reportRef': result['report_ref'],
                    'error': result['error'], 'finishedAt': timestamp(), 'pid': None})
            except Exception:
                result.update(run_status='FAILED', outcome='INFRA_FAILURE', error={'code': 'run_index_failed'})
    return safe_result(result, settings)


async def run_with_signals(request, settings):
    task = asyncio.create_task(execute_request(request, settings))
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
    result = failure_result(code='invalid_request')
    with open(os.devnull, 'w', encoding='utf-8') as discarded:
        with redirect_stdout(discarded), redirect_stderr(discarded):
            try:
                parser = argparse.ArgumentParser()
                parser.add_argument('--request-stdin', action='store_true', required=True)
                parser.parse_args(argv)
                payload = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
                if len(payload) > MAX_REQUEST_BYTES:
                    raise ValueError
                request = ServiceRequest.model_validate_json(payload)
                result = failure_result(task_id=request.task_id, run_id=request.run_id,
                                        repo_id=request.repo_id, code='configuration_missing')
                settings = ServiceSettings.from_environment()
                result = asyncio.run(run_with_signals(request, settings))
            except (ValidationError, ValueError, SystemExit):
                pass
            except ServiceError as error:
                result['error'] = {'code': error.code}
            except KeyboardInterrupt:
                result = failure_result(code='cancelled', cancelled=True)
            except Exception:
                result['error'] = {'code': 'infrastructure_error'}
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result['run_status'] == 'COMPLETED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
