import {codingBackend, controlProjection} from './database.js';
import {taskError} from './task-lifecycle.js';
import {isDeepStrictEqual} from 'node:util';

type Data = Record<string, any>;
const terminal = new Set(['completed', 'failed', 'cancelled', 'abnormal', 'unknown', 'superseded']);
const outcomes = new Set(['FIX_VERIFIED', 'NO_BUG_FOUND', 'BUG_CONFIRMED', 'INCONCLUSIVE', 'LOOP_DETECTED',
  'REPAIR_EXHAUSTED', 'POLICY_BLOCKED', 'INFRA_FAILURE', 'CANCELLED', 'UNKNOWN']);

function repairMetadata(report?: Data): Data {
  return {summary: report?.result_summary ?? null,
    branch: report?.publication?.branch ?? report?.branch ?? null,
    commit: report?.publication?.commit ?? report?.repair_commit ?? null,
    publication: report?.publication ?? null};
}

// delivery_diff 与 patch 证据文件共用同一上限（见 artifacts.ts），避免待审批差异超过可读取证据的边界。
const MAX_DELIVERY_DIFF_BYTES = 16 * 1024 * 1024;
const publicationStatuses = new Set(['local', 'published', 'failed', 'rejected', 'skipped']);
const isObject = (value: any): value is Data => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
const nonEmptyText = (value: any) => typeof value === 'string' && value.length > 0 && value.length <= 400;
const nullableText = (value: any) => value === null || value === undefined || typeof value === 'string' && value.length <= 4096;
const invalidResult = (message: string) => taskError('EXECUTOR_RESULT_INVALID', message, 503);

// 待审批记录：{id, status:'pending', job_id, accepted_commit, patch_hash, publish_branch, diff_ref, requested_at}
// 已决定记录在此基础上带 decision/decided_by/decided_at，status 为 approved|rejected。
function validateApproval(value: any, expected: 'pending' | 'decided'): Data {
  if (!isObject(value) || !nonEmptyText(value.id) || !['pending', 'approved', 'rejected'].includes(value.status) ||
      (expected === 'pending') !== (value.status === 'pending'))
    throw invalidResult('执行器交付审批记录无效');
  const required = expected === 'pending';
  const present = (key: string, valid: (item: any) => boolean) => required ? valid(value[key]) : value[key] === undefined || valid(value[key]);
  if (!present('job_id', nonEmptyText) || !present('accepted_commit', nullableText) || !present('patch_hash', nullableText) ||
      !present('publish_branch', item => typeof item === 'boolean') || !present('diff_ref', nullableText) ||
      !present('requested_at', nullableText))
    throw invalidResult('执行器交付审批记录字段无效');
  if (expected === 'decided') {
    const decision = value.status === 'approved' ? 'approve' : 'reject';
    if (value.decision !== undefined && value.decision !== decision || !nonEmptyText(value.decided_by) || !nonEmptyText(value.decided_at))
      throw invalidResult('执行器交付审批决定无效');
    return {...value, decision};
  }
  return value;
}

function validatePublication(value: any): Data | null {
  if (value === null || value === undefined) return null;
  if (!isObject(value) || !publicationStatuses.has(value.status) || !nullableText(value.branch) || !nullableText(value.commit) ||
      value.error !== undefined && value.error !== null && !isObject(value.error) && typeof value.error !== 'string')
    throw invalidResult('执行器发布结果无效');
  return value;
}

// 待审批（非终态）结果：SDK Job 最终累计验收后停在交付前，进程已退出，等待 Web/CLI 决定。
function pendingApprovalResult(value: Data, run: Data): Data {
  if (codingBackend(run) !== 'claude_sdk' || value.coding_backend !== 'claude_sdk' || value.outcome !== 'FIX_VERIFIED' ||
      value.control_status !== 'COMPLETED' || run.control_status !== 'COMPLETED' || value.can_resume === true ||
      value.publication !== null && value.publication !== undefined ||
      !run.harness_job_id || !run.harness_session_id || value.harness_job_id !== run.harness_job_id ||
      value.harness_session_id !== run.harness_session_id ||
      value.current_run_id !== undefined && run.agentRunId && value.current_run_id !== run.agentRunId)
    throw invalidResult('SDK 待审批结果与权威 Job、Session 或当前增量不一致');
  const approval = validateApproval(value.approval, 'pending');
  if (approval.job_id !== run.harness_job_id || isObject(run.approval) && run.approval.id !== approval.id)
    throw invalidResult('待审批记录与权威 Job 或已记录的审批不一致');
  if (typeof value.delivery_diff !== 'string' || !value.delivery_diff.trim() ||
      Buffer.byteLength(value.delivery_diff) > MAX_DELIVERY_DIFF_BYTES)
    throw invalidResult('待审批结果缺少有效的累计差异');
  return {status: 'waiting_approval', outcome: 'FIX_VERIFIED', error: null, approval, publication: null,
    delivery_diff: value.delivery_diff};
}

