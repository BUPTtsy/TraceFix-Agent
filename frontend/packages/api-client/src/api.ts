import {errorWithContext} from './errors';

export type Service = 'test' | 'repair' | 'chat';
export interface Project {id: string; repoId: string; root: string; profile: string | null; url: string | null; ready: boolean; issue: string | null; allowedFiles: string[]; commands: Record<string, string[]>; runCount: number; documentCount: number}
export interface Source {id: string; title: string; version: number}
export interface KnowledgeUse {phase: string; queries: string[]; documents: Source[]; artifact_ref: string}
export interface Run {id: string; projectId: string; agentRunId?: string; goal: string; mode: Service | 'unknown'; status: string; phase: string; outcome?: string; startedAt: string; finishedAt?: string; timeSource?: string; exitCode?: number; error?: string; branch?: string; continuationCount?: number; abnormalTermination?: boolean; continuationMarkers?: {instruction: string; previous_status: string; previous_error?: string; at: string}[]; knowledge: KnowledgeUse[]; logs?: string[]; artifacts?: {ref: string; label: string; bytes: number}[]}
export interface AgentStatus extends Partial<Run> {running: boolean; canStop?: boolean; stopUnavailableReason?: string; stopError?: string | null}
// 引导状态来自持久化账本；retarget 排队后仍需 confirmed_at 才能在运行时替换目标。
export interface Guidance {id: string; run_id: string; level: 'hint' | 'constraint' | 'retarget'; text: string; status: 'queued' | 'applied' | 'acknowledged' | 'rejected' | 'superseded'; created_at: number; applied_step?: number; how_applied?: string; rejection_reason?: string; confirmed_at?: number; child_run_id?: string}
export interface TraceEvent {seq: number; agentSeq?: number; type: string; phase: string; at: number; payload: Record<string, unknown>}
export type WorkerStatus = 'CREATED' | 'QUEUED' | 'RUNNING' | 'RETRYING' | 'SUCCEEDED' | 'PARTIAL' | 'FAILED' | 'CANCELLED' | 'EXPIRED' | string;
export interface WorkerSnapshot {
  workerId: string;
  taskId: string;
  role: string;
  roleLabel: string;
  goal: string;
  phase: string;
  status: WorkerStatus;
  attempt: number;
  maxAttempts: number;
  threadId?: string;
  durationMs?: number;
  summary?: string;
  error?: string;
  result?: unknown;
  resultRef?: string;
  active?: boolean;
  updatedAt?: number;
  lastEvent?: string;
  lastSeq?: number;
}
export interface WorkerAggregate {
  workers: WorkerSnapshot[];
  active: number;
  maxConcurrency: number;
}

const workerEventStatus: Record<string, WorkerStatus> = {
  created: 'CREATED', queued: 'QUEUED', started: 'RUNNING', progress: 'RUNNING',
  retrying: 'RETRYING', completed: 'SUCCEEDED', partial: 'PARTIAL', failed: 'FAILED',
  cancelled: 'CANCELLED', canceled: 'CANCELLED', expired: 'EXPIRED',
};
const workerStatusAliases: Record<string, WorkerStatus> = {
  COMPLETED: 'SUCCEEDED', SUCCESS: 'SUCCEEDED', SUCCEEDED: 'SUCCEEDED', DONE: 'SUCCEEDED',
  CANCELED: 'CANCELLED', CANCELLED: 'CANCELLED', FAILED: 'FAILED',
};
const workerTerminalStatuses = new Set(['SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELLED', 'EXPIRED']);
const workerActiveStatuses = new Set(['RUNNING', 'RETRYING']);
const value = (payload: Record<string, unknown>, ...keys: string[]) => {
  for (const key of keys) if (payload[key] !== undefined && payload[key] !== null) return payload[key];
  return undefined;
};
const stringValue = (payload: Record<string, unknown>, ...keys: string[]) => {
  const item = value(payload, ...keys);
  return item === undefined ? '' : String(item);
};
const numberValue = (payload: Record<string, unknown>, fallback: number, ...keys: string[]) => {
  const item = value(payload, ...keys);
  const parsed = typeof item === 'number' ? item : Number(item);
  return Number.isFinite(parsed) ? parsed : fallback;
};

