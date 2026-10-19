import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {loadRepositoryRules, loadRules, Project, RuleStatus, saveRepositoryRules} from '@tracefix/api-client';

interface Props {projectId: string; project?: Project; projectsLoaded: boolean; onDirty: (dirty: boolean) => void; onNavigate: (hash: string) => void; report: (error: unknown) => void}
interface RuleOption {id: string; name: string; category: string; severity: string; status: RuleStatus}
const statusText: Record<string, string> = {enabled: '已发布，可生效', archived: '已归档，不再生效'};

// 单个仓库的规则配置页：从统一规则目录中勾选本仓库新运行要注入的规则。
export function RepositoryRulesPage({projectId, project, projectsLoaded, onDirty, onNavigate, report}: Props) {
  const [options, setOptions] = useState<RuleOption[]>([]), [selected, setSelected] = useState<string[]>([]), [version, setVersion] = useState<number | null>(null);
  const [query, setQuery] = useState(''), [busy, setBusy] = useState(false), [dirty, setDirty] = useState(false), [notice, setNotice] = useState(''), [failure, setFailure] = useState('');
  const requestVersion = useRef(0), pending = useRef(false), dirtyRef = useRef(false);
  dirtyRef.current = dirty || busy;
  useEffect(() => {onDirty(dirty || busy);}, [dirty, busy, onDirty]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {if (dirtyRef.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  const refresh = useCallback(async () => {
    if (!project) return;
    const current = ++requestVersion.current;
    try {
      const [rules, settings] = await Promise.all([loadRules('', {includeArchived: true}), loadRepositoryRules(project.id)]);
      if (current !== requestVersion.current) return;
      setOptions(rules.map(rule => ({id: rule.id, name: rule.name, category: rule.category, severity: rule.severity, status: rule.status})));
      setSelected(settings.ruleIds); setVersion(settings.version); setDirty(false); setFailure('');
    } catch (error) {if (current === requestVersion.current) {setFailure('规则配置读取失败，请重新读取。'); report(error);}}
  }, [project?.id, report]);
  useEffect(() => {refresh(); return () => {requestVersion.current += 1;};}, [refresh]);
  async function save() {
    if (!project || version === null || pending.current || !dirty) return;
    const current = ++requestVersion.current;
    pending.current = true; setBusy(true); setFailure(''); setNotice('');
    try {
      const saved = await saveRepositoryRules(project.id, selected, version);
      if (current !== requestVersion.current) return;
      setSelected(saved.ruleIds); setVersion(saved.version); setDirty(false); setNotice('仓库规则配置已保存，新运行将按勾选结果注入规则。');
    } catch (error) {if (current === requestVersion.current) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}}
    finally {pending.current = false; setBusy(false);}
  }
  function toggle(id: string, checked: boolean) {setSelected(previous => checked ? [...previous, id] : previous.filter(item => item !== id)); setDirty(true); setNotice('');}
  const visible = useMemo(() => {const text = query.trim().toLowerCase(); return text ? options.filter(rule => rule.name.toLowerCase().includes(text) || rule.id.toLowerCase().includes(text)) : options;}, [options, query]);
  const published = options.filter(rule => rule.status === 'enabled').length;
  const effective = selected.filter(id => options.some(rule => rule.id === id && rule.status === 'enabled')).length;
  if (!project) return <section className="card"><div className="empty-state"><span aria-hidden="true">◇</span><h3>{projectsLoaded ? `未找到仓库 ${projectId || ''}` : '正在读取仓库列表…'}</h3><p>{projectsLoaded ? '仓库可能尚未注册或已被移除，请回到项目空间选择仓库。' : '读取完成后会自动显示规则配置。'}</p>{projectsLoaded && <button className="secondary" onClick={() => onNavigate('projects')}>返回项目空间</button>}</div></section>;
  return <>
    {notice && <div className="notice" role="status">{notice}</div>}
    <section className="card repository-rules">
      <div className="section-heading"><div><span className="eyebrow">REPOSITORY RULES · {project.id}</span><h2>配置规则 <span className="count">{selected.length}</span></h2><p>勾选并保存后，仅已发布的规则参与本仓库的新运行；历史运行保留原有规则版本。</p></div><div className="button-row"><button className="secondary" disabled={busy} onClick={() => {if (!dirty || window.confirm('重新读取会放弃尚未保存的规则勾选，确认继续？')) refresh();}}>重新读取规则</button><button className="primary" disabled={busy || version === null || !dirty} onClick={save}>{busy ? '保存中…' : '保存规则配置'}</button></div></div>
      <div className="rule-metrics"><span>已勾选 <b>{selected.length}</b></span><span>其中生效 <b>{effective}</b></span><span>目录中已发布 <b>{published}</b> / {options.length}</span></div>
      {version !== null && options.length > 0 && <div className="toolbar"><input aria-label="筛选规则" value={query} onChange={event => setQuery(event.target.value)} placeholder="按名称或规则 ID 筛选…"/></div>}
      {version === null ? <div className="notice">{failure ? '规则配置读取失败，请重新读取。' : '正在读取规则配置…'}</div> : options.length ? <div className="repository-rule-list">{visible.map(rule => <label key={rule.id} className="knowledge-toggle"><input type="checkbox" checked={selected.includes(rule.id)} disabled={busy || (rule.status !== 'enabled' && !selected.includes(rule.id))} onChange={event => toggle(rule.id, event.target.checked)}/><span><strong>{rule.name}</strong><small>{rule.id} · {statusText[rule.status] || '未发布，不会生效'}</small></span></label>)}{!visible.length && <p className="muted">没有匹配的规则。</p>}</div> : <div className="empty-state"><span aria-hidden="true">◇</span><h3>统一规则目录为空</h3><p>先在“统一规则管理”中创建并发布规则，再回到这里勾选。</p></div>}
      {failure && <div className="notice failure" role="alert">{failure}</div>}
      <div className="button-row"><button className="secondary" onClick={() => onNavigate('rules')}>统一规则管理</button><button className="secondary" onClick={() => onNavigate('projects')}>返回项目空间</button></div>
    </section>
  </>;
}
