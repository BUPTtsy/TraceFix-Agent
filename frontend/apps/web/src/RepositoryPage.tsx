import {FormEvent, useCallback, useEffect, useRef, useState} from 'react';
import {ApiError, loadRepository, Project, Repository, RepositoryDraft, RepositoryProfile, saveRepository} from '@tracefix/api-client';

interface Props {projects: Project[]; editId: string; onSaved: (id: string) => Promise<void>; onDirty: (dirty: boolean) => void; onNavigate: (hash: string, message?: string) => void; report: (error: unknown) => void}
interface Draft {id: string; url: string; ref: string; root: string; mode: RepositoryDraft['checkout_mode']; commands: string; allowedFiles: string; allowedUrls: string; appUrl: string; credential: string; enabled: boolean; publish: boolean; approval: boolean; profile: RepositoryProfile | null}
const defaultCommands = {prepare: ['node', '--version'], start: ['node', 'scripts/serve.mjs'], reset: ['node', 'scripts/reset.mjs'], static: ['node', 'node_modules/typescript/bin/tsc', '--noEmit'], unit: ['node', '--test', 'tests/tasks.test.mjs'], build: ['node', 'scripts/build.mjs']};
function blank(projects: Project[]): Draft {
  let id = 'test-bug-repo', suffix = 2;
  while (projects.some(project => project.id.toLowerCase() === id)) id = 'test-bug-repo-' + suffix++;
  return {id, url: 'https://github.com/BUPTtsy/Test-Bug-repo', ref: 'main', root: '', mode: 'managed', commands: JSON.stringify(defaultCommands, null, 2), allowedFiles: 'src/**\nserver/**', allowedUrls: 'http://app:3000', appUrl: 'http://app:3000', credential: '', enabled: true, publish: false, approval: false, profile: null};
}
function fromRecord(record: Repository): Draft {
  return {id: record.repo_id, url: `https://github.com/${record.owner}/${record.repo}`, ref: record.default_ref, root: record.root || '', mode: record.checkout_mode || 'existing', commands: JSON.stringify(record.profile.commands, null, 2), allowedFiles: record.allowed_files.join('\n'), allowedUrls: record.allowed_urls.join('\n'), appUrl: record.profile.url, credential: record.credential_ref || '', enabled: record.enabled, publish: record.publish_branch === true, approval: record.require_approval === true, profile: record.profile};
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

// 仓库注册 / 编辑页：editId 为空时注册新仓库，否则读取并编辑已有仓库。
export function RepositoryPage({projects, editId, onSaved, onDirty, onNavigate, report}: Props) {
  const [draft, setDraft] = useState<Draft | null>(() => editId ? null : blank(projects));
  const [loading, setLoading] = useState(Boolean(editId)), [busy, setBusy] = useState(false), [failure, setFailure] = useState('');
  const requestVersion = useRef(0), changed = useRef(false), saving = useRef(false);
  useEffect(() => {onDirty(changed.current || busy);}, [draft, busy, onDirty]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {if (changed.current || saving.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, []);
  // 新注册时，项目列表刷新后重新生成不冲突的默认 ID（用户尚未修改时）。
  useEffect(() => {if (!editId && !changed.current) setDraft(blank(projects));}, [editId, projects]);
  const load = useCallback(async () => {
    if (!editId) return;
    const version = ++requestVersion.current;
    setLoading(true); setFailure('');
    try {
      const record = await loadRepository(editId);
      if (version !== requestVersion.current) return;
      setDraft(fromRecord(record)); changed.current = false; onDirty(false);
    } catch (error) {if (version === requestVersion.current) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}}
    finally {if (version === requestVersion.current) setLoading(false);}
  }, [editId, onDirty, report]);
  useEffect(() => {load(); return () => {requestVersion.current += 1;};}, [load]);
  const duplicate = !editId && draft ? projects.find(item => item.id.toLowerCase() === draft.id.trim().toLowerCase()) : undefined;
  function update(fields: Partial<Draft>) {setDraft(previous => previous ? {...previous, ...fields} : previous); changed.current = true; onDirty(true); setFailure('');}
  function reload() {if (!changed.current || window.confirm('仓库配置尚未保存，确认重新读取？')) load();}
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || saving.current) return;
    saving.current = true; setBusy(true); setFailure('');
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
      const repository: RepositoryDraft = {repo_id: draft.id.trim(), owner: match[1], repo: match[2], default_ref: draft.ref.trim(), checkout_mode: draft.mode, publish_branch: draft.publish, require_approval: draft.approval, allowed_files: lines(draft.allowedFiles), allowed_urls: allowedUrls, credential_ref: draft.credential.trim() || null, enabled: draft.enabled, profile: {...draft.profile, project: draft.id.trim(), url: draft.appUrl.trim(), allowed_origins: allowedUrls, commands: commandsFrom(draft.commands)}};
      if (draft.root.trim()) repository.root = draft.root.trim();
      if (draft.mode === 'existing' && !repository.root) throw new Error('使用已有 checkout 时必须填写目标目录');
      const saved = await saveRepository(repository, editId || undefined);
      changed.current = false; saving.current = false; onDirty(false);
      let message = '仓库配置已保存。启动任务时，Agent 将准备工作区并运行命令。';
      try {await onSaved(saved.repo_id);}
      catch (error) {message = '仓库配置已保存，但列表刷新失败，请刷新页面读取最新配置。'; report(error);}
      onNavigate('projects', message);
    } catch (error) {setFailure(error instanceof ApiError && error.code === 'REPO_ID_CONFLICT' ? '项目 ID 已被注册，请读取已有配置后编辑。' : error instanceof Error ? error.message : String(error)); report(error);}
    finally {saving.current = false; setBusy(false);}
  }
  const back = <button type="button" className="text-button" disabled={busy} onClick={() => onNavigate('projects')}>返回项目空间</button>;
  if (!draft) return <section className="card repository-editor"><div className="section-heading"><div><span className="eyebrow">REPOSITORY CONFIGURATION</span><h2>编辑仓库 {editId}</h2><p>{loading ? '正在读取仓库配置…' : '仓库配置读取失败。'}</p></div>{back}</div>{failure && <div className="notice failure" role="alert">{failure}</div>}{!loading && <div className="button-row"><button className="secondary" onClick={reload}>重新读取</button></div>}</section>;
  return <section className="card repository-editor"><form onSubmit={save}>
    <div className="section-heading"><div><span className="eyebrow">REPOSITORY CONFIGURATION</span><h2>{editId ? `编辑仓库 ${editId}` : '注册远程仓库'}</h2><p>准备和验证命令以 argv 数组运行，保存后由 Agent 执行。</p></div><div className="button-row">{editId && <button type="button" className="secondary" disabled={busy || loading} onClick={reload}>{loading ? '读取中…' : '重新读取'}</button>}{back}</div></div>
    <fieldset disabled={busy || loading} className="repository-fields editor-fields">
      <div className="form-grid"><label>项目 ID<input required pattern="[A-Za-z0-9_\x2D]{1,64}" disabled={Boolean(editId)} value={draft.id} onChange={event => update({id: event.target.value})}/></label><label>默认分支或 ref<input required value={draft.ref} onChange={event => update({ref: event.target.value})}/></label></div>
      {duplicate && <div className="notice warning" role="status">项目 ID {duplicate.id} 已注册。<button type="button" className="text-button" onClick={() => onNavigate('repository/' + encodeURIComponent(duplicate.id))}>编辑已注册仓库</button></div>}
      <label>GitHub 仓库地址<input type="url" required value={draft.url} onChange={event => update({url: event.target.value})}/></label>
      <div className="form-grid"><label>代码获取方式<select value={draft.mode} onChange={event => update({mode: event.target.value === 'managed' ? 'managed' : 'existing'})}><option value="managed">Agent 自动拉取</option><option value="existing">已有本地 checkout</option></select></label><label>目标目录{draft.mode === 'managed' ? '（可留空，使用托管目录）' : ''}<input required={draft.mode === 'existing'} value={draft.root} onChange={event => update({root: event.target.value})}/></label></div>
      <label>测试地址<input type="url" required value={draft.appUrl} onChange={event => update({appUrl: event.target.value})}/></label>
      <div className="form-grid"><label>允许修改的文件（每行一项）<textarea required value={draft.allowedFiles} onChange={event => update({allowedFiles: event.target.value})}/></label><label>允许访问的来源（每行一个 origin）<textarea required value={draft.allowedUrls} onChange={event => update({allowedUrls: event.target.value})}/></label></div>
      <label>准备与验证命令（JSON argv）<textarea className="repository-commands" required spellCheck={false} value={draft.commands} onChange={event => update({commands: event.target.value})}/><small>prepare 可选；start、reset、static、unit、build 必填。prepare 示例：["node", "--version"]；依赖需预装在镜像中。</small></label>
      <label>凭据引用（可选）<input value={draft.credential} onChange={event => update({credential: event.target.value})} placeholder="部署凭据引用，不填写 token" autoComplete="off"/></label>
      <label className="knowledge-toggle"><input type="checkbox" checked={draft.enabled} onChange={event => update({enabled: event.target.checked})}/>启用仓库</label>
      <label className="knowledge-toggle"><input type="checkbox" checked={draft.publish} onChange={event => update({publish: event.target.checked})}/>验证通过后发布修复分支到目标仓库</label>
      <p className="muted">发布默认关闭；开启后需要服务端部署的 GitHub 写入凭据。</p>
      <label className="knowledge-toggle"><input type="checkbox" checked={draft.approval} onChange={event => update({approval: event.target.checked})}/>修复交付前需人工审批</label>
      <p className="muted">审批默认关闭；开启后修复通过最终验收会停在“等待审批”，需在运行详情或 CLI 中批准后才交付（推送分支或保留本地提交）。</p>
      {failure && <div className="notice failure" role="alert">{failure}</div>}
      <div className="editor-footer"><span className="muted">保存后可在项目空间进入“配置规则”页面勾选检测规则，再启动 Test 或 Repair。</span><button className="primary" disabled={Boolean(duplicate)}>{busy ? '保存中…' : '保存仓库配置'}</button></div>
    </fieldset>
  </form></section>;
}
