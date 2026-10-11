import {taskError} from './task-lifecycle.js';

type Data = Record<string, any>;
const terminal = new Set(['completed', 'failed', 'cancelled', 'abnormal', 'unknown', 'superseded']);
const outcomes = new Set(['FIX_VERIFIED', 'NO_BUG_FOUND', 'BUG_CONFIRMED', 'INCONCLUSIVE', 'LOOP_DETECTED',
  'REPAIR_EXHAUSTED', 'POLICY_BLOCKED', 'INFRA_FAILURE', 'CANCELLED', 'UNKNOWN']);

export function executorResult(value: Data, run: Data, view?: Data, patchBytes?: Buffer): Data {
  if (!value || value.schema_version !== 1 || value.task_id !== run.taskId || value.run_id !== run.id || value.repo_id !== run.projectId ||
      value.terminal !== true || !terminal.has(String(value.run_status).toLowerCase()) || !outcomes.has(value.outcome))
    throw taskError('EXECUTOR_RESULT_INVALID', '执行器终态结果或身份无效', 503);
  const report = view?.issueReport;
  if (value.report_ref && (run.reportRef !== value.report_ref || run.agentRunId !== run.id || !report ||
      report.run_status !== value.run_status || report.outcome !== value.outcome))
    throw taskError('EXECUTOR_RESULT_INVALID', '执行器结果与权威报告不一致', 503);
  if (!report && !['INFRA_FAILURE', 'CANCELLED', 'UNKNOWN'].includes(value.outcome) && value.run_status !== 'CANCELLED')
    throw taskError('EXECUTOR_RESULT_INVALID', '执行器缺少权威终态报告', 503);
  const ref = report?.patch_diff_ref ?? null;
  if (ref && (!patchBytes || typeof value.diff !== 'string' || !patchBytes.equals(Buffer.from(value.diff, 'utf8')) || value.patch_diff_ref !== ref))
    throw taskError('EXECUTOR_RESULT_INVALID', '执行器补丁与已验证证据不一致', 503);
  if (value.outcome === 'FIX_VERIFIED' && (!ref || !patchBytes?.length || report?.patch_verification !== 'verified' ||
      report?.patch_available !== true || value.patch_available !== true || value.patch_verification !== 'verified'))
    throw taskError('EXECUTOR_RESULT_INVALID', '修复成功必须绑定已验证补丁', 503);
  const error = value.error ? {code: typeof value.error.code === 'string' ? value.error.code : 'EXECUTION_FAILED',
    message: '执行器报告执行失败', retryable: false} : null;
  return {status: String(value.run_status).toLowerCase(), outcome: value.outcome, error,
    executionResult: {outcome: value.outcome, settled: value.outcome !== 'UNKNOWN',
      ruleCoverage: {snapshot: view?.ruleSnapshot ?? null, summary: report?.check_summary ?? null, results: report?.check_results ?? []},
      patch: ref ? {ref, diff: value.diff, available: report.patch_available === true, verification: report.patch_verification ?? 'none'} : null,
      verification: report ? {overall_status: report.overall_status ?? null, check_summary: report.check_summary ?? null,
        patch_verification: report.patch_verification ?? 'none', validation_refs: report.validation_refs ?? []} : null}};
}
export function resultView(run: Data, findings: Data[]): Data {
  const report = run.issueReport;
  const recorded = run.executionResult;
  const outcome = recorded?.outcome ?? run.outcome ?? report?.outcome ??
    (run.status === 'failed' ? 'INFRA_FAILURE' : run.status === 'cancelled' ? 'CANCELLED' : terminal.has(run.status) ? 'UNKNOWN' : null);
  const ready = terminal.has(run.status);
  const normalized = outcome && outcomes.has(outcome) ? outcome : ready ? 'UNKNOWN' : null;
  const settled = ready && run.status !== 'unknown' && normalized !== 'UNKNOWN' &&
    (recorded ? recorded.settled === true : ['failed', 'cancelled'].includes(run.status) || Boolean(report?.outcome));
  const structuredError = run.error && typeof run.error === 'object' ? run.error : run.error ?
    {code: ready ? 'EXECUTION_FAILED' : 'EXECUTION_PENDING', message: String(run.error), retryable: false} : null;
  return {runId: run.id, taskId: run.taskId ?? null, outcome: normalized, settled, ready,
    ruleCoverage: recorded?.ruleCoverage ?? recorded?.rule_coverage ?? {
      snapshot: run.ruleSnapshot, summary: report?.check_summary ?? null, results: report?.check_results ?? []},
    findings, patch: recorded?.patch ?? (report?.patch_diff_ref ? {ref: report.patch_diff_ref,
      available: report.patch_available === true, verification: report.patch_verification ?? 'none'} : null),
    verification: recorded?.verification ?? (report ? {overall_status: report.overall_status ?? null,
      check_summary: report.check_summary ?? null, validation_refs: report.validation_refs ?? []} : null),
    error: structuredError ?? (!ready ? {code: 'RESULT_NOT_READY', message: '结果尚未就绪', retryable: true} :
      normalized === 'UNKNOWN' ? {code: 'RESULT_UNKNOWN', message: '缺少可确认的最终结果', retryable: false} : null),
    provenance: {projectId: run.projectId, consoleRunId: run.id, agentRunId: run.agentRunId ?? null,
      commit: run.repositorySnapshot?.commit ?? null, configDigest: run.repositorySnapshot?.config_digest ?? null,
      sourceRunId: run.source_run_id ?? null, sourceFindingIds: run.source_finding_ids ?? [],
      reportRef: run.reportRef ?? null}};
}