/** Fold append-only worker lifecycle events into the latest UI snapshots. */
// 只从追加的生命周期事件还原子 Agent 快照，页面刷新或重新轮询时可得到一致的最终状态。
export function aggregateWorkers(events: TraceEvent[]): WorkerAggregate {
  const snapshots = new Map<string, WorkerSnapshot>();
  let maxConcurrency = 4;
  for (const event of events) {
    const kind = event.type.toLowerCase();
    if (!(kind.startsWith('worker.') || kind.startsWith('subtask.'))) continue;
    const payload = event.payload || {};
    maxConcurrency = numberValue(payload, maxConcurrency, 'max_concurrency', 'maxConcurrency', 'worker_limit');
    const suffix = kind.split('.').pop() || '';
    const workerId = stringValue(payload, 'worker_id', 'workerId', 'id', 'task_id', 'taskId');
    if (!workerId || suffix === 'joined') continue;
    const previous = snapshots.get(workerId);
    const rawStatus = stringValue(payload, 'status').toUpperCase();
    // 兼容 worker/subtask 两套事件字段；显式状态优先，缺失时保留前一个快照中的信息。
    const status = workerStatusAliases[rawStatus] || rawStatus || workerEventStatus[suffix] || previous?.status || 'UNKNOWN';
    const taskId = stringValue(payload, 'task_id', 'taskId') || previous?.taskId || workerId;
    const role = stringValue(payload, 'role') || previous?.role || 'worker';
    const roleLabel = stringValue(payload, 'role_label', 'roleLabel') || previous?.roleLabel || role;
    const goal = stringValue(payload, 'goal', 'task', 'description') || previous?.goal || '';
    const snapshot: WorkerSnapshot = {
      workerId,
      taskId,
      role,
      roleLabel,
      goal,
      phase: stringValue(payload, 'phase') || previous?.phase || event.phase,
      status,
      attempt: numberValue(payload, previous?.attempt || 1, 'attempt', 'try', 'retry'),
      maxAttempts: numberValue(payload, previous?.maxAttempts || 4, 'max_attempts', 'maxAttempts', 'retry_limit'),
      threadId: stringValue(payload, 'thread_id', 'threadId', 'thread_name', 'threadName') || previous?.threadId,
      durationMs: numberValue(payload, previous?.durationMs || 0, 'duration_ms', 'durationMs') || undefined,
      summary: stringValue(payload, 'summary') || (payload.result && typeof payload.result === 'object' ? stringValue(payload.result as Record<string, unknown>, 'summary') : '') || previous?.summary,
      error: stringValue(payload, 'error', 'message') || previous?.error,
      result: value(payload, 'result') ?? previous?.result,
      resultRef: stringValue(payload, 'result_ref', 'resultRef', 'artifact_ref', 'artifactRef') || previous?.resultRef,
      active: workerActiveStatuses.has(status),
      updatedAt: event.at,
      lastEvent: kind,
      lastSeq: event.seq,
    };
    snapshots.set(workerId, snapshot);
  }
  const workers = [...snapshots.values()].sort((a, b) => {
    const activeDifference = Number(Boolean(b.active)) - Number(Boolean(a.active));
    return activeDifference || (b.lastSeq || 0) - (a.lastSeq || 0);
  });
  const active = workers.filter(worker => worker.active && workerActiveStatuses.has(worker.status)).length;
  return {workers, active, maxConcurrency};
}
export interface Run {canContinue?: boolean}
export interface ReportIssue {id: string; title: string; source: string; status: string; location: string; expected: string; actual: string; steps: string[]; verification: string; evidence_refs: string[]}
export interface Run {issueReport?: {summary: string; issues: ReportIssue[]; coverage: string; limits: string} | null; reportError?: string | null}
export interface Run {parentRunId?: string; additionalRuleIds?: string[]; ruleSnapshot?: {run_id: string; parent_run_id: string | null; snapshot_hash: string; refs: {id: string; version: number}[]} | null}
export interface KnowledgeDocument {id: string; title: string; content: string; preview?: string; kind: 'repair' | 'testing' | 'experience'; projectId: string | null; tags: string[]; enabled: boolean; version: number; createdAt: string; updatedAt: string; sourceRunId: string | null}
export type DocumentDraft = Omit<KnowledgeDocument, 'id' | 'createdAt' | 'updatedAt'> & {id?: string};
export interface SearchHit extends Omit<KnowledgeDocument, 'content'> {excerpt: string; score: number; chunk: number}
export type RuleStatus = 'draft' | 'enabled' | 'disabled' | 'archived';
export interface DetectionRule {id: string; name: string; version: number; status: RuleStatus; category: string; severity: string; priority: number; pinned: boolean; scope: {level: string; project_ids: string[]; url_patterns: string[]; path_globs: string[]; frameworks: string[]}; phases: string[]; detection: {type: 'oracle' | 'static' | 'guided'; oracle?: Record<string, unknown>; static?: Record<string, unknown>; guided?: Record<string, unknown>}; fix_guidance: string; examples: Record<string, string>; tags: string[]; owner: string; created_at?: string; updated_at?: string}
export interface RuleVersion {rule_id: string; version: number; body_hash: string; change_note: string; author: string; created_at: string}
export interface RuleInsight {rule_id: string | null; total: number; reproduced: number; false_positive: number; fixed: number; reproduction_rate: number; false_positive_rate: number; fix_rate: number}
export interface ChatMessage {role: 'user' | 'assistant'; content: string; reasoning?: string; sources?: Source[]}

