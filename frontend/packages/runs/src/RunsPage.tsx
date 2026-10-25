import {useEffect, useMemo, useRef, useState} from 'react';
import {aggregateWorkers, artifactUrl, CheckItem, CheckResult, CheckSummary, decideApproval, deriveRun, loadRun, loadRunResult, loadRunTrace, Publication, resumeAgent, Run, RunResult, TraceEvent, WorkerSnapshot, Guidance, loadGuidance, submitGuidance, confirmGuidance} from '@tracefix/api-client';
import {eventLabel, outcomeLabel, phaseLabel, publicationLabel, serviceLabel, workerRoleLabel, workerStatusLabel} from '@tracefix/presentation';

const statuses: Record<string, string> = {idle: '空闲', running: '运行中', completed: '已结束', abnormal: '异常结束', failed: '失败', cancelled: '已停止', stopped: '已停止', blocked: '已阻塞', superseded: '目标已替换', paused: '已暂停', waiting_input: '等待输入', waiting_approval: '等待审批', stopping: '停止中'};
const issueStatuses: Record<string, string> = {suspected: '待确认', confirmed: '已确认', reproduced: '已确认', fixed: '已验证修复', not_reproducible: '未稳定复现', wont_fix: '暂不修复', false_positive: '已判定误报'};
const guidanceStatus = (entry: Guidance) => entry.level === 'retarget' && entry.status === 'queued' && !entry.confirmed_at ? '等待确认' : {queued: '排队中', applied: '已注入', acknowledged: '已采纳', rejected: '被拒绝', superseded: '已改目标'}[entry.status];
export const statusLabel = (status: string) => statuses[status.toLowerCase()] || status;
export const timeLabel = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '—';
const checkStatuses: Record<string, string> = {pass: '通过', fail: '失败', error: '执行错误', inconclusive: '无法判断'};
const overallStatuses: Record<string, string> = {PASSED: '全部通过', PASSED_WITH_FINDINGS: '通过但有非阻断问题', FAILED: '阻断检查失败', INCONCLUSIVE: '尚无法确定'};
const checkSeverities: Record<string, string> = {blocker: '阻断', critical: '严重', major: '主要', minor: '次要'};
function CheckResults({plan, results, summary, runId, allowed}: {plan: CheckItem[]; results: CheckResult[]; summary?: CheckSummary; runId: string; allowed: Set<string>}) {
  const plannedIds = new Set(plan.map(item => item.id));
  const checks = [...plan.map(item => ({item, result: results.find(result => result.id === item.id)})),
    ...results.filter(result => !plannedIds.has(result.id)).map(result => ({item: result, result}))];
  const missing = summary?.missing ?? summary?.missing_ids?.length ?? plan.filter(item => !results.some(result => result.id === item.id)).length;
  return <>
    {summary && <p>已执行 {summary.executed ?? results.length} / {summary.total} 项 · 通过 {summary.passed} · 失败 {summary.failed} · 执行错误 {summary.error} · 无法判断 {summary.inconclusive} · 阻断未通过 {summary.blocker_failed}{missing > 0 && ` · 未执行 ${missing}`}</p>}
    {checks.map(({item, result}, index) => <article className="knowledge-use" key={item.id}>
      <h4>{index + 1}. {item.name} <span className={'badge ' + (result?.status === 'pass' ? 'success' : result?.status === 'fail' || result?.status === 'error' ? 'failure' : 'warning')}>{result ? checkStatuses[result.status] || result.status : '未执行'}</span></h4>
      <dl><dt>检查 ID</dt><dd>{item.id}</dd><dt>来源 / 级别</dt><dd>{item.source === 'user_goal' ? '用户目标' : '配置规则'} · {checkSeverities[item.severity] || item.severity}</dd><dt>检测方式</dt><dd>{item.detector}</dd><dt>检测内容 / 指标</dt><dd>{item.criteria}</dd><dt>实际检测结果</dt><dd>{result?.actual || '尚未记录执行结果'}</dd></dl>
      {result?.error && <p className="notice failure">{result.error}</p>}
      {result?.fallback && <p className="notice warning">已回退模型多模态判断：{result.fallback.reason || '辅助分析器不可用'}{result.fallback.status && ` · ${checkStatuses[result.fallback.status] || result.fallback.status}`}</p>}
      <div className="source-links">{result?.evidence_refs.map(ref => allowed.has(ref) ? <a href={artifactUrl(runId, ref)} key={ref} download={!ref.endsWith('.html')}>{ref}</a> : <span key={ref}>{ref}</span>)}</div>
    </article>)}
  </>;
}
export function CheckReportPanel({run}: {run: Run}) {
  const report = run.check_plan || run.check_results ? run : run.issueReport;
  if (!report || (!report.check_plan && !report.check_results)) return null;
  const plan = report.check_plan || [], results = report.check_results || [];
  const initialResults = report.initial_check_results || [];
  const verified = initialResults.length > 0 && results.some(result => result.stage === 'verify');
  const allowed = new Set(run.artifacts?.map(artifact => artifact.ref) || []);
  return <section className="detail-block" aria-label="逐项检查报告">
    <h3>逐项检查 {report.overall_status && <span className="badge">{overallStatuses[report.overall_status] || report.overall_status}</span>}</h3>
    {verified && <details><summary>修复前检查结果</summary><CheckResults plan={plan} results={initialResults} summary={report.initial_check_summary} runId={run.id} allowed={allowed}/></details>}
    {verified && <h4>修复后检查结果</h4>}
    <CheckResults plan={plan} results={results} summary={report.check_summary} runId={run.id} allowed={allowed}/>
    {!!report.images?.length && <div><h4>图片证据</h4>{report.images.filter(image => allowed.has(image.ref) && image.mime === 'image/png').map(image => <figure key={image.ref}><a href={artifactUrl(run.id, image.ref)}><img src={artifactUrl(run.id, image.ref)} alt={image.alt || '页面证据截图'} loading="lazy" style={{maxWidth: '100%', height: 'auto'}}/></a><figcaption>{image.alt || '页面证据截图'} · {image.ref}</figcaption></figure>)}</div>}
  </section>;
}
export function RunList({runs, onSelect, selectedId}: {runs: Run[]; onSelect: (run: Run) => void; selectedId?: string}) {
  return runs.length ? <div className="run-list">{runs.map(run => <button className={'run-row ' + (selectedId === run.id ? 'selected' : '')} key={run.id} onClick={() => onSelect(run)} aria-current={selectedId === run.id ? 'true' : undefined}><span className={'run-symbol ' + run.mode} aria-hidden="true">{run.mode === 'repair' ? '↗' : '✓'}</span><div className="run-copy"><strong>{run.goal}</strong><small>{run.projectId} · {serviceLabel(run.mode)} · {run.timeSource ? '报告时间 ' : ''}{timeLabel(run.startedAt)}</small>{run.abnormalTermination && <small>曾非成功结束 · 已继续 {run.continuationCount || 0} 次</small>}</div><div className="run-state"><span className={'badge ' + (run.status === 'failed' ? 'failure' : run.status === 'running' ? 'success' : '')}>{statusLabel(run.status)}</span><small>{run.outcome ? outcomeLabel(run.outcome) : phaseLabel(run.phase)}</small></div></button>)}</div> : <div className="empty-state"><span aria-hidden="true">⌁</span><h3>还没有运行记录</h3><p>从右侧选择 Test 或 Repair 并启动，实际记录会出现在这里。</p></div>;
}
const workerStatusClass = (status: string) => {
  const normalized = status.toUpperCase();
  if (normalized === 'SUCCEEDED') return 'success';
  if (normalized === 'PARTIAL' || normalized === 'RETRYING' || normalized === 'QUEUED') return 'warning';
  if (normalized === 'FAILED' || normalized === 'CANCELLED' || normalized === 'EXPIRED') return 'failure';
  return '';
};
function WorkerCard({worker}: {worker: WorkerSnapshot}) {
  const role = workerRoleLabel(worker.roleLabel || worker.role) || 'Worker';
  const status = workerStatusLabel(worker.status);
  const summary = worker.summary || worker.error;
  return <article className={'worker-card ' + (worker.active ? 'active' : 'history')}>
    <div className="worker-card-heading">
      <div className="worker-role"><span className={'worker-dot ' + workerStatusClass(worker.status)} aria-hidden="true"/><strong>{role}</strong><small>{worker.workerId}</small></div>
      <span className={'badge ' + workerStatusClass(worker.status)}>{status}</span>
    </div>
    <div className="worker-card-meta"><span>{phaseLabel(worker.phase)}</span><span>任务 {worker.taskId}</span>{worker.threadId && <span>线程 {worker.threadId}</span>}</div>
    <p className="worker-goal">{worker.goal || '未提供任务描述'}</p>
    <div className="worker-card-footer"><span>尝试 {worker.attempt}/{worker.maxAttempts}</span>{worker.durationMs !== undefined && <span>{Math.max(0, Math.round(worker.durationMs / 1000))} 秒</span>}{worker.resultRef && <span>结果 {worker.resultRef}</span>}</div>
    {summary && <p className={'worker-summary ' + (worker.error && !worker.summary ? 'failure-text' : '')}>{summary}</p>}
  </article>;
}
function WorkerPanel({events}: {events: TraceEvent[]}) {
  // 子 Agent 面板以审计事件聚合状态；运行、排队与历史结果分别展示，便于观察并发占用和失败隔离。
  const aggregate = useMemo(() => aggregateWorkers(events), [events]);
  if (!aggregate.workers.length) return null;
  const activeWorkers = aggregate.workers.filter(worker => ['RUNNING', 'RETRYING'].includes(worker.status.toUpperCase()));
  const queuedWorkers = aggregate.workers.filter(worker => ['CREATED', 'QUEUED'].includes(worker.status.toUpperCase()));
  const historyWorkers = aggregate.workers.filter(worker => !activeWorkers.includes(worker) && !queuedWorkers.includes(worker));
  return <section className="detail-block worker-panel" aria-label="Worker 执行情况">
    <div className="worker-panel-heading"><div><h3>并发 Workers <span className="count">{aggregate.workers.length}</span></h3><p>动态角色由 Supervisor 派发；读取可并行，写入由运行时互斥。</p></div><strong>{aggregate.active} / {aggregate.maxConcurrency} 活动</strong></div>
    {activeWorkers.length > 0 && <div className="worker-group"><h4>活动 Worker</h4><div className="worker-list">{activeWorkers.map(worker => <WorkerCard key={worker.workerId} worker={worker}/>)}</div></div>}
    {queuedWorkers.length > 0 && <div className="worker-group"><h4>排队中</h4><div className="worker-list">{queuedWorkers.map(worker => <WorkerCard key={worker.workerId} worker={worker}/>)}</div></div>}
    {historyWorkers.length > 0 && <div className="worker-group"><h4>历史结果</h4><div className="worker-list">{historyWorkers.map(worker => <WorkerCard key={worker.workerId} worker={worker}/>)}</div></div>}
  </section>;
}
const approvalDecisions: Record<string, string> = {approve: '已批准交付', reject: '已拒绝交付'};
const approvalActors: Record<string, string> = {web: 'Web 控制台', cli: 'CLI'};
// 交付审批：SDK Job 最终累计验收后停在 waiting_approval，人工批准后才推送远端分支或保留本地提交；未要求审批的自动交付不在此展示。
function DeliveryApprovalPanel({run, publication, onDecided}: {run: Run; publication: Publication | null; onDecided: () => Promise<void>}) {
  const [busy, setBusy] = useState<'' | 'approve' | 'reject'>(''), [failure, setFailure] = useState('');
  const approval = run.approval;
  if (!approval || approval.decided_by === 'auto') return null;
  const taskId = run.taskId;
  async function decide(decision: 'approve' | 'reject') {
    if (busy || !taskId || !approval) return;
    setBusy(decision); setFailure('');
    try {await decideApproval(taskId, approval.id, decision);}
    catch (error) {setFailure(error instanceof Error ? error.message : String(error));}
    finally {await onDecided(); setBusy('');}
  }
  const heading = <h3 id="delivery-approval-title">交付审批</h3>;
  if (run.status === 'waiting_approval' && (run.can_approve === true || approval.status === 'pending')) return <section className="detail-block" aria-labelledby="delivery-approval-title">
    {heading}
    <p>修复已通过最终累计验收，请核对累计 diff 后决定是否交付。</p>
    <dl><dt>审批 ID</dt><dd><code>{approval.id}</code></dd><dt>已验收提交</dt><dd><code>{approval.accepted_commit || run.accepted_commit || '未记录'}</code></dd><dt>补丁哈希</dt><dd><code title={approval.patch_hash || undefined}>{approval.patch_hash ? approval.patch_hash.slice(0, 12) : '未记录'}</code></dd><dt>推送远端分支</dt><dd>{approval.publish_branch ? '是，批准后推送修复分支（不创建 PR）' : '否，批准后仅保留本地提交'}</dd><dt>请求时间</dt><dd>{timeLabel(approval.requested_at ?? undefined)}</dd></dl>
    {run.delivery_diff ? <details open><summary>累计 diff</summary><pre className="log-view patch-diff">{run.delivery_diff}</pre></details> : <p className="muted">累计 diff 暂不可用{approval.diff_ref ? `（证据 ${approval.diff_ref}）` : ''}。</p>}
    {failure && <p className="notice failure" role="alert">{failure}</p>}
    <div className="button-row"><button className="primary" type="button" disabled={Boolean(busy) || !taskId} onClick={() => decide('approve')}>{busy === 'approve' ? '提交中…' : '批准交付'}</button><button className="secondary" type="button" disabled={Boolean(busy) || !taskId} onClick={() => decide('reject')}>{busy === 'reject' ? '提交中…' : '拒绝'}</button></div>
  </section>;
  // 已提交决定但执行器尚未回写终态时 status 仍为 pending，此时只展示已提交的决定。
  const decision = approval.decision || (approval.status === 'approved' ? 'approve' : approval.status === 'rejected' ? 'reject' : '');
  if (!decision) return null;
  const settled = approval.status !== 'pending';
  return <section className="detail-block" aria-labelledby="delivery-approval-title">
    {heading}
    <dl><dt>审批 ID</dt><dd><code>{approval.id}</code></dd><dt>审批决定</dt><dd>{settled ? approvalDecisions[decision] : `已提交${decision === 'approve' ? '批准' : '拒绝'}，等待交付完成`}</dd><dt>决定方 / 时间</dt><dd>{approval.decided_by ? approvalActors[approval.decided_by] || approval.decided_by : '未记录'} · {timeLabel(approval.decided_at)}</dd><dt>交付结果</dt><dd>{publication ? publicationLabel(publication.status) : '等待交付结果'}</dd></dl>
  </section>;
}
interface Props {runs: Run[]; selectedId: string; onSelect: (id: string) => void; report: (error: unknown) => void}
export function RetrievalWarning({events}: {events: TraceEvent[]}) {
  const degradation = events.find(event => event.type === 'retrieval.degraded');
  if (!degradation) return null;
  const message = degradation.payload?.message;
  return <div className="notice warning" role="status">{typeof message === 'string' && message.trim() ? message : '修复记忆的读写未启用：当前运行未连接 PostgreSQL。配置 PostgreSQL 后重新运行以恢复该能力。'}</div>;
}
export function RunsPage({runs, selectedId, onSelect, report}: Props) {
  const [query, setQuery] = useState(''), [status, setStatus] = useState(''), [detail, setDetail] = useState<Run | null>(null);
  const [loading, setLoading] = useState(false), [resuming, setResuming] = useState(false), [deriving, setDeriving] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [deriveGoal, setDeriveGoal] = useState('');
  const [guidance, setGuidance] = useState<Guidance[]>([]), [guidanceText, setGuidanceText] = useState(''), [guidanceLevel, setGuidanceLevel] = useState<Guidance['level']>('hint'), [guidanceBusy, setGuidanceBusy] = useState(false);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  useEffect(() => {
    if (!selectedId) {setDetail(null); setTrace([]); setResult(null); return;}
    let active = true, timer: number;
    let loadedRules = false;
    setDetail(null); setResult(null); setLoading(true); setTrace([]); setDeriveGoal('');
    setGuidance([]); setGuidanceText('');
    let lastSequence = 0;
    async function poll() {
      // 轨迹按 seq 增量追加，Worker/工具生命周期由同一事件流驱动；引导账本则刷新完整状态。
      try {
        const [record, events] = await Promise.all([loadRun(selectedId), loadRunTrace(selectedId, lastSequence)]);
        if (active) {
          setDetail(record); setTrace(previous => [...previous, ...events]);
          loadRunResult(record.id).then(value => {if (active) setResult(value);}).catch(error => {if (active) report(error);});
          // 引导通过实际 Agent Run 关联查询；只有收到新账本状态才显示“已注入/已采纳”等执行结果。
          if (record.agentRunId) loadGuidance(record.id).then(items => {if (active) setGuidance(items);}).catch(error => {if (active) report(error);});
          if (events.length) lastSequence = events[events.length - 1].seq;
        }
      }
      catch (error) {if (active) report(error);}
      finally {if (active) {setLoading(false); timer = window.setTimeout(poll, 2500);}}
    }
    poll(); return () => {active = false; window.clearTimeout(timer);};
  }, [selectedId, report]);
  // 审批决定后立即刷新详情与结果，不必等待下一次轮询；期间切换了 Run 则丢弃旧结果。
  async function refreshDetail(id: string) {
    try {
      const record = await loadRun(id);
      if (selectedRef.current !== id) return;
      setDetail(record);
      const value = await loadRunResult(record.id);
      if (selectedRef.current === id) setResult(value);
    } catch (error) {report(error);}
  }
  const publication = result?.publication ?? detail?.publication ?? null;
  const filtered = runs.filter(run => (!status || run.status === status) && (run.goal + run.id + (run.agentRunId || '')).toLowerCase().includes(query.toLowerCase()));
  // 改目标条目先显示独立确认操作；trace 保留 tool.started/tool.completed 的原始 payload 供检查回执。
  return <><section className="card"><div className="section-heading"><div><h2>Agent Runs <span className="count">{runs.length}</span></h2><p>按项目持久化，页面刷新后仍可追溯</p></div></div><div className="toolbar"><input aria-label="搜索运行记录" placeholder="搜索目标或 Run ID…" value={query} onChange={event => setQuery(event.target.value)}/><select aria-label="运行状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statuses).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></div>{runs.length > 0 && !filtered.length ? <div className="empty-state"><span aria-hidden="true">⌕</span><h3>没有匹配的运行记录</h3><p>调整关键词或状态筛选，再看看其他运行。</p></div> : <RunList runs={filtered} onSelect={run => onSelect(run.id)} selectedId={selectedId}/>}</section>
    {loading && <div className="notice" role="status">正在加载运行详情…</div>}
    {detail && <section className="card detail-block continuation-detail">
      <RetrievalWarning events={trace}/>
      {detail.coding_backend === 'claude_sdk' && <section aria-label="SDK 任务状态">
        <h3>SDK 任务 · {statusLabel(detail.control_status || detail.status)}</h3>
        <dl><dt>当前增量</dt><dd>{detail.current_run_id || detail.agentRunId || '尚未分配'}</dd><dt>已验收提交</dt><dd>{detail.accepted_commit || '尚未接纳增量'}</dd><dt>验收状态</dt><dd>{detail.quality || '等待验收'}</dd></dl>
        {!!detail.resume_blockers?.length && <p className="notice warning" role="status">恢复受阻：{detail.resume_blockers.join('；')}</p>}
        <p className="muted">继续执行原始目标；新目标请创建新任务。</p>
      </section>}
      <DeliveryApprovalPanel key={detail.id} run={detail} publication={publication} onDecided={() => refreshDetail(detail.id)}/>
      <h3>对话与干预</h3>
      {guidance.map(entry => <article className="knowledge-use" key={entry.id}><span className="badge">{guidanceStatus(entry)}</span><p>{entry.text}</p>{entry.how_applied && <p>{entry.how_applied}</p>}{entry.rejection_reason && <p className="notice failure">{entry.rejection_reason}</p>}{detail.coding_backend !== 'claude_sdk' && entry.level === 'retarget' && entry.status === 'queued' && !entry.confirmed_at && <button type="button" disabled={guidanceBusy} onClick={async () => {setGuidanceBusy(true); try {await confirmGuidance(detail.id, entry.id); setGuidance(await loadGuidance(detail.id));} catch (error) {report(error);} finally {setGuidanceBusy(false);}}}>确认结束当前目标并派生新任务</button>}{entry.child_run_id && <p>子 Run：{entry.child_run_id}</p>}</article>)}
      {detail.agentRunId && detail.coding_backend !== 'claude_sdk' && ['running', 'paused', 'waiting_input'].includes(detail.status) && <form onSubmit={async event => {event.preventDefault(); setGuidanceBusy(true); try {await submitGuidance(detail.id, guidanceText, guidanceLevel); setGuidance(await loadGuidance(detail.id)); setGuidanceText('');} catch (error) {report(error);} finally {setGuidanceBusy(false);}}}><label htmlFor="guidance-level">引导类型</label><select id="guidance-level" value={guidanceLevel} onChange={event => setGuidanceLevel(event.target.value as Guidance['level'])}><option value="hint">提示 L1</option><option value="constraint">约束 L2</option><option value="retarget">改目标 L3</option></select><label htmlFor="guidance-text">补充引导</label><textarea id="guidance-text" value={guidanceText} onChange={event => setGuidanceText(event.target.value)} maxLength={2000} required placeholder={guidanceLevel === 'constraint' ? '不要改 src/legacy；补丁不超过 50 行，或输入约束 JSON' : '描述需要调整的行为…'}/><button className="primary" disabled={guidanceBusy || !guidanceText.trim()}>{guidanceBusy ? '发送中…' : '发送引导'}</button></form>}
      <CheckReportPanel run={detail}/>
      <h3>问题清单</h3>
      {detail.reportError && <div className="notice failure">{detail.reportError}</div>}
      {detail.issueReport ? <>
        <p>{detail.issueReport.summary}</p>
        {detail.issueReport.issues.map((issue, index) => <article className="knowledge-use" key={issue.id}>
          <h4>{index + 1}. {issue.title} <span className="badge">{issueStatuses[issue.status] || issue.status}</span></h4>
          <dl><dt>位置</dt><dd>{issue.location}</dd><dt>预期表现</dt><dd>{issue.expected}</dd><dt>实际表现</dt><dd>{issue.actual}</dd><dt>验证结果</dt><dd>{issue.verification}</dd></dl>
          <p>复现步骤</p><ol>{issue.steps.map((step, stepIndex) => <li key={stepIndex}>{step}</li>)}</ol>
          <div className="source-links">{issue.evidence_refs.map(ref => <a href={artifactUrl(detail.id, ref)} key={ref} download={!ref.endsWith('.html')}>{ref}</a>)}</div>
        </article>)}
        <p className="muted">{detail.issueReport.coverage}</p><p className="muted">{detail.issueReport.limits}</p>
      </> : !detail.reportError && <p className="muted">{['running', 'paused', 'waiting_input', 'waiting_approval', 'stopping'].includes(detail.status) ? '问题报告将在本次执行收尾后生成；检测过程可查看进程日志。' : '此历史运行未记录结构化问题描述，可查看原报告及进程日志。'}</p>}
      {detail.abnormalTermination && <div className="notice warning">此任务曾非成功结束，已继续执行 {detail.continuationCount || 0} 次。此前的轨迹与报告持续保留。</div>}
      {detail.can_resume && <button className="primary" type="button" disabled={resuming} onClick={async () => {setResuming(true); try {await resumeAgent(detail.taskId || detail.id); setDetail(await loadRun(detail.id));} catch (error) {report(error);} finally {setResuming(false);}}}>{resuming ? '恢复中…' : '继续 SDK Job'}</button>}
      {!!detail.continuationMarkers?.length && <details><summary>非成功结束与继续执行标记</summary>{detail.continuationMarkers.map((marker, index) => <div className="knowledge-use" key={index}><strong>第 {index + 1} 次继续 · {timeLabel(marker.at)}</strong><p>此前状态：{statusLabel(marker.previous_status.toLowerCase())} · {marker.previous_error || '未记录错误'}</p><p>{marker.instruction}</p></div>)}</details>}
      <WorkerPanel events={trace}/>
      <h3>任务轨迹</h3><div className="task-trace">{trace.map(event => <details className="knowledge-use" key={event.seq} open={event.type.startsWith('run.continu')}><summary>#{event.seq} · {phaseLabel(event.phase)} · {eventLabel(event.type)}</summary><pre className="log-view">{JSON.stringify(event.payload, null, 2)}</pre></details>)}</div>{!trace.length && <p className="muted">尚无已记录的轨迹事件</p>}
      <h3>本次规则快照</h3>
      {detail.ruleSnapshot ? <div className="knowledge-use"><p>{detail.ruleSnapshot.refs.length} 条规则 · 派生 Run 保留以下版本</p>{detail.ruleSnapshot.refs.map(ref => <div key={ref.id}>{ref.id} · v{ref.version}</div>)}{detail.parentRunId && <p>父 Run：{detail.parentRunId}</p>}</div> : <p className="muted">尚未生成规则快照，暂时不能派生 Run。</p>}
      {detail.ruleSnapshot && detail.status !== 'running' && detail.status !== 'stopping' && <form className="derive-run-form" onSubmit={async event => {
        event.preventDefault(); if (deriving) return; setDeriving(true);
        try {const created = await deriveRun(detail, deriveGoal); if (created.id) onSelect(created.id); setDeriveGoal('');}
        catch (error) {report(error);} finally {setDeriving(false);}
      }}><label htmlFor="derive-goal">派生 Run 目标</label><textarea id="derive-goal" value={deriveGoal} onChange={event => setDeriveGoal(event.target.value)} maxLength={4000} placeholder="默认沿用父 Run 目标；可填写本次派生 Run 的补充目标"/><p className="muted">派生 Run 自动继承父 Run 的规则快照；规则选择请在项目空间的“配置规则”中维护。</p><button className="primary" disabled={deriving}>{deriving ? '启动中…' : '创建派生 Run'}</button></form>}
    </section>}
    {detail && <section className="card run-detail"><div className="section-heading"><div><span className="eyebrow">RUN DETAIL</span><h2>{detail.goal}</h2><p>{detail.agentRunId || detail.id}</p></div></div>{detail.timeSource && <div className="notice">此记录导入自历史报告，未记录的服务模式与启动时间不作推断。</div>}<dl><dt>执行状态</dt><dd>{statusLabel(detail.status)} · {phaseLabel(detail.phase)}</dd><dt>验证结论</dt><dd>{detail.outcome ? outcomeLabel(detail.outcome) : '尚未生成验证结论'}</dd><dt>{detail.timeSource ? '报告文件时间' : '开始 / 结束'}</dt><dd>{detail.timeSource ? timeLabel(detail.finishedAt) : `${timeLabel(detail.startedAt)} / ${timeLabel(detail.finishedAt)}`}</dd><dt>退出码</dt><dd>{detail.exitCode ?? (detail.timeSource ? '历史未记录' : '进程尚未退出')}</dd><dt>工作分支</dt><dd>{detail.branch || '尚未生成候选分支'}</dd></dl>{detail.error && <div className="notice failure">{detail.error}</div>}
      <div className="detail-block"><h3>修复结果</h3>{result?.summary ? <p className="repair-summary">{result.summary}</p> : <p className="muted">尚未生成修复摘要。</p>}<dl><dt>修复分支</dt><dd><code>{result?.branch || result?.patch?.branch || detail.branch || '尚未生成'}</code></dd><dt>修复 commit</dt><dd><code>{result?.commit || result?.patch?.commit || '尚未生成'}</code></dd><dt>发布状态</dt><dd>{publication ? publicationLabel(publication.status) : detail.status === 'waiting_approval' ? '等待交付审批' : '暂无发布记录'}</dd></dl>{publication?.error && <p className="notice failure">{typeof publication.error === 'string' ? publication.error : publication.error.message || (publication.error.code ? `发布失败（${publication.error.code}）` : '发布失败')}</p>}{result?.patch?.diff && <details><summary>查看补丁 diff · {result.patch.verification === 'verified' ? '已验证' : result.patch.verification}</summary><pre className="log-view patch-diff">{result.patch.diff}</pre></details>}</div>
      <div className="detail-block"><h3>报告与补丁</h3><div className="artifact-links">{detail.artifacts?.length ? detail.artifacts.map(artifact => <a href={artifactUrl(detail.id, artifact.ref)} key={artifact.ref} download={!artifact.ref.endsWith('.html')}><span><span aria-hidden="true">↓</span> {artifact.label}</span><small>{Math.ceil(artifact.bytes / 1024)} KiB</small></a>) : <p className="muted">此 Run 尚未产生可下载的报告或补丁。</p>}</div></div>
      <div className="detail-block"><h3>进程日志 <span className="muted">最近 120 个输出片段</span></h3><pre className="log-view">{detail.logs?.join('') || '等待 Agent 输出…'}</pre></div>
    </section>}
  </>;
}
