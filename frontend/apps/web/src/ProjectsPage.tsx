import {FormEvent, useCallback, useEffect, useRef, useState} from 'react';
import {ApiError, deleteRepository, loadRepository, loadRepositoryRules, loadRules, Project, RepositoryDraft, RepositoryProfile, RuleStatus, saveRepository, saveRepositoryRules} from '@tracefix/api-client';

interface Props {projects: Project[]; projectId: string; onSelect: (id: string) => boolean; onSaved: (id: string) => Promise<void>; onDirty: (dirty: boolean) => void; onNavigate: (page: 'runs' | 'rules') => void; report: (error: unknown) => void}
interface Draft {id: string; url: string; ref: string; root: string; mode: RepositoryDraft['checkout_mode']; commands: string; allowedFiles: string; allowedUrls: string; appUrl: string; credential: string; enabled: boolean; publish: boolean; profile: RepositoryProfile | null}
const defaultCommands = {prepare: ['node', '--version'], start: ['node', 'scripts/serve.mjs'], reset: ['node', 'scripts/reset.mjs'], static: ['node', 'node_modules/typescript/bin/tsc', '--noEmit'], unit: ['node', '--test', 'tests/tasks.test.mjs'], build: ['node', 'scripts/build.mjs']};
function blank(projects: Project[]): Draft {
  let id = 'test-bug-repo', suffix = 2;
  while (projects.some(project => project.id.toLowerCase() === id)) id = 'test-bug-repo-' + suffix++;
  return {id, url: 'https://github.com/BUPTtsy/Test-Bug-repo', ref: 'main', root: '', mode: 'managed', commands: JSON.stringify(defaultCommands, null, 2), allowedFiles: 'src/**\nserver/**', allowedUrls: 'http://app:3000', appUrl: 'http://app:3000', credential: '', enabled: true, publish: false, profile: null};
}
function lines(text: string): string[] {return [...new Set(text.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];}
function commandsFrom(text: string): Record<string, string[]> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('命令必须是 JSON 对象');
  const commands: Record<string, string[]> = {};
  for (const [name, argv] of Object.entries(value)) {
    if (!Array.isArray(argv) || !argv.length || !argv.every(argument => typeof argument === 'string' && argument.length > 0)) throw new Error(`${name} 必须是非空字符串 argv 数组`);
    commands[name] = argv;
  }
  if (['start', 'reset', 'static', 'unit', 'build'].some(name => !commands[name])) throw new Error('必须配置 start、reset、static、unit、build 命令');
  return commands;
}

