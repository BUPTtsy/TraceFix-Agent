import {useCallback, useEffect, useState} from 'react';
import {AgentStatus, DocumentDraft, loadAgentStatus, loadProjects, loadRuns, Project, Run} from '@tracefix/api-client';
import {AgentDock} from '@tracefix/agent-ui';
import {KnowledgePage} from '@tracefix/knowledge-ui';
import {RulesPage} from '@tracefix/rules-ui';
import {RunList, RunsPage, statusLabel} from '@tracefix/runs-ui';
import {outcomeLabel, serviceLabel} from '@tracefix/presentation';
import './style.css';

type Page = 'overview' | 'runs' | 'projects' | 'knowledge' | 'rules';
const pages: {id: Page; label: string; icon: string; subtitle: string}[] = [
  {id: 'overview', label: '总览', icon: '◈', subtitle: '从一个目标开始，让每次验证与修复都有迹可循。'},
  {id: 'runs', label: '运行记录', icon: '⌁', subtitle: '查看真实的执行阶段、结果、日志与候选补丁。'},
  {id: 'projects', label: '项目空间', icon: '▱', subtitle: '管理目标项目的运行入口，连接项目、运行与经验。'},
  {id: 'knowledge', label: '知识库', icon: '▤', subtitle: '把修复方法和测试经验写成文档，让 Agent 在需要时找到它们。'},
  {id: 'rules', label: '检测规则', icon: '◇', subtitle: '管理确定性检查、引导规则与 Run 级规则快照。'},
];
function currentPage(): Page {return pages.find(page => '#' + page.id === window.location.hash)?.id || 'overview';}

