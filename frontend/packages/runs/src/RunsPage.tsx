import {useEffect, useState} from 'react';
import {artifactUrl, continueRun, deriveRun, DetectionRule, loadRun, loadRules, loadRunTrace, Run, TraceEvent} from '@tracefix/api-client';
import {eventLabel, outcomeLabel, phaseLabel, serviceLabel} from '@tracefix/presentation';

const statuses: Record<string, string> = {idle: '空闲', running: '运行中', completed: '已结束', abnormal: '异常结束', failed: '失败', cancelled: '已停止', paused: '已暂停', waiting_input: '等待输入', waiting_approval: '等待审批', stopping: '停止中'};
export const statusLabel = (status: string) => statuses[status.toLowerCase()] || status;
export const timeLabel = (value?: string) => value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '—';
export function RunList({runs, onSelect, selectedId}: {runs: Run[]; onSelect: (run: Run) => void; selectedId?: string}) {
  return runs.length ? <div className="run-list">{runs.map(run => <button className={'run-row ' + (selectedId === run.id ? 'selected' : '')} key={run.id} onClick={() => onSelect(run)}><span className={'run-symbol ' + run.mode}>{run.mode === 'repair' ? '↗' : '✓'}</span><div className="run-copy"><strong>{run.goal}</strong><small>{run.projectId} · {serviceLabel(run.mode)} · {run.timeSource ? '报告时间 ' : ''}{timeLabel(run.startedAt)}</small>{run.abnormalTermination && <small>曾非成功结束 · 已继续 {run.continuationCount || 0} 次</small>}</div><div className="run-state"><span className={'badge ' + (run.status === 'failed' ? 'failure' : run.status === 'running' ? 'success' : '')}>{statusLabel(run.status)}</span><small>{run.outcome ? outcomeLabel(run.outcome) : phaseLabel(run.phase)}</small></div></button>)}</div> : <div className="empty-state"><span>⌁</span><h3>还没有运行记录</h3><p>从右侧选择 Test 或 Repair 并启动，实际记录会出现在这里。</p></div>;
}
interface Props {runs: Run[]; selectedId: string; onSelect: (id: string) => void; onDocument: (id: string) => void; onCapture: (run: Run) => void; report: (error: unknown) => void}
export function RunsPage({runs, selectedId, onSelect, onDocument, onCapture, report}: Props) {
  const [query, setQuery] = useState(''), [status, setStatus] = useState(''), [detail, setDetail] = useState<Run | null>(null);
  const [loading, setLoading] = useState(false), [continuing, setContinuing] = useState(false), [deriving, setDeriving] = useState(false);
  const [instruction, setInstruction] = useState(''), [trace, setTrace] = useState<TraceEvent[]>([]);
  const [ruleOptions, setRuleOptions] = useState<DetectionRule[]>([]), [additionalRuleIds, setAdditionalRuleIds] = useState<string[]>([]), [deriveGoal, setDeriveGoal] = useState('');
  useEffect(() => {
    if (!selectedId) {setDetail(null); setTrace([]); return;}
    let active = true, timer: number;
    let loadedRules = false;
    setDetail(null); setLoading(true); setInstruction(''); setTrace([]); setRuleOptions([]); setAdditionalRuleIds([]); setDeriveGoal('');
    let lastSequence = 0;
    async function poll() {
      try {
        const [record, events] = await Promise.all([loadRun(selectedId), loadRunTrace(selectedId, lastSequence)]);
        if (active) {
          setDetail(record); setTrace(previous => [...previous, ...events]);
          if (!loadedRules) {
            loadedRules = true;
            loadRules(record.projectId).then(items => {
              if (active) setRuleOptions(items.filter(rule => rule.status === 'enabled' || rule.status === 'draft'));
            }).catch(error => {loadedRules = false; if (active) report(error);});
          }
          if (events.length) lastSequence = events[events.length - 1].seq;
        }
      }
      catch (error) {if (active) report(error);}
      finally {if (active) {setLoading(false); timer = window.setTimeout(poll, 2500);}}
    }
    poll(); return () => {active = false; window.clearTimeout(timer);};
  }, [selectedId, report]);
  const filtered = runs.filter(run => (!status || run.status === status) && (run.goal + run.id + (run.agentRunId || '')).toLowerCase().includes(query.toLowerCase()));
  return <><section className="card"><div className="section-heading"><div><h2>Agent Runs <span className="count">{runs.length}</span></h2><p>按项目持久化，页面刷新后仍可追溯</p></div></div><div className="toolbar"><input aria-label="搜索运行记录" placeholder="搜索目标或 Run ID…" value={query} onChange={event => setQuery(event.target.value)}/><select aria-label="运行状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statuses).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></div>{runs.length > 0 && !filtered.length ? <div className="empty-state">没有匹配的运行记录</div> : <RunList runs={filtered} onSelect={run => onSelect(run.id)} selectedId={selectedId}/>}</section>
    {loading && <div className="notice">正在加载运行详情…</div>}
    {detail && <section className="card detail-block continuation-detail">
      {detail.abnormalTermination && <div className="notice warning">此任务曾非成功结束，已继续执行 {detail.continuationCount || 0} 次。此前的轨迹与报告持续保留。</div>}
      {detail.canContinue && <form onSubmit={async event => {
        event.preventDefault(); setContinuing(true);
        try {await continueRun(detail.id, instruction); setDetail(await loadRun(detail.id)); setInstruction('');}
        catch (error) {report(error);} finally {setContinuing(false);}
      }}><label htmlFor="continue-instruction">继续此任务</label><textarea id="continue-instruction" value={instruction} onChange={event => setInstruction(event.target.value)} maxLength={4000} required placeholder="输入补充指令，将在当前任务中继续执行…"/><button className="primary" disabled={continuing || !instruction.trim()}>{continuing ? '启动中…' : '继续执行'}</button></form>}
      {!!detail.continuationMarkers?.length && <details><summary>非成功结束与继续执行标记</summary>{detail.continuationMarkers.map((marker, index) => <div className="knowledge-use" key={index}><strong>第 {index + 1} 次继续 · {timeLabel(marker.at)}</strong><p>此前状态：{statusLabel(marker.previous_status.toLowerCase())} · {marker.previous_error || '未记录错误'}</p><p>{marker.instruction}</p></div>)}</details>}
      <h3>任务轨迹</h3><div className="task-trace">{trace.map(event => <details className="knowledge-use" key={event.seq} open={event.type.startsWith('run.continu')}><summary>#{event.seq} · {phaseLabel(event.phase)} · {eventLabel(event.type)}</summary><pre className="log-view">{JSON.stringify(event.payload, null, 2)}</pre></details>)}</div>{!trace.length && <p className="muted">尚无已记录的轨迹事件</p>}
      <h3>本次规则快照</h3>
      {detail.ruleSnapshot ? <div className="knowledge-use"><p>{detail.ruleSnapshot.refs.length} 条规则 · 派生 Run 保留以下版本</p>{detail.ruleSnapshot.refs.map(ref => <div key={ref.id}>{ref.id} · v{ref.version}</div>)}{detail.parentRunId && <p>父 Run：{detail.parentRunId}</p>}</div> : <p className="muted">尚未生成规则快照，暂时不能派生 Run。</p>}
      {detail.ruleSnapshot && detail.status !== 'running' && detail.status !== 'stopping' && <form className="derive-run-form" onSubmit={async event => {
        event.preventDefault(); if (deriving) return; setDeriving(true);
        try {const created = await deriveRun(detail.id, additionalRuleIds, deriveGoal.trim() || detail.goal); if (created.id) onSelect(created.id); setDeriveGoal(''); setAdditionalRuleIds([]);}
        catch (error) {report(error);} finally {setDeriving(false);}
      }}><label htmlFor="derive-goal">派生 Run 目标</label><textarea id="derive-goal" value={deriveGoal} onChange={event => setDeriveGoal(event.target.value)} maxLength={4000} placeholder="默认沿用父 Run 目标；可填写本次派生 Run 的补充目标"/><fieldset><legend>追加检测规则（父 Run 规则会自动继承）</legend><div className="derive-rule-list">{ruleOptions.filter(rule => !detail.ruleSnapshot?.refs.some(ref => ref.id === rule.id)).length ? ruleOptions.filter(rule => !detail.ruleSnapshot?.refs.some(ref => ref.id === rule.id)).map(rule => <label key={rule.id}><input type="checkbox" checked={additionalRuleIds.includes(rule.id)} onChange={event => setAdditionalRuleIds(previous => event.target.checked ? [...previous, rule.id] : previous.filter(id => id !== rule.id))}/><span>{rule.name}<small>{rule.id} · v{rule.version}</small></span></label>) : <span className="muted">暂无可追加的已启用规则或草稿</span>}</div></fieldset><button className="primary" disabled={deriving}>{deriving ? '启动中…' : '创建派生 Run'}</button></form>}
    </section>}
    {detail && <section className="card run-detail"><div className="section-heading"><div><span className="eyebrow">RUN DETAIL</span><h2>{detail.goal}</h2><p>{detail.agentRunId || detail.id}</p></div><button className="secondary" onClick={() => onCapture(detail)}>沉淀为知识</button></div>{detail.timeSource && <div className="notice">此记录导入自历史报告，未记录的服务模式与启动时间不作推断。</div>}<dl><dt>执行状态</dt><dd>{statusLabel(detail.status)} · {phaseLabel(detail.phase)}</dd><dt>验证结论</dt><dd>{detail.outcome ? outcomeLabel(detail.outcome) : '尚未生成验证结论'}</dd><dt>{detail.timeSource ? '报告文件时间' : '开始 / 结束'}</dt><dd>{detail.timeSource ? timeLabel(detail.finishedAt) : `${timeLabel(detail.startedAt)} / ${timeLabel(detail.finishedAt)}`}</dd><dt>退出码</dt><dd>{detail.exitCode ?? (detail.timeSource ? '历史未记录' : '进程尚未退出')}</dd><dt>工作分支</dt><dd>{detail.branch || '尚未生成候选分支'}</dd></dl>{detail.error && <div className="notice failure">{detail.error}</div>}
      <div className="detail-block"><h3>使用的知识来源</h3>{detail.knowledge.length ? detail.knowledge.map((entry, index) => <div className="knowledge-use" key={index}><span className="badge">{phaseLabel(entry.phase)}</span><p>检索：{entry.queries.join(' · ') || 'Agent 未请求检索'}</p><div className="source-links">{entry.documents.length ? entry.documents.map(document => <button key={document.id} onClick={() => onDocument(document.id)}>▤ {document.title} · v{document.version}</button>) : <span className="muted">未选择相关文档</span>}</div></div>) : <p className="muted">尚无知识检索记录。Agent 会在测试规划、探索或诊断阶段按需检索。</p>}</div>
      <div className="detail-block"><h3>报告与补丁</h3><div className="artifact-links">{detail.artifacts?.length ? detail.artifacts.map(artifact => <a href={artifactUrl(detail.id, artifact.ref)} key={artifact.ref} download>↓ {artifact.label}<small>{Math.ceil(artifact.bytes / 1024)} KiB</small></a>) : <p className="muted">此 Run 尚未产生可下载的报告或补丁。</p>}</div></div>
      <div className="detail-block"><h3>进程日志 <span className="muted">最近 120 个输出片段</span></h3><pre className="log-view">{detail.logs?.join('') || '等待 Agent 输出…'}</pre></div>
    </section>}
  </>;
}
