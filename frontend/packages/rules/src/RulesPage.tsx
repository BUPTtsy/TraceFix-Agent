import {FormEvent, useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {changeRuleStatus, deleteRule, DetectionRule, loadRuleFindings, loadRuleInsights, loadRulePreview, loadRules, loadRuleVersions, RuleInsight, RuleVersion, saveRule} from '@tracefix/api-client';

interface Props {onDirty: (dirty: boolean) => void; report: (error: unknown) => void}
const statusLabels: Record<string, string> = {draft: '草稿', enabled: '已发布', disabled: '已停用', archived: '已归档'};
const severityLabels: Record<string, string> = {blocker: '阻断', critical: '严重', major: '主要', minor: '一般'};
const categoryLabels: Record<string, string> = {functional: '功能', a11y: '无障碍', console: 'Console', network: '网络', visual: '视觉', performance: '性能', security: '安全', i18n: '国际化', 'code-pattern': '代码模式'};

function blankRule(): Partial<DetectionRule> {return {id: '', name: '', version: 0, status: 'draft', category: 'functional', severity: 'major', priority: 50, pinned: false, scope: {level: 'org', project_ids: [], url_patterns: [], path_globs: [], frameworks: []}, phases: ['EXPLORE', 'DIAGNOSE', 'VERIFY'], detection: {type: 'oracle', oracle: {kind: 'console_no_error'}}, fix_guidance: '', examples: {}, tags: [], owner: ''};}
function asJson(value: unknown): string {return JSON.stringify(value || {}, null, 2);}

export function RulesPage({onDirty, report}: Props) {
  const [rules, setRules] = useState<DetectionRule[]>([]), [query, setQuery] = useState(''), [status, setStatus] = useState('');
  const [draft, setDraft] = useState<Partial<DetectionRule> | null>(null), [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  const [versions, setVersions] = useState<RuleVersion[]>([]), [insights, setInsights] = useState<RuleInsight[]>([]), [findings, setFindings] = useState<Array<Record<string, unknown>>>([]), [prompt, setPrompt] = useState(''), [previewItems, setPreviewItems] = useState<Array<Record<string, unknown>>>([]), [configText, setConfigText] = useState('');
  const [dirty, setDirty] = useState(false), [failure, setFailure] = useState('');
  const pending = useRef(false), dirtyRef = useRef(false), refreshVersion = useRef(0);
  dirtyRef.current = dirty || busy;
  useEffect(() => {onDirty(dirty || busy);}, [dirty, busy, onDirty]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {if (dirtyRef.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn);
    return () => {window.removeEventListener('beforeunload', warn); refreshVersion.current += 1;};
  }, []);
  const refresh = useCallback(async () => {const version = ++refreshVersion.current; const [items, metrics] = await Promise.all([loadRules('', {status, q: query, includeArchived: status === 'archived'}), loadRuleInsights()]); if (version === refreshVersion.current) {setRules(items); setInsights(metrics);}}, [query, status]);
  useEffect(() => {refresh().catch(report);}, [refresh, report]);
  const insightMap = useMemo(() => new Map(insights.map(item => [item.rule_id || '', item])), [insights]);
  useEffect(() => {
    let active = true;
    setVersions([]); setFindings([]); setPrompt(''); setPreviewItems([]);
    if (draft?.id && draft.version) {
      Promise.all([loadRuleVersions(draft.id), loadRuleFindings(draft.id), loadRulePreview(draft.id)])
        .then(([history, records, preview]) => {
          if (active) {setVersions(history); setFindings(records); setPrompt(preview.prompt); setPreviewItems(preview.items);}
        }).catch(error => {if (active) report(error);});
    }
    return () => {active = false;};
  }, [draft?.id, draft?.version, report]);
  function canReplace() {return !dirtyRef.current || window.confirm('规则尚未保存，确认放弃当前更改？');}
  function open(rule: DetectionRule) {if (pending.current || !canReplace()) return; setDraft(rule); setDirty(false); setConfigText(asJson(rule.detection)); setNotice(''); setFailure('');}
  function update(fields: Partial<DetectionRule>) {setDraft(previous => previous ? {...previous, ...fields} : previous); setDirty(true); setNotice(''); setFailure('');}
  function newRule() {if (pending.current || !canReplace()) return; const value = blankRule(); setDraft(value); setDirty(true); setVersions([]); setConfigText(asJson(value.detection)); setNotice(''); setFailure('');}
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || pending.current) return;
    pending.current = true; setBusy(true); setFailure('');
    try {
      let detection: unknown;
      try {detection = JSON.parse(configText);} catch {throw new Error('检测配置必须是有效 JSON');}
      if (!detection || typeof detection !== 'object' || Array.isArray(detection) || !('type' in detection) || detection.type !== draft.detection?.type) throw new Error('检测配置的 type 必须与所选检测类型一致');
      const selectedType = draft.detection?.type;
      if (selectedType !== 'oracle' && selectedType !== 'static' && selectedType !== 'guided') throw new Error('检测类型无效');
      const normalizedDetection = {...draft.detection, ...detection, type: selectedType};
      const saved = await saveRule({...draft, detection: normalizedDetection, id: draft.id?.trim() || undefined, name: draft.name?.trim(), expectedVersion: draft.version || undefined});
      setDraft(saved); setDirty(false); setConfigText(asJson(saved.detection)); setNotice(`已保存 v${saved.version}，当前状态：${statusLabels[saved.status]}`);
      await refresh();
    } catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}
    finally {pending.current = false; setBusy(false);}
  }
  async function statusAction(action: 'publish' | 'disable' | 'archive') {if (!draft?.id || !draft.version || pending.current) return; if (dirty) {setFailure('请先保存规则内容，再变更状态。'); return;} pending.current = true; setBusy(true); setFailure(''); try {const saved = await changeRuleStatus(draft.id, action, draft.version); setDraft(saved); setNotice(`规则已${action === 'publish' ? '发布' : action === 'disable' ? '停用' : '归档'}`); await refresh();} catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);} finally {pending.current = false; setBusy(false);}}
  async function remove() {if (!draft?.id || !draft.version || pending.current || !window.confirm('确认删除这条规则？已被 Run 引用的规则只能归档。')) return; pending.current = true; setBusy(true); setFailure(''); try {await deleteRule(draft.id); setDraft(null); setDirty(false); setNotice('规则已删除。'); await refresh();} catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);} finally {pending.current = false; setBusy(false);}}
  const currentInsight = draft?.id ? insightMap.get(draft.id) : undefined;
  return <>
    {notice && !draft && <div className="notice" role="status">{notice}</div>}
    <section className="card rules-library"><div className="section-heading"><div><h2>统一规则管理 <span className="count">{rules.length}</span></h2><p>集中维护规则内容与版本，发布后可在各仓库的配置规则页面勾选生效</p></div><div className="button-row"><button className="secondary" disabled={busy} onClick={() => refresh().catch(report)}>刷新</button><button className="primary" disabled={busy} onClick={newRule}>＋ 新建规则</button></div></div>
      <div className="toolbar"><input aria-label="搜索检测规则" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、标签或规则 ID…"/><select aria-label="规则状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></div>
      {rules.length ? <div className="rule-table"><div className="rule-row rule-head"><span>规则</span><span>类型 / 类别</span><span>严重级别</span><span>版本与命中</span><span>状态</span></div>{rules.map(rule => {const metric = insightMap.get(rule.id); return <button key={rule.id} className={'rule-row ' + (draft?.id === rule.id ? 'selected' : '')} disabled={busy} onClick={() => open(rule)} aria-pressed={draft?.id === rule.id}><span><strong>{rule.pinned ? '◆ ' : ''}{rule.name}</strong><small>{rule.id}</small></span><span><b>{rule.detection.type}</b><small>{categoryLabels[rule.category] || rule.category}</small></span><span><b className={'severity-' + rule.severity}>{severityLabels[rule.severity] || rule.severity}</b><small>优先级 {rule.priority}</small></span><span><b>v{rule.version}</b><small>{metric?.total || 0} 次命中 · 误报 {metric ? `${Math.round(metric.false_positive_rate * 100)}%` : '—'}</small></span><span className={'badge ' + (rule.status === 'enabled' ? 'success' : rule.status === 'draft' ? 'warning' : '')}>{statusLabels[rule.status]}</span></button>;})}</div> : <div className="empty-state"><span aria-hidden="true">◇</span><h3>还没有匹配的检测规则</h3><p>创建并发布规则后，在项目空间中为相应仓库勾选；保存后对新运行生效。</p></div>}
    </section>
    {draft && <section className="card rule-editor"><form onSubmit={save}><div className="section-heading"><div><span className="eyebrow">RULE EDITOR</span><h2>{draft.version ? '编辑检测规则' : '新建检测规则'} {!!draft.version && <small className="muted">v{draft.version}</small>}</h2></div><button type="button" className="text-button" disabled={busy} onClick={() => {if (canReplace()) {setDraft(null); setDirty(false); setFailure('');}}}>关闭</button></div>{notice && <div className="notice" role="status">{notice}</div>}<fieldset disabled={busy} className="repository-fields editor-fields"><div className="form-grid"><label>规则 ID（可选）<input pattern="[A-Za-z0-9][A-Za-z0-9_.:\x2D]{0,119}" disabled={busy || Boolean(draft.version)} value={draft.id || ''} onChange={event => update({id: event.target.value})} placeholder="rule_checkout_feedback"/></label><label>规则名称<input required maxLength={160} value={draft.name || ''} onChange={event => update({name: event.target.value})} placeholder="提交后必须有反馈"/></label></div><div className="form-grid"><label>检测类型<select value={draft.detection?.type || 'oracle'} onChange={event => {const type = event.target.value as 'oracle' | 'static' | 'guided'; const detection = {type, [type]: type === 'oracle' ? {kind: 'console_no_error'} : type === 'static' ? {regex: 'TODO', path_globs: ['**']} : {prompt: '检查页面是否符合预期，并引用命中规则。'}}; update({detection}); setConfigText(asJson(detection));}}><option value="oracle">Oracle · 运行时</option><option value="static">Static · 代码</option><option value="guided">Guided · 引导</option></select></label><label>类别<select value={draft.category || 'functional'} onChange={event => update({category: event.target.value})}>{Object.entries(categoryLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div><div className="form-grid"><label>严重级别<select value={draft.severity || 'major'} onChange={event => update({severity: event.target.value})}>{Object.entries(severityLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>优先级（1-100）<input type="number" min="1" max="100" value={draft.priority || 50} onChange={event => update({priority: Number(event.target.value)})}/></label></div><label className="knowledge-toggle"><input type="checkbox" checked={Boolean(draft.pinned)} onChange={event => update({pinned: event.target.checked})}/>置顶注入摘要</label><label>检测配置（JSON）<textarea className="rule-json" required value={configText} onChange={event => {setConfigText(event.target.value); setDirty(true); setFailure(''); setNotice('');}} /></label><label>修复指引<textarea maxLength={4096} value={draft.fix_guidance || ''} onChange={event => update({fix_guidance: event.target.value})} placeholder="说明如何修复违反规则的问题"/></label><div className="form-grid"><label>标签（英文逗号分隔）<input value={(draft.tags || []).join(',')} onChange={event => update({tags: event.target.value.split(',').map(tag => tag.trim()).filter(Boolean)})}/></label><label>负责人<input value={draft.owner || ''} onChange={event => update({owner: event.target.value})}/></label></div><div className="editor-footer"><span className="muted">保存会生成新版本；启用前会检查敏感信息和 4 KiB 限制。</span><button className="primary" disabled={busy || !draft.name?.trim()}>{busy ? '保存中…' : '保存规则'}</button></div><div className="button-row"><button type="button" className="secondary" disabled={busy || !draft.version || draft.status === 'enabled'} onClick={() => statusAction('publish')}>发布规则</button><button type="button" className="secondary" disabled={busy || !draft.version || draft.status !== 'enabled'} onClick={() => statusAction('disable')}>停用</button><button type="button" className="secondary" disabled={busy || !draft.version || draft.status === 'archived'} onClick={() => statusAction('archive')}>归档</button><button type="button" className="text-button danger" disabled={busy || !draft.version} onClick={remove}>删除</button></div>{failure && <div className="notice failure" role="alert">{failure}</div>}</fieldset></form></section>}
    {draft?.id && !!draft.version && <section className="card rule-history"><div className="section-heading"><div><h2>版本与质量</h2><p>规则历史版本不可变；回滚会创建新版本</p></div><span className="badge">{currentInsight ? `${currentInsight.total} 次命中` : '暂无命中'}</span></div>{currentInsight && <div className="rule-metrics"><span>复现率 <b>{Math.round(currentInsight.reproduction_rate * 100)}%</b></span><span>误报率 <b>{Math.round(currentInsight.false_positive_rate * 100)}%</b></span><span>修复率 <b>{Math.round(currentInsight.fix_rate * 100)}%</b></span></div>}<div className="rule-prompt"><b>注入提示词预览</b><pre>{prompt || '正在读取已保存规则的注入预览…'}</pre><code>{JSON.stringify(previewItems.map(({scope, phases, ...content}) => content), null, 2)}</code></div><div className="version-list">{versions.map(version => <div key={version.version}><b>v{version.version}</b><span>{version.change_note || '无变更说明'}</span><small>{version.author} · {new Date(version.created_at).toLocaleString('zh-CN')}</small></div>)}</div>{findings.length > 0 && <div className="finding-list"><b>最近命中</b>{findings.slice(0, 5).map(item => <div key={String(item.id)}><span>{String(item.title || '规则命中')}</span><small>{String(item.status || 'suspected')} · {String(item.run_id || '')}</small></div>)}</div>}</section>}
  </>;
}