export default function App() {
  const [page, setPage] = useState<Page>(currentPage), [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState(''), [runs, setRuns] = useState<Run[]>([]), [agent, setAgent] = useState<AgentStatus>({running: false});
  const [connected, setConnected] = useState(false), [error, setError] = useState(''), [syncedAt, setSyncedAt] = useState('');
  const [selectedRun, setSelectedRun] = useState(''), [documentId, setDocumentId] = useState(''), [documentDraft, setDocumentDraft] = useState<DocumentDraft | null>(null), [dirty, setDirty] = useState(false);
  const [documentRequest, setDocumentRequest] = useState(0);
  const report = useCallback((error: unknown) => setError(error instanceof Error ? error.message : String(error)), []);
  const refreshProjects = useCallback(async () => {
    const records = await loadProjects(); setProjects(records);
    setProjectId(previous => records.some(project => project.id === previous) ? previous : records[0]?.id || '');
  }, []);
  useEffect(() => {refreshProjects().catch(report); const timer = window.setInterval(() => refreshProjects().catch(report), 15000); return () => window.clearInterval(timer);}, [refreshProjects, report]);
  useEffect(() => {
    let active = true, timer: number;
    async function poll() {
      try {
        const [records, status] = await Promise.all([loadRuns(), loadAgentStatus()]);
        if (active) {setRuns(records); setAgent(status); setConnected(true); setSyncedAt(new Date().toLocaleTimeString('zh-CN'));}
      } catch (error) {if (active) {setConnected(false); report(error);}}
      finally {if (active) timer = window.setTimeout(poll, 2500);}
    }
    poll(); return () => {active = false; window.clearTimeout(timer);};
  }, [report]);
  useEffect(() => {
    const change = () => {
      if (dirty && !window.confirm('文档尚未保存，确认离开并放弃更改？')) {window.history.replaceState(null, '', '#' + page); return;}
      setDirty(false); setPage(currentPage());
    };
    window.addEventListener('hashchange', change); return () => window.removeEventListener('hashchange', change);
  }, [dirty, page]);
  function navigate(next: Page) {window.location.hash = next;}
  function selectProject(id: string) {
    if (id === projectId) return true;
    if (dirty && !window.confirm('切换项目会放弃未保存的文档，确认继续？')) return false;
    setDirty(false); setProjectId(id); setDocumentId(''); setDocumentDraft(null); setSelectedRun('');
    return true;
  }
  function openRun(run: Run) {if (selectProject(run.projectId)) {setSelectedRun(run.id); navigate('runs');}}
  function openDocument(id: string) {setDocumentDraft(null); setDocumentId(id); setDocumentRequest(previous => previous + 1); navigate('knowledge');}
  function captureExperience(run: Run) {
    setDocumentId(''); setDocumentDraft({title: run.goal.slice(0, 120), projectId: run.projectId, kind: 'experience', tags: [], enabled: false, version: 0, sourceRunId: run.id,
      content: `# ${run.goal}\n\n## 来源\nRun: ${run.agentRunId || run.id}\n服务: ${serviceLabel(run.mode)}\n结果: ${run.outcome ? outcomeLabel(run.outcome) : statusLabel(run.status)}\n\n## 复现与定位\n\n请补充可复用的步骤、原因及证据。\n\n## 修复方法 / 测试方法\n\n## 适用条件与局限\n`});
    navigate('knowledge');
  }
  const project = projects.find(item => item.id === projectId), scopedRuns = runs.filter(run => run.projectId === projectId);
  const heading = pages.find(item => item.id === page)!;
  return <div className="app-shell">
    <aside className="sidebar">
      <a href="#overview" className="brand"><span className="brand-mark">✦</span>trace<span>fix</span></a>
      <div className="workspace-label"><span className="workspace-avatar">T</span><div><strong>TraceFix workspace</strong><small>Agent 控制台</small></div></div>
      <p className="nav-label">工作台</p><nav aria-label="工作台导航">{pages.map(item => <a href={'#' + item.id} key={item.id} className={page === item.id ? 'active' : ''} aria-current={page === item.id ? 'page' : undefined}><span>{item.icon}</span>{item.label}</a>)}</nav>
      <p className="nav-label">已注册项目 <span>{projects.length}</span></p><div className="sidebar-projects">{projects.map(item => <button className={projectId === item.id ? 'active' : ''} key={item.id} onClick={() => {if (selectProject(item.id)) navigate('projects');}}><span className={'project-dot ' + (item.ready ? 'ready' : '')}/>{item.id}</button>)}{!projects.length && <small>暂无已注册项目</small>}</div>
      <div className="sidebar-bottom"><span>LOCAL WORKSPACE</span><p>控制台与目标仓库独立运行</p><small>TraceFix · v0.1.1</small></div>
    </aside>
    <main className="main-content">
      <header className="topbar"><div>工作台 <span>/</span> <strong>{heading.label}</strong></div><span className={'connection ' + (connected ? 'online' : '')}><i/>{connected ? 'API 已连接' : 'API 连接中断或连接中'}</span></header>
      <div className="page-heading"><div><p className="kicker">YOUR AGENT WORKSPACE</p><h1>{heading.label}<span> /</span></h1><p>{heading.subtitle}</p></div><label className="project-picker">当前项目<select aria-label="当前项目" value={projectId} onChange={event => selectProject(event.target.value)}><option value="" disabled>选择项目</option>{projects.map(item => <option key={item.id} value={item.id}>{item.id}</option>)}</select></label></div>
      {error && <div className="error-banner" role="alert"><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError('')}>×</button></div>}
      {!connected && <div className="notice">正在尝试连接后端。已有数据可能已过期，连接恢复后会自动刷新。</div>}
      {page === 'overview' && <>
        <section className="metric-grid"><article className="card metric"><span>项目运行总数</span><strong>{scopedRuns.length}</strong><small>来自持久化运行记录</small></article><article className="card metric"><span>已验证修复</span><strong>{scopedRuns.filter(run => run.outcome === 'FIX_VERIFIED').length}</strong><small>仅统计 FIX_VERIFIED 结果</small></article><article className="card metric"><span>知识文档</span><strong>{project?.documentCount ?? 0}</strong><small>本项目与全局文档，含停用草稿</small></article></section>
        <section className="card featured"><div><span className="eyebrow">CONNECTED WORKSPACE</span><h2>{project?.id || '先连接一个目标项目'}</h2><p>{project ? project.root : '在 profiles/projects.yaml 中注册仓库，并添加对应 Profile。'}</p><span className={'badge ' + (project?.ready ? 'success' : 'warning')}>{project?.ready ? '项目配置就绪' : project?.issue || '尚未配置'}</span></div><button className="primary" onClick={() => navigate('projects')}>查看项目空间 ↗</button></section>
        <section className="card"><div className="section-heading"><div><h2>最近运行</h2><p>执行状态与修复结论分别记录</p></div><button className="text-button" onClick={() => navigate('runs')}>全部记录 →</button></div><RunList runs={scopedRuns.slice(0, 5)} onSelect={openRun}/></section>
        <div className="journey"><button onClick={() => navigate('projects')}><b>01</b><strong>选择目标项目</strong><span>确认仓库与运行配置</span></button><div>→</div><button onClick={() => navigate('runs')}><b>02</b><strong>检查运行结果</strong><span>从日志追溯到补丁</span></button><div>→</div><button onClick={() => navigate('knowledge')}><b>03</b><strong>沉淀为知识</strong><span>积累可检索的经验</span></button></div>
      </>}
      {page === 'runs' && <RunsPage runs={scopedRuns} selectedId={selectedRun} onSelect={setSelectedRun} onDocument={openDocument} onCapture={captureExperience} report={report}/>}
      {page === 'projects' && <><div className="project-grid">{projects.map(item => <button key={item.id} className={'card project-card ' + (projectId === item.id ? 'selected' : '')} onClick={() => selectProject(item.id)}><div><span className="project-avatar">{item.id.slice(0, 1).toUpperCase()}</span><span className={'badge ' + (item.ready ? 'success' : 'warning')}>{item.ready ? '配置就绪' : '待配置'}</span></div><h2>{item.id}</h2><p>{item.repoId}</p><code>{item.root}</code><footer><span>{item.runCount} 次运行</span><span>{item.documentCount} 篇文档</span></footer></button>)}</div>{project && <section className="card project-detail"><div className="section-heading"><div><h2>{project.id} 的运行配置</h2><p>读取已注册仓库及其实际 Profile</p></div><span className="badge">{project.repoId}</span></div>{project.issue && <div className="notice">{project.issue}</div>}<dl><dt>目标目录</dt><dd><code>{project.root}</code></dd><dt>Profile</dt><dd><code>{project.profile || '缺失'}</code></dd><dt>测试地址</dt><dd>{project.url || '未配置'}</dd><dt>允许修改</dt><dd>{project.allowedFiles.join(' · ')}</dd></dl><div className="command-list">{Object.entries(project.commands).map(([name, command]) => <div key={name}><span>{name}</span><code>{command.join(' ')}</code></div>)}</div><div className="button-row"><button className="primary" onClick={() => navigate('runs')}>查看项目运行</button><button className="secondary" onClick={() => navigate('knowledge')}>查看项目知识</button></div></section>}<div className="notice">添加项目：在 profiles/projects.yaml 注册独立仓库，在同目录添加 project 字段匹配的 Profile。控制台自动同步配置；右侧 Test／Repair 使用当前项目启动 Agent。</div></>}
      {page === 'knowledge' && <KnowledgePage key={projectId} projectId={projectId} projects={projects} documentId={documentId} documentRequest={documentRequest} initialDraft={documentDraft} onDirty={setDirty} onSaved={() => {setDocumentDraft(null); refreshProjects().catch(report);}} onRun={id => {setSelectedRun(id); navigate('runs');}} report={report}/>}
      {page === 'rules' && <RulesPage key={projectId} projectId={projectId} report={report}/>}
      <footer className="page-footer"><span>TRACEFIX · Evidence before confidence</span><span>{syncedAt ? `上次同步 ${syncedAt}` : '等待首次同步'}{agent.running && ` · ${statusLabel(agent.status || 'running')}`}</span></footer>
    </main>
    <AgentDock project={project} agent={agent} onStarted={status => {setAgent(status); if (status.id) setSelectedRun(status.id);}} onRun={() => {if (agent.projectId && !selectProject(agent.projectId)) return; if (agent.id) setSelectedRun(agent.id); navigate('runs');}} onDocument={openDocument} report={report}/>
  </div>;
}