async function fetchResponse(url: string, options: RequestInit): Promise<Response> {
  try {return await fetch(url, options);}
  catch (error) {throw errorWithContext('请求发送失败，请检查网络连接或服务状态', error);}
}
async function responseJson(response: Response) {
  try {return await response.json();}
  catch (error) {throw errorWithContext(`响应 JSON 解析失败（HTTP ${response.status}）`, error);}
}
async function request<Value>(url: string, options: RequestInit = {}): Promise<Value> {
  const response = await fetchResponse(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  const data = await responseJson(response);
  if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）${data?.error ? '：' + data.error : ''}`);
  return data;
}
export const loadProjects = () => request<Project[]>('/api/projects');
export const loadServices = () => request<{id: Service; label: string}[]>('/api/services');
export const loadRuns = () => request<Run[]>('/api/runs');
export const loadRun = (id: string) => request<Run>(`/api/runs/${encodeURIComponent(id)}`);
export const loadRunTrace = (id: string, after = 0) => request<TraceEvent[]>(`/api/runs/${encodeURIComponent(id)}/trace?after=${after}`);
// 提交引导和确认改目标是两个独立请求，避免一次误操作直接结束正在执行的父 Run。
export const loadGuidance = (id: string) => request<Guidance[]>(`/api/runs/${encodeURIComponent(id)}/guidance`);
export const submitGuidance = (id: string, text: string, level: Guidance['level']) => request<Guidance>(`/api/runs/${encodeURIComponent(id)}/guidance`, {method: 'POST', body: JSON.stringify({text, level})});
export const confirmGuidance = (id: string, guidanceId: string) => request<Guidance>(`/api/runs/${encodeURIComponent(id)}/guidance/${encodeURIComponent(guidanceId)}/confirm`, {method: 'POST', body: '{}'});
export const loadAgentStatus = () => request<AgentStatus>('/api/agent/status');
export const startAgent = (goal: string, mode: Service, projectId: string) => request<AgentStatus>('/api/agent/start', {method: 'POST', body: JSON.stringify({goal, mode, projectId})});
export const stopAgent = () => request<AgentStatus>('/api/agent/stop', {method: 'POST'});
export const continueRun = (id: string, instruction: string) => request<AgentStatus>(`/api/runs/${encodeURIComponent(id)}/continue`, {method: 'POST', body: JSON.stringify({instruction})});
export const deriveRun = (id: string, additionalRuleIds: string[], goal = '') => request<AgentStatus>(`/api/runs/${encodeURIComponent(id)}/derive`, {method: 'POST', body: JSON.stringify({additionalRuleIds, goal})});
export const loadDocuments = (projectId: string, query = '') => request<KnowledgeDocument[]>(`/api/knowledge?${new URLSearchParams({projectId, q: query})}`);
export const loadDocument = (id: string) => request<KnowledgeDocument>(`/api/knowledge/${encodeURIComponent(id)}`);
export const saveDocument = (document: DocumentDraft) => request<KnowledgeDocument>(`/api/knowledge${document.id ? '/' + encodeURIComponent(document.id) : ''}`, {method: document.id ? 'PUT' : 'POST', body: JSON.stringify(document)});
export const searchDocuments = (projectId: string, query: string) => request<SearchHit[]>('/api/knowledge/search', {method: 'POST', body: JSON.stringify({projectId, query})});
export const loadRules = (projectId: string, filters: {status?: string; q?: string; includeArchived?: boolean} = {}) => request<DetectionRule[]>(`/api/rules?${new URLSearchParams({projectId, ...(filters.status ? {status: filters.status} : {}), ...(filters.q ? {q: filters.q} : {}), ...(filters.includeArchived ? {includeArchived: 'true'} : {})})}`);
export const loadRule = (id: string) => request<DetectionRule>(`/api/rules/${encodeURIComponent(id)}`);
export const saveRule = (rule: Partial<DetectionRule> & {id?: string; expectedVersion?: number; changeNote?: string}) => request<DetectionRule>(`/api/rules${rule.version && rule.id ? '/' + encodeURIComponent(rule.id) : ''}`, {method: rule.version ? 'PUT' : 'POST', body: JSON.stringify(rule)});
export const changeRuleStatus = (id: string, action: 'publish' | 'disable' | 'archive', expectedVersion?: number) => request<DetectionRule>(`/api/rules/${encodeURIComponent(id)}:${action}`, {method: 'POST', body: JSON.stringify({expectedVersion})});
export const deleteRule = (id: string) => request<{ok: boolean}>(`/api/rules/${encodeURIComponent(id)}`, {method: 'DELETE'});
export const loadRuleVersions = (id: string) => request<RuleVersion[]>(`/api/rules/${encodeURIComponent(id)}/versions`);
export const loadRuleInsights = () => request<RuleInsight[]>('/api/rules/insights');
export const loadRuleFindings = (id: string) => request<Array<Record<string, unknown>>>(`/api/rules/${encodeURIComponent(id)}/findings`);
export const loadRulePreview = (id: string) => request<{prompt: string; items: Array<Record<string, unknown>>; rule_ids: string[]; tokens: number}>(`/api/rules/${encodeURIComponent(id)}/preview`);
export const artifactUrl = (id: string, ref: string) => `/api/runs/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(ref)}`;

export async function streamChat(message: string, history: ChatMessage[], projectId: string, useKnowledge: boolean, onDelta: (delta: string) => void, onSources: (sources: Source[]) => void, onReasoning?: (delta: string) => void) {
  // 历史只发送角色与正文；reasoning 用专用回调展示，由服务端保存为审计记录，不重复注入历史。
  const response = await fetchResponse('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'text/event-stream'}, body: JSON.stringify({message, history: history.map(({role, content}) => ({role, content})), projectId, useKnowledge})});
  if (!response.ok) {const data = await responseJson(response); throw new Error(`对话请求失败（HTTP ${response.status}）${data?.error ? '：' + data.error : ''}`);}
  if (response.headers.get('content-type')?.includes('application/json')) {
    const payload = await responseJson(response);
    if (payload.error) throw new Error(`对话服务返回错误：${payload.error}`);
    if (payload.sources) onSources(payload.sources);
    if (payload.reasoning) onReasoning?.(payload.reasoning);
    if (payload.content) onDelta(payload.content);
    return;
  }
  if (!response.body) throw new Error('浏览器不支持流式响应');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      // 网络块可能切断 SSE 事件或 UTF-8 字符，缓冲至完整事件边界后再分别转发正文和 reasoning。
      const events = buffer.split(/\r?\n\r?\n/); buffer = events.pop() || '';
      for (const event of events) for (const line of event.split(/\r?\n/)) if (line.startsWith('data:')) {
        const payload = JSON.parse(line.slice(5));
        if (payload.error) throw new Error(`对话服务返回错误：${payload.error}`);
        if (payload.reasoning) onReasoning?.(payload.reasoning);
        if (payload.delta) onDelta(payload.delta);
        if (payload.sources) onSources(payload.sources);
      }
      if (done) break;
    }
  } catch (error) {throw errorWithContext(error instanceof SyntaxError ? '对话响应 JSON 解析失败' : '对话响应读取失败', error);}
  finally {await reader.cancel().catch(() => {}); reader.releaseLock();}
}
