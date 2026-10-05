import {FormEvent, useEffect, useRef, useState} from 'react';
import {AgentStatus, ChatMessage, loadServices, Project, Service, startAgent, stopAgent, streamChat} from '@tracefix/api-client';
import {statusLabel} from '@tracefix/runs-ui';
import {phaseLabel, serviceLabel} from '@tracefix/presentation';

interface Props {project?: Project; agent: AgentStatus; onStarted: (agent: AgentStatus) => void; onRun: () => void; onDocument: (id: string) => void; report: (error: unknown) => void}
export function AgentDock({project, agent, onStarted, onRun, onDocument, report}: Props) {
  const [services, setServices] = useState<{id: Service; label: string}[]>([]), [service, setService] = useState<Service>('repair');
  const [goal, setGoal] = useState(''), [busy, setBusy] = useState(false), [useKnowledge, setUseKnowledge] = useState(true);
  const [stopping, setStopping] = useState(false);
  const stopPending = useRef(false);
  const submitPending = useRef(false);
  const [messages, setMessages] = useState<Record<string, ChatMessage[]>>({}), [streamingProject, setStreamingProject] = useState('');
  const viewport = useRef<HTMLDivElement>(null), projectKey = project?.id || 'workspace';
  const conversation = messages[projectKey] || [];
  async function stop() {
    if (stopPending.current || !agent.canStop) return;
    stopPending.current = true; setStopping(true);
    try {onStarted(await stopAgent());}
    catch (error) {report(error);}
    finally {stopPending.current = false; setStopping(false);}
  }
  useEffect(() => {loadServices().then(setServices).catch(report);}, [report]);
  useEffect(() => {viewport.current?.scrollTo({top: viewport.current.scrollHeight});}, [conversation.length, conversation.at(-1)?.content, conversation.at(-1)?.reasoning]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitPending.current || !services.length || !goal.trim() || (service !== 'chat' && (agent.running || !project?.ready))) return;
    submitPending.current = true;
    const text = goal.trim(), key = projectKey, history = messages[key] || [], selectedService = service;
    const assistantIndex = history.length + 1;
    setBusy(true); setStreamingProject(key);
    const update = (work: (items: ChatMessage[]) => ChatMessage[]) => setMessages(previous => ({...previous, [key]: work(previous[key] || [])}));
    update(items => [...items, {role: 'user', content: text}, {role: 'assistant', content: ''}]);
    setGoal('');
    try {
      if (selectedService === 'chat') await streamChat(text, history, project?.id || '', useKnowledge,
        delta => update(items => items.map((item, index) => index === assistantIndex ? {...item, content: item.content + delta} : item)),
        sources => update(items => items.map((item, index) => index === assistantIndex ? {...item, sources} : item)),
        delta => update(items => items.map((item, index) => index === assistantIndex ? {...item, reasoning: (item.reasoning || '') + delta} : item)));
      else {
        if (!project) throw new Error('请先选择目标项目');
        const status = await startAgent(text, selectedService, project.id); onStarted(status);
        update(items => items.map((item, index) => index === assistantIndex ? {...item, content: `${project.id} 的${serviceLabel(selectedService)}已启动。运行记录中可查看阶段、日志和检索来源。`} : item));
      }
    } catch (error) {
      report(error);
      update(items => items.map((item, index) => index === assistantIndex ? {...item, content: item.content + '\n请求未完成，请查看错误提示后重试。'} : item));
      setGoal(current => current || text);
    } finally {submitPending.current = false; setBusy(false); setStreamingProject('');}
  }
  return <aside className="assistant-dock" aria-label="Agent 对话面板"><div className="dock-heading"><span className="ai-mark" aria-hidden="true">✦</span><div><h2>与 TraceFix 协作</h2><span>常驻助手 · {project?.id || '未选择项目'}</span></div></div>
    <div className="service-tabs" role="group" aria-label="服务模式">{services.map(item => <button key={item.id} type="button" className={service === item.id ? 'selected' : ''} onClick={() => setService(item.id)} disabled={busy} aria-pressed={service === item.id}>{item.label.split(' ')[0]}<small>{item.label.split(' ')[1]}</small></button>)}</div>
    <div className="dock-hint">{service === 'chat' ? (project ? '附带当前项目信息，模型可按需只读查看项目文件，并流式回答。' : '流式咨询模型；选择项目后可只读查看项目文件。') : 'Agent 在目标仓库的隔离工作区执行，并自主检索相关知识。'}</div>
    <div className="chat-window" ref={viewport} aria-live="polite">{!conversation.length && <div className="chat-intro"><span aria-hidden="true">✦</span><h3>一个问题，一次推进。</h3><p>描述遇到的前端问题，或询问测试方法。切换工作台页面时，对话会保留。</p><div><b>试着描述</b><span>操作步骤 · 预期表现 · 实际结果</span></div></div>}{conversation.map((message, index) => <div className={'chat-message ' + message.role} key={index}><small>{message.role === 'user' ? '你' : 'TRACEFIX'}</small>{message.role === 'assistant' && !!message.reasoning && <details className="thinking-block" open={busy && streamingProject === projectKey}><summary>思考过程</summary><pre>{message.reasoning}</pre></details>}<p>{message.content || (busy && streamingProject === projectKey ? '正在处理…' : '未返回内容')}</p>{!!message.sources?.length && <div className="source-links">{message.sources.map(source => <button type="button" onClick={() => onDocument(source.id)} key={source.id}><span aria-hidden="true">▤</span> {source.title} · v{source.version}</button>)}</div>}</div>)}</div>
    {!!agent.id && <div className="active-run"><button type="button" onClick={onRun}><i className={agent.running ? 'pulse' : ''} aria-hidden="true"/><span>{agent.projectId} · {statusLabel(agent.status || '')}<small>{agent.phase ? phaseLabel(agent.phase) : '等待进程反馈'}</small></span>↗</button>{agent.running && <button className="text-button danger" disabled={busy || stopping || !agent.canStop} title={agent.stopUnavailableReason} onClick={stop}>{stopping || agent.status === 'stopping' ? '停止中…' : '停止'}</button>}</div>}
    {agent.running && agent.stopUnavailableReason && agent.status !== 'stopping' && <small className="dock-footnote">{agent.stopUnavailableReason}</small>}
    {agent.stopError && <small className="dock-footnote" role="alert">{agent.stopError}</small>}
    <form className="run-composer" onSubmit={submit}><label htmlFor="run-goal">{service === 'chat' ? '发送消息' : '新的运行目标'}</label><textarea id="run-goal" value={goal} onChange={event => setGoal(event.target.value)} onKeyDown={event => {if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {event.preventDefault(); event.currentTarget.form?.requestSubmit();}}} maxLength={service === 'chat' ? 8000 : 4000} placeholder={service === 'chat' ? '向 TraceFix 提问…' : '输入目标后按 Enter 直接启动；Shift+Enter 换行'}/><div className="composer-footer"><span>{goal.length} / {service === 'chat' ? 8000 : 4000} · Enter 发送 · Shift+Enter 换行</span><button className="primary" disabled={busy || !services.length || !goal.trim() || (service !== 'chat' && (agent.running || !project?.ready))}>{busy ? '处理中…' : service === 'chat' ? '发送 ↑' : '启动 Run ↑'}</button></div></form>
    {service === 'chat' ? <label className="knowledge-toggle"><input type="checkbox" checked={useKnowledge} onChange={event => setUseKnowledge(event.target.checked)}/>使用当前项目与全局知识</label> : <small className="dock-footnote">检索仅加入相关片段；修复结论由验证结果决定。</small>}
  </aside>;
}
