import {FormEvent, useCallback, useEffect, useRef, useState} from 'react';
import {DocumentDraft, KnowledgeDocument, loadDocument, loadDocuments, Project, saveDocument, searchDocuments, SearchHit} from '@tracefix/api-client';
import {timeLabel} from '@tracefix/runs-ui';
import {errorWithContext} from '@tracefix/api-client/errors';

const kinds = {repair: '修复方法', testing: '测试方法', experience: '经验记录'};
interface Props {projectId: string; projects: Project[]; documentId: string; documentRequest: number; initialDraft: DocumentDraft | null; onDirty: (dirty: boolean) => void; onSaved: () => void; onRun: (id: string) => void; report: (error: unknown) => void}
export function KnowledgePage({projectId, projects, documentId, documentRequest, initialDraft, onDirty, onSaved, onRun, report}: Props) {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]), [query, setQuery] = useState(''), [kind, setKind] = useState('');
  const [draft, setDraft] = useState<DocumentDraft | null>(initialDraft), [dirty, setDirty] = useState(Boolean(initialDraft)), [busy, setBusy] = useState(false), [notice, setNotice] = useState('');
  const [retrievalQuery, setRetrievalQuery] = useState(''), [hits, setHits] = useState<SearchHit[] | null>(null);
  const upload = useRef<HTMLInputElement>(null), dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const refresh = useCallback(async () => setDocuments(await loadDocuments(projectId)), [projectId]);
  useEffect(() => {refresh().catch(report);}, [refresh, report]);
  useEffect(() => {onDirty(dirty);}, [dirty, onDirty]);
  useEffect(() => {
    const unload = (event: BeforeUnloadEvent) => {if (dirtyRef.current) {event.preventDefault(); event.returnValue = '';}};
    window.addEventListener('beforeunload', unload); return () => window.removeEventListener('beforeunload', unload);
  }, []);
  useEffect(() => {
    if (!documentId) return;
    let active = true;
    if (dirtyRef.current && !window.confirm('文档尚未保存，确认打开另一篇文档？')) return;
    loadDocument(documentId).then(document => {if (active) {setDraft(document); setDirty(false);}}).catch(report);
    return () => {active = false;};
  }, [documentId, documentRequest, report]);
  function canReplace() {return !dirty || window.confirm('文档尚未保存，确认放弃当前更改？');}
  function createDraft(content = '', title = ''): DocumentDraft {return {title, content, projectId: projectId || null, kind: 'experience', enabled: true, tags: [], version: 0, sourceRunId: null};}
  async function open(id: string) {
    if (!canReplace()) return;
    setBusy(true);
    try {setDraft(await loadDocument(id)); setDirty(false); setNotice('');} catch (error) {report(error);} finally {setBusy(false);}
  }
  function update(fields: Partial<DocumentDraft>) {setDraft(previous => previous ? {...previous, ...fields} : previous); setDirty(true); setNotice('');}
  async function save(event: FormEvent) {
    event.preventDefault(); if (!draft || busy) return;
    setBusy(true);
    try {const saved = await saveDocument({...draft, tags: draft.tags.map(tag => tag.trim()).filter(Boolean)}); setDraft(saved); setDirty(false); setNotice(`已保存 v${saved.version}，${saved.enabled ? '后续检索立即可用' : '当前未参与 Agent 检索'}。`); await refresh(); onSaved();}
    catch (error) {report(error);} finally {setBusy(false);}
  }
  async function importFile(file?: File) {
    if (!file || !canReplace()) return;
    try {
      if (!/\.(md|markdown|txt)$/i.test(file.name) || file.size > 262144) throw new Error('支持 .md、.markdown、.txt，单个文件最大 256 KiB');
      let content: string;
      try {content = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer());}
      catch (error) {throw errorWithContext('文件读取失败，请确认文件为 UTF-8 编码且可读取', error);}
      if (!content.trim() || content.includes('\0')) throw new Error('请上传非空 UTF-8 文本文件');
      setDraft(createDraft(content, file.name.replace(/\.[^.]+$/, '').slice(0, 120))); setDirty(true); setNotice('文件已读入编辑器；确认内容后保存到知识库。');
    } catch (error) {report(error);}
  }
  async function retrieve(event: FormEvent) {
    event.preventDefault(); setBusy(true);
    try {setHits(await searchDocuments(projectId, retrievalQuery));} catch (error) {report(error);} finally {setBusy(false);}
  }
  const filtered = documents.filter(document => (!kind || document.kind === kind) && (document.title + document.preview + document.tags.join(' ')).toLowerCase().includes(query.toLowerCase()));
  return <>
    <section className="card knowledge-library"><div className="section-heading"><div><h2>文档资料库 <span className="count">{documents.length}</span></h2><p>当前项目 + 全局共享 · Markdown / 纯文本</p></div><div className="button-row"><input type="file" ref={upload} hidden accept=".md,.markdown,.txt" onChange={event => {importFile(event.target.files?.[0]); event.target.value = '';}}/><button className="secondary" disabled={busy} onClick={() => upload.current?.click()}>↑ 上传</button><button className="primary" disabled={busy} onClick={() => {if (canReplace()) {setDraft(createDraft()); setDirty(false); setNotice('');}}}>＋ 新建文档</button></div></div>
      <div className="toolbar"><input aria-label="搜索知识文档" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题、摘要或标签…"/><select aria-label="知识类型" value={kind} onChange={event => setKind(event.target.value)}><option value="">全部类型</option>{Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button className="text-button" disabled={busy} onClick={() => refresh().catch(report)}>刷新</button></div>
      {filtered.length ? <div className="document-grid">{filtered.map(document => <button key={document.id} className={'document-card ' + (draft?.id === document.id ? 'selected' : '')} onClick={() => open(document.id)} disabled={busy} aria-pressed={draft?.id === document.id}><div><span className="document-icon" aria-hidden="true">▤</span><span className={'badge ' + (document.enabled ? 'success' : '')}>{document.enabled ? '参与检索' : '已停用'}</span></div><h3>{document.title}</h3><p>{document.preview}</p><div className="tags"><span>{kinds[document.kind]}</span><span>{document.projectId || '全局'}</span>{document.tags.slice(0, 3).map(tag => <span key={tag}>#{tag}</span>)}</div><footer>v{document.version} · {timeLabel(document.updatedAt)}</footer></button>)}</div> : <div className="empty-state"><span aria-hidden="true">▤</span><h3>{documents.length ? '没有匹配的文档' : '把第一次经验留在这里'}</h3><p>上传测试方法，或从运行详情创建经验文档。保存并启用后，Agent 即可按需检索。</p></div>}
    </section>
    {draft && <section className="card document-editor"><form onSubmit={save}><div className="section-heading"><div><span className="eyebrow">DOCUMENT EDITOR</span><h2>{draft.id ? '编辑文档' : '新建文档'} {dirty && <span className="unsaved">未保存</span>}</h2></div><button type="button" className="text-button" onClick={() => {if (canReplace()) {setDraft(null); setDirty(false);}}}>关闭</button></div>
      {notice && <div className="notice" role="status">{notice}</div>}<div className="editor-fields"><label>文档标题<input required maxLength={120} value={draft.title} onChange={event => update({title: event.target.value})} placeholder="例如：刷新后状态丢失的排查方法"/></label><div className="form-grid"><label>文档类型<select value={draft.kind} onChange={event => update({kind: event.target.value as DocumentDraft['kind']})}>{Object.entries(kinds).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label>可检索范围<select disabled={Boolean(draft.sourceRunId)} value={draft.projectId || ''} onChange={event => update({projectId: event.target.value || null})}><option value="">全局共享</option>{projects.map(project => <option value={project.id} key={project.id}>{project.id}</option>)}</select></label></div><label>标签（英文逗号分隔）<input value={draft.tags.join(',')} onChange={event => update({tags: event.target.value.split(',')})} placeholder="React,持久化,回归测试"/></label><label>文档内容 <span className="muted">支持 Markdown 源文编辑 · 最大 256 KiB</span><textarea className="document-content" required value={draft.content} onChange={event => update({content: event.target.value})} placeholder={'## 适用场景\n\n## 问题定位\n\n## 修复或测试步骤\n\n## 验证与注意事项'}/></label>
      <div className="editor-footer"><label className="knowledge-toggle"><input type="checkbox" checked={draft.enabled} onChange={event => update({enabled: event.target.checked})}/>允许 Agent 检索此文档</label><button className="primary" disabled={busy || !draft.title.trim() || !draft.content.trim()}>{busy ? '保存中…' : '保存文档'}</button></div>{draft.sourceRunId && <button type="button" className="text-button" onClick={() => onRun(draft.sourceRunId!)}>查看来源运行 →</button>}</div></form></section>}
    <section className="card retrieval-preview"><div className="section-heading"><div><h2>检索预览</h2><p>使用与 Agent 相同的项目过滤和中英文关键词检索。分数表示匹配程度。</p></div><span className="badge">RAG CONTEXT</span></div><form className="toolbar" onSubmit={retrieve}><input aria-label="检索测试问题" value={retrievalQuery} onChange={event => setRetrievalQuery(event.target.value)} placeholder="输入问题，例如：刷新之后勾选状态丢失" maxLength={2000}/><button className="primary" disabled={busy || !projectId || !retrievalQuery.trim()}>检索</button></form>{hits !== null && <div className="search-results">{hits.length ? hits.map(hit => <article key={hit.id}><button className="text-button" onClick={() => open(hit.id)}>{hit.title} ↗</button><small>{hit.projectId || '全局'} · v{hit.version} · 匹配分 {hit.score}</small><pre>{hit.excerpt}</pre></article>) : <p className="muted">未找到相关且已启用的文档。试试文档中出现的关键词。</p>}</div>}</section>
  </>;
}
