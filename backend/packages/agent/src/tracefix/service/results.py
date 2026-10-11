from __future__ import annotations

from tracefix.runtime.contracts import Outcome, RunStatus, digest
from tracefix.storage.artifacts import redact, sanitize


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.ABNORMAL,
                              RunStatus.FAILED, RunStatus.SUPERSEDED})


class ServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def failure_result(*, task_id=None, run_id=None, repo_id=None, code='infrastructure_error', cancelled=False):
    return {'schema_version': 1, 'task_id': task_id, 'run_id': run_id, 'repo_id': repo_id,
            'run_status': 'CANCELLED' if cancelled else 'FAILED',
            'outcome': 'INCONCLUSIVE' if cancelled else 'INFRA_FAILURE', 'report_ref': None,
            'report': None, 'patch_diff_ref': None, 'diff': None, 'patch_available': False,
            'patch_verification': 'none', 'error': {'code': code}, 'terminal': True}


def preserve_report_diff(state, artifacts, workspace):
    report = artifacts.json(state.scope_id, state.run_id, state.report_ref)
    if not isinstance(report, dict) or (report.get('run_id'), report.get('scope_id')) != (state.run_id, state.scope_id):
        raise ServiceError('report_invalid', '报告身份不一致')
    reference = report.get('patch_diff_ref')
    if not reference:
        return state
    exported = artifacts.read(state.scope_id, state.run_id, reference)
    raw = workspace.diff(state.patch_base_commit or 'HEAD').encode('utf-8')
    if sanitize(raw.decode('utf-8')).encode('utf-8') != raw.replace(b'\r', b''):
        raise ServiceError('patch_export_redacted', '补丁含有需要脱敏的文本，拒绝原样导出')
    if sanitize(raw.decode('utf-8')).encode('utf-8') != exported:
        raise ServiceError('report_invalid', '差异产物与工作区不一致')
    if report.get('patch_verification') == 'verified' and digest(raw) != state.patch_hash:
        raise ServiceError('report_invalid', '已验证差异与补丁 hash 不一致')
    if raw == exported:
        return state
    report['display_patch_diff_ref'] = reference
    report['patch_diff_ref'] = artifacts.put(state.scope_id, state.run_id, raw, 'diff', label='原始补丁差异')
    state.report_ref = artifacts.put(state.scope_id, state.run_id, report, label='服务权威报告')
    return state


def serialize_result(state, artifacts, *, task_id, repo_id):
    if state.run_status not in TERMINAL_STATUSES:
        raise ServiceError('nonterminal_run', 'batch Run 未到达终态')
    if state.scope_id != repo_id:
        raise ServiceError('result_binding_invalid', '报告作用域与请求仓库不一致')
    report = artifacts.json(state.scope_id, state.run_id, state.report_ref) if state.report_ref else None
    if not isinstance(report, dict) or any(report.get(key) != value for key, value in {
            'run_id': state.run_id, 'scope_id': state.scope_id,
            'run_status': str(state.run_status), 'outcome': str(state.outcome)}.items()):
        raise ServiceError('report_invalid', '权威终态报告缺失或身份不一致')
    diff_ref = report.get('patch_diff_ref')
    diff = artifacts.read(state.scope_id, state.run_id, diff_ref).decode('utf-8') if diff_ref else None
    available = bool(diff and diff.strip())
    verification = report.get('patch_verification', 'none')
    if (report.get('patch_available', False) != available
            or verification not in {'none', 'unverified', 'verified'}
            or verification == 'verified' and (state.outcome != Outcome.FIX_VERIFIED
                or not available or digest(diff.encode()) != state.patch_hash)):
        raise ServiceError('report_invalid', '权威补丁报告与状态/差异不一致')
    result = redact({'schema_version': 1, 'task_id': task_id, 'run_id': state.run_id, 'repo_id': repo_id,
        'run_status': str(state.run_status), 'outcome': str(state.outcome), 'terminal': True,
        'report_ref': state.report_ref, 'report': report, 'patch_diff_ref': diff_ref,
        'diff': diff, 'patch_available': available, 'patch_verification': verification,
        'error': state.error_details or ({'code': 'runtime_error'} if state.error else None)})
    result['diff'] = diff
    return result
