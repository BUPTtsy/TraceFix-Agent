type Data = Record<string, any>;
const terminal = new Set(['completed', 'failed', 'cancelled', 'abnormal', 'unknown', 'superseded']);
const outcomes = new Set(['FIX_VERIFIED', 'NO_BUG_FOUND', 'BUG_CONFIRMED', 'INCONCLUSIVE', 'LOOP_DETECTED',
  'REPAIR_EXHAUSTED', 'POLICY_BLOCKED', 'INFRA_FAILURE', 'CANCELLED', 'UNKNOWN']);

export function resultView(run: Data, findings: Data[]): Data {
  const report = run.issueReport;
  const recorded = run.executionResult;
  const outcome = recorded?.outcome ?? report?.outcome ?? run.outcome ??
    (run.status === 'failed' ? 'INFRA_FAILURE' : run.status === 'cancelled' ? 'CANCELLED' : terminal.has(run.status) ? 'UNKNOWN' : null);
  const ready = terminal.has(run.status);
  const normalized = outcome && outcomes.has(outcome) ? outcome : ready ? 'UNKNOWN' : null;
  const settled = ready && normalized !== 'UNKNOWN' &&
    (recorded ? recorded.settled === true : ['failed', 'cancelled'].includes(run.status) || Boolean(report?.outcome));
  const structuredError = run.error && typeof run.error === 'object' ? run.error : run.error ?
    {code: ready ? 'EXECUTION_FAILED' : 'EXECUTION_PENDING', message: String(run.error), retryable: false} : null;
  return {runId: run.id, taskId: run.taskId ?? null, outcome: normalized, settled, ready,
    ruleCoverage: recorded?.ruleCoverage ?? recorded?.rule_coverage ?? {
      snapshot: run.ruleSnapshot, summary: report?.check_summary ?? null, results: report?.check_results ?? []},
    findings: findings.length ? findings : recorded?.findings ?? [], patch: recorded?.patch ?? (report?.patch_ref ? {ref: report.patch_ref} : null),
    verification: recorded?.verification ?? (report ? {overall_status: report.overall_status ?? null,
      check_summary: report.check_summary ?? null, validation_refs: report.validation_refs ?? []} : null),
    error: structuredError ?? (!ready ? {code: 'RESULT_NOT_READY', message: '结果尚未就绪', retryable: true} :
      normalized === 'UNKNOWN' ? {code: 'RESULT_UNKNOWN', message: '缺少可确认的最终结果', retryable: false} : null),
    provenance: {projectId: run.projectId, consoleRunId: run.id, agentRunId: run.agentRunId ?? null,
      commit: run.repositorySnapshot?.commit ?? null, configDigest: run.repositorySnapshot?.config_digest ?? null,
      sourceRunId: run.source_run_id ?? null, sourceFindingIds: run.source_finding_ids ?? [],
      reportRef: run.reportRef ?? null}};
}
