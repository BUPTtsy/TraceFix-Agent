import {FormEvent, useEffect, useRef, useState} from 'react';
import {loadRepository, Project, RepositoryDraft, RepositoryProfile, saveRepository} from '@tracefix/api-client';

interface Props {projects: Project[]; projectId: string; onSelect: (id: string) => boolean; onSaved: (id: string) => Promise<void>; onDirty: (dirty: boolean) => void; onNavigate: (page: 'runs' | 'rules') => void; report: (error: unknown) => void}
interface Draft {id: string; url: string; ref: string; root: string; mode: RepositoryDraft['checkout_mode']; commands: string; allowedFiles: string; allowedUrls: string; appUrl: string; credential: string; enabled: boolean; publish: boolean; profile: RepositoryProfile | null}
const defaultCommands = {prepare: ['node', '--version'], start: ['node', 'scripts/serve.mjs'], reset: ['node', 'scripts/reset.mjs'], static: ['node', 'node_modules/typescript/bin/tsc', '--noEmit'], unit: ['node', '--test', 'tests/tasks.test.mjs'], build: ['node', 'scripts/build.mjs']};
function blank(): Draft {return {id: 'test-bug-repo', url: 'https://github.com/BUPTtsy/Test-Bug-repo', ref: 'main', root: '', mode: 'managed', commands: JSON.stringify(defaultCommands, null, 2), allowedFiles: 'src/**\nserver/**', allowedUrls: 'http://app:3000', appUrl: 'http://app:3000', credential: '', enabled: true, publish: false, profile: null};}
function lines(text: string): string[] {return text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);}
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
  const requestVersion = useRef(0), changed = useRef(false), saving = useRef(false);
  const project = projects.find(item => item.id === projectId);
  useEffect(() => {requestVersion.current += 1; setDraft(null); setExistingId(''); setLoading(false); setFailure(''); changed.current = false; onDirty(false);}, [projectId, onDirty]);
  useEffect(() => () => {requestVersion.current += 1;}, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {if (changed.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  function update(fields: Partial<Draft>) {setDraft(previous => previous ? {...previous, ...fields} : previous); changed.current = true; onDirty(true); setFailure(''); setNotice('');}
  function close() {if (changed.current && !window.confirm('仓库配置尚未保存，确认放弃更改？')) return; requestVersion.current += 1; setDraft(null); setLoading(false); changed.current = false; onDirty(false);}
  function create() {if (changed.current && !window.confirm('仓库配置尚未保存，确认放弃更改？')) return; requestVersion.current += 1; setDraft(blank()); setExistingId(''); setLoading(false); setNotice(''); setFailure(''); changed.current = true; onDirty(true);}
  async function edit() {
    if (!project || loading || busy) return;
    if (changed.current && !window.confirm('仓库配置尚未保存，确认重新读取？')) return;
    const version = ++requestVersion.current;
    setLoading(true); setFailure('');
    try {
      const record = await loadRepository(project.id);
      if (version !== requestVersion.current) return;
      setExistingId(project.id); setDraft({id: record.repo_id, url: `https://github.com/${record.owner}/${record.repo}`, ref: record.default_ref, root: record.root || '', mode: record.checkout_mode || 'existing', commands: JSON.stringify(record.profile.commands, null, 2), allowedFiles: record.allowed_files.join('\n'), allowedUrls: record.allowed_urls.join('\n'), appUrl: record.profile.url, credential: record.credential_ref || '', enabled: record.enabled, publish: record.publish_branch === true, profile: record.profile});
      changed.current = false; onDirty(false); setNotice('');
    } catch (error) {if (version === requestVersion.current) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}}
    finally {if (version === requestVersion.current) setLoading(false);}
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || saving.current) return;
    saving.current = true; setBusy(true); setFailure(''); setNotice('');
    try {
      const url = new URL(draft.url);
      const match = url.pathname.match(/^\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/);
      if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash || !match) throw new Error('仓库地址必须是 https://github.com/owner/repo');
      const allowedUrls = lines(draft.allowedUrls);
      const repository: RepositoryDraft = {repo_id: draft.id.trim(), owner: match[1], repo: match[2], default_ref: draft.ref.trim(), checkout_mode: draft.mode, publish_branch: draft.publish, allowed_files: lines(draft.allowedFiles), allowed_urls: allowedUrls, credential_ref: draft.credential.trim() || null, enabled: draft.enabled, profile: {...draft.profile, project: draft.id.trim(), url: draft.appUrl.trim(), allowed_origins: allowedUrls, commands: commandsFrom(draft.commands)}};
      if (draft.root.trim()) repository.root = draft.root.trim();
      if (draft.mode === 'existing' && !repository.root) throw new Error('使用已有 checkout 时必须填写目标目录');
      await saveRepository(repository, existingId || undefined);
      changed.current = false; onDirty(false); setDraft(null); setNotice('仓库配置已保存。启动任务时，Agent 将准备工作区并运行命令。');
      await onSaved(repository.repo_id);
    } catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}
    finally {saving.current = false; setBusy(false);}
  }
  return <>
    <section className="card"><div className="section-heading"><div><h2>仓库接入</h2><p>配置 GitHub 来源、工作区准备和验证命令，让 Agent 自行获取目标代码。</p></div><button className="primary" disabled={busy} onClick={create}>＋ 注册仓库</button></div></section>
    {notice && <div className="notice" role="status">{notice}</div>}
    <div className="project-grid">{projects.map(item => <button key={item.id} className={'card project-card ' + (projectId === item.id ? 'selected' : '')} disabled={busy} onClick={() => onSelect(item.id)} aria-pressed={projectId === item.id}><div><span className="project-avatar" aria-hidden="true">{item.id.slice(0, 1).toUpperCase()}</span><span className={'badge ' + (item.ready ? 'success' : 'warning')}>{item.ready ? '配置就绪' : '待配置'}</span></div><h2>{item.id}</h2><p>{item.repoId}</p><code>{item.root}</code><footer><span>{item.runCount} 次运行</span><span>{item.documentCount} 篇文档</span></footer></button>)}</div>
    {!projects.length && !draft && <div className="notice">注册远程仓库后即可配置检测规则，并从右侧启动 Test 或 Repair。</div>}
    {project && <section className="card project-detail"><div className="section-heading"><div><h2>{project.id} 的运行配置</h2><p>仓库与 Profile 持久化保存，运行启动时冻结配置。</p></div><button className="secondary" disabled={busy || loading} onClick={edit}>{loading ? '读取中…' : '编辑仓库配置'}</button></div>{project.issue && <div className="notice">{project.issue}</div>}<dl><dt>GitHub 仓库</dt><dd>{project.repoId}</dd><dt>目标目录</dt><dd><code>{project.root}</code></dd><dt>测试地址</dt><dd>{project.url || '未配置'}</dd><dt>允许修改</dt><dd>{project.allowedFiles.join(' · ')}</dd></dl><div className="command-list">{Object.entries(project.commands).map(([name, argv]) => <div key={name}><span>{name}</span><code>{JSON.stringify(argv)}</code></div>)}</div><div className="button-row"><button className="secondary" onClick={() => onNavigate('rules')}>管理检测规则</button><button className="primary" onClick={() => onNavigate('runs')}>查看运行与补丁</button></div></section>}
    {failure && <div className="notice failure" role="alert">{failure}</div>}
    {draft && <section className="card repository-editor"><form onSubmit={save}><div className="section-heading"><div><span className="eyebrow">REPOSITORY CONFIGURATION</span><h2>{existingId ? '编辑仓库' : '注册远程仓库'}</h2><p>准备和验证命令以 argv 数组运行，保存后由 Agent 执行。</p></div><button type="button" className="text-button" disabled={busy} onClick={close}>关闭</button></div><fieldset disabled={busy} className="repository-fields editor-fields"><div className="form-grid"><label>项目 ID<input required pattern="[A-Za-z0-9_-]{1,64}" disabled={Boolean(existingId)} value={draft.id} onChange={event => update({id: event.target.value})}/></label><label>默认分支或 ref<input required value={draft.ref} onChange={event => update({ref: event.target.value})}/></label></div><label>GitHub 仓库地址<input type="url" required value={draft.url} onChange={event => update({url: event.target.value})}/></label><div className="form-grid"><label>代码获取方式<select value={draft.mode} onChange={event => update({mode: event.target.value === 'managed' ? 'managed' : 'existing'})}><option value="managed">Agent 自动拉取</option><option value="existing">已有本地 checkout</option></select></label><label>目标目录{draft.mode === 'managed' ? '（可留空，使用托管目录）' : ''}<input required={draft.mode === 'existing'} value={draft.root} onChange={event => update({root: event.target.value})}/></label></div><label>测试地址<input type="url" required value={draft.appUrl} onChange={event => update({appUrl: event.target.value})}/></label><div className="form-grid"><label>允许修改的文件（每行一项）<textarea required value={draft.allowedFiles} onChange={event => update({allowedFiles: event.target.value})}/></label><label>允许访问的来源（每行一个 origin）<textarea required value={draft.allowedUrls} onChange={event => update({allowedUrls: event.target.value})}/></label></div><label>准备与验证命令（JSON argv）<textarea className="repository-commands" required spellCheck={false} value={draft.commands} onChange={event => update({commands: event.target.value})}/><small>prepare 可选；start、reset、static、unit、build 必填。prepare 示例：["node", "--version"]；依赖需预装在镜像中。</small></label><label>凭据引用（可选）<input value={draft.credential} onChange={event => update({credential: event.target.value})} placeholder="部署凭据引用，不填写 token" autoComplete="off"/></label><label className="knowledge-toggle"><input type="checkbox" checked={draft.enabled} onChange={event => update({enabled: event.target.checked})}/>启用仓库</label><label className="knowledge-toggle"><input type="checkbox" checked={draft.publish} onChange={event => update({publish: event.target.checked})}/>验证通过后发布修复分支到目标仓库</label><p className="muted">发布默认关闭；开启后需要服务端部署的 GitHub 写入凭据。</p><div className="editor-footer"><span className="muted">保存配置后，可管理检测规则，再启动 Test 或 Repair。</span><button className="primary">{busy ? '保存中…' : '保存仓库配置'}</button></div></fieldset></form></section>}
  </>;
}