export function ProjectsPage({projects, projectId, onSelect, onSaved, onDirty, onNavigate, report}: Props) {
  const [draft, setDraft] = useState<Draft | null>(null), [existingId, setExistingId] = useState('');
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [notice, setNotice] = useState(''), [failure, setFailure] = useState('');
  const [ruleOptions, setRuleOptions] = useState<Array<{id: string; name: string; category: string; severity: string; status: RuleStatus}>>([]);
  const [selectedRuleIds, setSelectedRuleIds] = useState<string[]>([]), [rulesVersion, setRulesVersion] = useState<number | null>(null), [rulesBusy, setRulesBusy] = useState(false), [rulesDirty, setRulesDirty] = useState(false);
  const requestVersion = useRef(0), rulesRequestVersion = useRef(0), changed = useRef(false), saving = useRef(false), rulesPending = useRef(false), rulesChanged = useRef(false), currentProjectId = useRef(projectId);
  currentProjectId.current = projectId;
  rulesChanged.current = rulesDirty;
  useEffect(() => {onDirty(changed.current || rulesDirty || busy || rulesBusy);}, [draft, rulesDirty, busy, rulesBusy, onDirty]);
  const project = projects.find(item => item.id === projectId);
  const duplicate = !existingId && draft ? projects.find(item => item.id.toLowerCase() === draft.id.trim().toLowerCase()) : undefined;
  const refreshRules = useCallback(async () => {
    if (!projectId) return;
    const version = ++rulesRequestVersion.current;
    try {
      const [rules, settings] = await Promise.all([loadRules('', {includeArchived: true}), loadRepositoryRules(projectId)]);
      if (version !== rulesRequestVersion.current || currentProjectId.current !== projectId) return;
      setRuleOptions(rules.map(rule => ({id: rule.id, name: rule.name, category: rule.category, severity: rule.severity, status: rule.status})));
      setSelectedRuleIds(settings.ruleIds); setRulesVersion(settings.version); setRulesDirty(false); setFailure('');
    } catch (error) {if (version === rulesRequestVersion.current && currentProjectId.current === projectId) {setFailure('规则配置读取失败，请重新读取。'); report(error);}}
  }, [projectId, report]);
  useEffect(() => {
    requestVersion.current += 1; setDraft(null); setExistingId(''); setLoading(false); setFailure(''); changed.current = false; setRulesDirty(false); onDirty(false);
    setRuleOptions([]); setSelectedRuleIds([]); setRulesVersion(null);
    refreshRules();
    return () => {rulesRequestVersion.current += 1;};
  }, [projectId, onDirty, refreshRules]);
  useEffect(() => () => {requestVersion.current += 1; rulesRequestVersion.current += 1;}, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {if (changed.current || rulesChanged.current || saving.current || rulesPending.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  function update(fields: Partial<Draft>) {setDraft(previous => previous ? {...previous, ...fields} : previous); changed.current = true; onDirty(true); setFailure(''); setNotice('');}
  function close() {if (changed.current && !window.confirm('仓库配置尚未保存，确认放弃更改？')) return; requestVersion.current += 1; setDraft(null); setLoading(false); changed.current = false; onDirty(rulesChanged.current);}
  function create() {if (changed.current && !window.confirm('仓库配置尚未保存，确认放弃更改？')) return; requestVersion.current += 1; setDraft(blank(projects)); setExistingId(''); setLoading(false); setNotice(''); setFailure(''); changed.current = true; onDirty(true);}
  async function edit(id = project?.id) {
    if (!id || loading || busy) return;
    if (changed.current && !window.confirm('仓库配置尚未保存，确认重新读取？')) return;
    const version = ++requestVersion.current;
    setLoading(true); setFailure('');
    try {
      const record = await loadRepository(id);
      if (version !== requestVersion.current) return;
      setExistingId(record.repo_id); setDraft({id: record.repo_id, url: `https://github.com/${record.owner}/${record.repo}`, ref: record.default_ref, root: record.root || '', mode: record.checkout_mode || 'existing', commands: JSON.stringify(record.profile.commands, null, 2), allowedFiles: record.allowed_files.join('\n'), allowedUrls: record.allowed_urls.join('\n'), appUrl: record.profile.url, credential: record.credential_ref || '', enabled: record.enabled, publish: record.publish_branch === true, profile: record.profile});
      changed.current = false; onDirty(rulesChanged.current); setNotice('');
    } catch (error) {if (version === requestVersion.current) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}}
    finally {if (version === requestVersion.current) setLoading(false);}
  }
  async function remove() {
    if (!project || saving.current || !window.confirm(`确认移除仓库 ${project.id} 的注册配置？本地代码目录会保留；有关联记录的仓库请改为停用。`)) return;
    saving.current = true; setBusy(true); setFailure('');
    try {
      await deleteRepository(project.id);
      changed.current = false; onDirty(rulesChanged.current); setDraft(null); setNotice('仓库注册已移除，本地代码目录已保留。');
      await onSaved('');
    } catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}
    finally {saving.current = false; setBusy(false);}
  }
  async function saveRules() {
    if (!project || rulesVersion === null || rulesPending.current || !rulesDirty) return;
    const id = project.id, version = ++rulesRequestVersion.current;
    rulesPending.current = true; setRulesBusy(true); setFailure('');
    try {
      const saved = await saveRepositoryRules(id, selectedRuleIds, rulesVersion);
      if (version !== rulesRequestVersion.current || currentProjectId.current !== id) return;
      setSelectedRuleIds(saved.ruleIds); setRulesVersion(saved.version); setRulesDirty(false); setNotice('仓库规则配置已保存，新运行将按勾选结果注入规则。');
    } catch (error) {if (version === rulesRequestVersion.current && currentProjectId.current === id) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}}
    finally {rulesPending.current = false; setRulesBusy(false);}
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || saving.current) return;
    saving.current = true; setBusy(true); setFailure(''); setNotice('');
    try {
      if (duplicate) throw new Error(`项目 ID ${duplicate.id} 已注册，请编辑已有仓库或使用其他 ID`);
      if (!/^[A-Za-z0-9_-]{1,64}$/.test(draft.id.trim())) throw new Error('项目 ID 只能包含 1-64 个英文字母、数字、下划线或连字符');
      const url = new URL(draft.url);
      const match = url.pathname.match(/^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/);
      if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash || !match) throw new Error('仓库地址必须是 https://github.com/owner/repo');
      const allowedUrls = lines(draft.allowedUrls);
      if (!allowedUrls.length) throw new Error('请至少填写一个允许访问的来源');
      for (const origin of allowedUrls) {
        const parsed = new URL(origin);
        if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash || !['', '/'].includes(parsed.pathname)) throw new Error('允许访问的来源必须是完整的 http(s) origin，不含路径、参数或凭据');
      }
      const appUrl = new URL(draft.appUrl);
      if (!allowedUrls.some(origin => new URL(origin).origin === appUrl.origin)) throw new Error('测试地址必须属于允许访问的来源');
      const repository: RepositoryDraft = {repo_id: draft.id.trim(), owner: match[1], repo: match[2], default_ref: draft.ref.trim(), checkout_mode: draft.mode, publish_branch: draft.publish, allowed_files: lines(draft.allowedFiles), allowed_urls: allowedUrls, credential_ref: draft.credential.trim() || null, enabled: draft.enabled, profile: {...draft.profile, project: draft.id.trim(), url: draft.appUrl.trim(), allowed_origins: allowedUrls, commands: commandsFrom(draft.commands)}};
      if (draft.root.trim()) repository.root = draft.root.trim();
      if (draft.mode === 'existing' && !repository.root) throw new Error('使用已有 checkout 时必须填写目标目录');
      const saved = await saveRepository(repository, existingId || undefined);
      changed.current = false; onDirty(rulesChanged.current); setDraft(null); setNotice('仓库配置已保存。启动任务时，Agent 将准备工作区并运行命令。');
      try {await onSaved(saved.repo_id);}
      catch (error) {setNotice('仓库配置已保存，但列表刷新失败，请刷新页面读取最新配置。'); report(error);}
    } catch (error) {setFailure(error instanceof ApiError && error.code === 'REPO_ID_CONFLICT' ? '项目 ID 已被注册，请读取已有配置后编辑。' : error instanceof Error ? error.message : String(error)); report(error);}
    finally {saving.current = false; setBusy(false);}
  }
  return <>
    <section className="card"><div className="section-heading"><div><h2>仓库接入</h2><p>配置 GitHub 来源、工作区准备和验证命令，让 Agent 自行获取目标代码。</p></div><button className="primary" disabled={busy || rulesBusy} onClick={create}>＋ 注册仓库</button></div></section>
    {notice && <div className="notice" role="status">{notice}</div>}
    <div className="project-grid">{projects.map(item => <button key={item.id} className={'card project-card ' + (projectId === item.id ? 'selected' : '')} disabled={busy || rulesBusy} onClick={() => onSelect(item.id)} aria-pressed={projectId === item.id}><div><span className="project-avatar" aria-hidden="true">{item.id.slice(0, 1).toUpperCase()}</span><span className={'badge ' + (item.ready ? 'success' : 'warning')}>{item.ready ? '配置就绪' : '待配置'}</span></div><h2>{item.id}</h2><p>{item.repoId}</p><code>{item.root}</code><footer><span>{item.runCount} 次运行</span><span>{item.documentCount} 篇文档</span></footer></button>)}</div>
    {!projects.length && !draft && <div className="notice">注册远程仓库后即可配置检测规则，并从右侧启动 Test 或 Repair。</div>}
    {project && <section className="card project-detail"><div className="section-heading"><div><h2>{project.id} 的运行配置</h2><p>仓库与 Profile 持久化保存，运行启动时冻结配置。</p></div><button className="secondary" disabled={busy || loading} onClick={() => edit()}>{loading ? '读取中…' : '编辑仓库配置'}</button></div>{project.issue && <div className="notice">{project.issue}</div>}<dl><dt>GitHub 仓库</dt><dd>{project.repoId}</dd><dt>目标目录</dt><dd><code>{project.root}</code></dd><dt>测试地址</dt><dd>{project.url || '未配置'}</dd><dt>允许修改</dt><dd>{project.allowedFiles.join(' · ')}</dd></dl><div className="command-list">{Object.entries(project.commands).map(([name, argv]) => <div key={name}><span>{name}</span><code>{JSON.stringify(argv)}</code></div>)}</div><div className="button-row">{Boolean(project.repositoryConfigVersion) && <button className="text-button danger" disabled={busy || loading} onClick={remove}>移除仓库注册</button>}<button className="secondary" onClick={() => onNavigate('rules')}>统一规则管理</button><button className="primary" onClick={() => onNavigate('runs')}>查看运行与补丁</button></div></section>}
    {project && <section className="card repository-rules"><div className="section-heading"><div><span className="eyebrow">REPOSITORY RULES</span><h2>配置规则 <span className="count">{selectedRuleIds.length}</span></h2><p>勾选并保存后，仅已发布的规则参与本仓库的新运行；历史运行保留原有规则版本。</p></div><div className="button-row"><button className="secondary" disabled={busy || rulesBusy} onClick={() => {if (!rulesDirty || window.confirm('重新读取会放弃尚未保存的规则勾选，确认继续？')) refreshRules();}}>重新读取规则</button><button className="primary" disabled={busy || rulesBusy || rulesVersion === null || !rulesDirty} onClick={saveRules}>{rulesBusy ? '保存中…' : '保存规则配置'}</button></div></div>{rulesVersion === null ? <div className="notice">正在等待规则配置，请在读取失败后重试。</div> : ruleOptions.length ? <div className="repository-rule-list">{ruleOptions.map(rule => <label key={rule.id} className="knowledge-toggle"><input type="checkbox" checked={selectedRuleIds.includes(rule.id)} disabled={busy || rulesBusy || (rule.status !== 'enabled' && !selectedRuleIds.includes(rule.id))} onChange={event => {setSelectedRuleIds(previous => event.target.checked ? [...previous, rule.id] : previous.filter(id => id !== rule.id)); setRulesDirty(true);}}/><span><strong>{rule.name}</strong><small>{rule.id} · {rule.status === 'enabled' ? '已发布，可生效' : rule.status === 'archived' ? '已归档，不再生效' : '未发布，不会生效'}</small></span></label>)}</div> : <div className="empty-state"><span aria-hidden="true">◇</span><h3>统一规则目录为空</h3><p>先在“统一规则管理”中创建并发布规则，再回到这里勾选。</p></div>}</section>}
    {failure && <div className="notice failure" role="alert">{failure}</div>}
    {draft && <section className="card repository-editor"><form onSubmit={save}><div className="section-heading"><div><span className="eyebrow">REPOSITORY CONFIGURATION</span><h2>{existingId ? '编辑仓库' : '注册远程仓库'}</h2><p>准备和验证命令以 argv 数组运行，保存后由 Agent 执行。</p></div><button type="button" className="text-button" disabled={busy} onClick={close}>关闭</button></div><fieldset disabled={busy} className="repository-fields editor-fields"><div className="form-grid"><label>项目 ID<input required pattern="[A-Za-z0-9_\x2D]{1,64}" disabled={Boolean(existingId)} value={draft.id} onChange={event => update({id: event.target.value})}/></label><label>默认分支或 ref<input required value={draft.ref} onChange={event => update({ref: event.target.value})}/></label></div>{duplicate && <div className="notice warning" role="status">项目 ID {duplicate.id} 已注册。<button type="button" className="text-button" onClick={() => edit(duplicate.id)}>编辑已注册仓库</button></div>}<label>GitHub 仓库地址<input type="url" required value={draft.url} onChange={event => update({url: event.target.value})}/></label><div className="form-grid"><label>代码获取方式<select value={draft.mode} onChange={event => update({mode: event.target.value === 'managed' ? 'managed' : 'existing'})}><option value="managed">Agent 自动拉取</option><option value="existing">已有本地 checkout</option></select></label><label>目标目录{draft.mode === 'managed' ? '（可留空，使用托管目录）' : ''}<input required={draft.mode === 'existing'} value={draft.root} onChange={event => update({root: event.target.value})}/></label></div><label>测试地址<input type="url" required value={draft.appUrl} onChange={event => update({appUrl: event.target.value})}/></label><div className="form-grid"><label>允许修改的文件（每行一项）<textarea required value={draft.allowedFiles} onChange={event => update({allowedFiles: event.target.value})}/></label><label>允许访问的来源（每行一个 origin）<textarea required value={draft.allowedUrls} onChange={event => update({allowedUrls: event.target.value})}/></label></div><label>准备与验证命令（JSON argv）<textarea className="repository-commands" required spellCheck={false} value={draft.commands} onChange={event => update({commands: event.target.value})}/><small>prepare 可选；start、reset、static、unit、build 必填。prepare 示例：["node", "--version"]；依赖需预装在镜像中。</small></label><label>凭据引用（可选）<input value={draft.credential} onChange={event => update({credential: event.target.value})} placeholder="部署凭据引用，不填写 token" autoComplete="off"/></label><label className="knowledge-toggle"><input type="checkbox" checked={draft.enabled} onChange={event => update({enabled: event.target.checked})}/>启用仓库</label><label className="knowledge-toggle"><input type="checkbox" checked={draft.publish} onChange={event => update({publish: event.target.checked})}/>验证通过后发布修复分支到目标仓库</label><p className="muted">发布默认关闭；开启后需要服务端部署的 GitHub 写入凭据。</p><div className="editor-footer"><span className="muted">保存配置后，可管理检测规则，再启动 Test 或 Repair。</span><button className="primary" disabled={Boolean(duplicate)}>{busy ? '保存中…' : '保存仓库配置'}</button></div></fieldset></form></section>}
  </>;
}
