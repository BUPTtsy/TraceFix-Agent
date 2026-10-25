import {useState} from 'react';
import {deleteRepository, Project} from '@tracefix/api-client';

interface Props {projects: Project[]; projectId: string; onSelect: (id: string) => boolean; onRemoved: () => Promise<void>; onNavigate: (hash: string) => void; report: (error: unknown) => void}

// 项目空间：列出已注册仓库，并作为仓库注册、规则配置、运行记录等页面的入口。
export function ProjectsPage({projects, projectId, onSelect, onRemoved, onNavigate, report}: Props) {
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState(''), [failure, setFailure] = useState('');
  const project = projects.find(item => item.id === projectId);
  const target = (page: string) => page + '/' + encodeURIComponent(projectId);
  async function remove() {
    if (!project || busy || !window.confirm(`确认移除仓库 ${project.id} 的注册配置？本地代码目录会保留；有关联记录的仓库请改为停用。`)) return;
    setBusy(true); setFailure(''); setNotice('');
    try {
      await deleteRepository(project.id);
      setNotice('仓库注册已移除，本地代码目录已保留。');
      try {await onRemoved();} catch (error) {setNotice('仓库注册已移除，但列表刷新失败，请刷新页面。'); report(error);}
    } catch (error) {setFailure(error instanceof Error ? error.message : String(error)); report(error);}
    finally {setBusy(false);}
  }
  return <>
    <section className="card"><div className="section-heading"><div><h2>仓库接入</h2><p>配置 GitHub 来源、工作区准备和验证命令，让 Agent 自行获取目标代码。</p></div><button className="primary" disabled={busy} onClick={() => onNavigate('repository')}>＋ 注册仓库</button></div></section>
    {notice && <div className="notice" role="status">{notice}</div>}
    <div className="project-grid">{projects.map(item => <button key={item.id} className={'card project-card ' + (projectId === item.id ? 'selected' : '')} disabled={busy} onClick={() => onSelect(item.id)} aria-pressed={projectId === item.id}><div><span className="project-avatar" aria-hidden="true">{item.id.slice(0, 1).toUpperCase()}</span><span className={'badge ' + (item.ready ? 'success' : 'warning')}>{item.ready ? '配置就绪' : '待配置'}</span></div><h2>{item.id}</h2><p>{item.repoId}</p><code>{item.root}</code><footer><span>{item.runCount} 次运行</span></footer></button>)}</div>
    {!projects.length && <div className="notice">注册远程仓库后即可配置检测规则，并从右侧启动 Test 或 Repair。</div>}
    {project && <section className="card project-detail"><div className="section-heading"><div><h2>{project.id} 的运行配置</h2><p>仓库与 Profile 持久化保存，运行启动时冻结配置。</p></div><button className="secondary" disabled={busy} onClick={() => onNavigate(target('repository'))}>编辑仓库配置</button></div>{project.issue && <div className="notice">{project.issue}</div>}<dl><dt>GitHub 仓库</dt><dd>{project.repoId}</dd><dt>目标目录</dt><dd><code>{project.root}</code></dd><dt>测试地址</dt><dd>{project.url || '未配置'}</dd><dt>允许修改</dt><dd>{project.allowedFiles.join(' · ')}</dd></dl><div className="command-list">{Object.entries(project.commands).map(([name, argv]) => <div key={name}><span>{name}</span><code>{JSON.stringify(argv)}</code></div>)}</div><div className="button-row">{Boolean(project.repositoryConfigVersion) && <button className="text-button danger" disabled={busy} onClick={remove}>移除仓库注册</button>}<button className="secondary" disabled={busy} onClick={() => onNavigate(target('repository-rules'))}>配置规则</button><button className="secondary" onClick={() => onNavigate('rules')}>统一规则管理</button><button className="primary" onClick={() => onNavigate('runs')}>查看运行与补丁</button></div></section>}
    {failure && <div className="notice failure" role="alert">{failure}</div>}
  </>;
}