export function executorResult(value: Data, run: Data, view?: Data, patchBytes?: Buffer): Data {
  if (!value || value.schema_version !== 1 || value.task_id !== run.taskId || value.run_id !== run.id || value.repo_id !== run.projectId ||
      !outcomes.has(value.outcome))
    throw invalidResult('执行器终态结果或身份无效');
  if (value.terminal === false && String(value.run_status).toUpperCase() === 'WAITING_APPROVAL') return pendingApprovalResult(value, run);
  if (value.terminal !== true || !terminal.has(String(value.run_status).toLowerCase()))
    throw invalidResult('执行器终态结果或身份无效');
  const sdk = codingBackend(run) === 'claude_sdk';
  const reportRunId = sdk ? value.current_run_id : run.id;
  if (sdk && (value.coding_backend !== 'claude_sdk' || !reportRunId || reportRunId !== run.agentRunId ||
      value.harness_job_id !== run.harness_job_id || value.harness_session_id !== run.harness_session_id ||
      !run.harness_job_id || !run.harness_session_id || run.control_status !== 'COMPLETED' || value.control_status !== 'COMPLETED'))
    throw taskError('EXECUTOR_RESULT_INVALID', 'SDK 结果与权威 Job、Session 或当前增量不一致', 503);
  const approval = value.approval === undefined || value.approval === null ? null : validateApproval(value.approval, 'decided');
  const publication = validatePublication(value.publication);
  if (approval && (!sdk || isObject(run.approval) && (run.approval.id !== approval.id ||
      run.approval.decision !== undefined && run.approval.decision !== approval.decision)))
    throw invalidResult('交付审批结果与已记录的审批决定不一致');
  if (approval && approval.job_id !== undefined && approval.job_id !== run.harness_job_id)
    throw invalidResult('交付审批记录与权威 Job 不一致');
  // 交付后的结果以 publication 为准：顶层 branch/commit 取自 publication，报告可能早于交付生成。
  const delivered = sdk && publication !== null;
  if (delivered && ((value.branch ?? null) !== (publication.branch ?? null) || (value.commit ?? null) !== (publication.commit ?? null)))
    throw invalidResult('执行器分支或提交与发布结果不一致');
  const report = view?.issueReport;
  if (value.report_ref && (run.reportRef !== value.report_ref || run.agentRunId !== reportRunId || !report ||
      sdk && (report.run_id !== reportRunId || report.scope_id !== run.projectId) ||
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
  const reportMetadata = repairMetadata(report);
  const metadata = delivered ? {...reportMetadata, branch: publication.branch ?? null, commit: publication.commit ?? null, publication} :
    reportMetadata;
  if (delivered ? !isDeepStrictEqual(value.summary ?? null, reportMetadata.summary) ||
        [report?.publication, value.report?.publication].some(item => item !== undefined && item !== null && !isDeepStrictEqual(item, publication)) :
      Object.entries(metadata).some(([key, expected]) => !isDeepStrictEqual(value[key] ?? null, expected)) ||
        (value.report && !isDeepStrictEqual(repairMetadata(value.report), metadata)))
    throw taskError('EXECUTOR_RESULT_INVALID', '执行器修复摘要、分支或提交与权威报告不一致', 503);
  const error = value.error ? {code: typeof value.error.code === 'string' ? value.error.code : 'EXECUTION_FAILED',
    message: '执行器报告执行失败', retryable: false} : null;
  return {status: String(value.run_status).toLowerCase(), outcome: value.outcome, error,
    ...(approval ? {approval} : {}), ...(Object.hasOwn(value, 'publication') ? {publication} : {}),
    ...(typeof value.delivery_diff === 'string' && Buffer.byteLength(value.delivery_diff) <= MAX_DELIVERY_DIFF_BYTES ?
      {delivery_diff: value.delivery_diff} : {}),
    executionResult: {outcome: value.outcome, settled: value.outcome !== 'UNKNOWN',
      ...metadata,
      ruleCoverage: {snapshot: view?.ruleSnapshot ?? null, summary: report?.check_summary ?? null, results: report?.check_results ?? []},
      patch: ref ? {ref, diff: value.diff, available: report.patch_available === true, verification: report.patch_verification ?? 'none',
        branch: metadata.branch, commit: metadata.commit} : null,
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
  const metadata = repairMetadata(report);
  for (const key of ['summary', 'branch', 'commit', 'publication'])
    if (recorded && Object.hasOwn(recorded, key)) metadata[key] = recorded[key];
  if (metadata.publication === null && run.publication) metadata.publication = run.publication;
  return {runId: run.id, taskId: run.taskId ?? null, ...controlProjection(run), outcome: normalized, settled, ready,
    ...metadata,
    ruleCoverage: recorded?.ruleCoverage ?? recorded?.rule_coverage ?? {
      snapshot: run.ruleSnapshot, summary: report?.check_summary ?? null, results: report?.check_results ?? []},
    findings, patch: recorded?.patch ?? (report?.patch_diff_ref ? {ref: report.patch_diff_ref,
      available: report.patch_available === true, verification: report.patch_verification ?? 'none',
      branch: metadata.branch, commit: metadata.commit} : null),
    verification: recorded?.verification ?? (report ? {overall_status: report.overall_status ?? null,
      check_summary: report.check_summary ?? null, validation_refs: report.validation_refs ?? []} : null),
    error: structuredError ?? (!ready ? {code: 'RESULT_NOT_READY', message: '结果尚未就绪', retryable: true} :
      normalized === 'UNKNOWN' ? {code: 'RESULT_UNKNOWN', message: '缺少可确认的最终结果', retryable: false} : null),
    provenance: {projectId: run.projectId, consoleRunId: run.id, agentRunId: run.agentRunId ?? null,
      commit: run.repositorySnapshot?.commit ?? null, configDigest: run.repositorySnapshot?.config_digest ?? null,
      sourceRunId: run.source_run_id ?? null, sourceFindingIds: run.source_finding_ids ?? [],
      reportRef: run.reportRef ?? null}};
}
