import wrapAnsi from 'wrap-ansi';
import type {PublicRecord, TraceFixEvent, TraceFixMessage} from '../tracefix-events.js';
import {detailOnly} from './toolProjection.js';
export {selectUiMessages} from './toolProjection.js';

export interface UiMessage extends TraceFixMessage {publicEvents?: TraceFixEvent[]; compact?: boolean}

const LABELS: Record<string, string> = {
  RUNNING: '正在执行', WAITING_INPUT: '等待输入', WAITING_APPROVAL: '等待审批', WAITING_NETWORK: '等待网络恢复',
  PAUSED: '已暂停', COMPLETED: '已结束', CANCELLED: '已取消', FAILED: '运行失败', ABNORMAL: '异常结束',
  UNKNOWN: '操作结果未知', UNKNOWN_OPERATION: '操作结果未知', FIX_VERIFIED: '修复已验证',
  NO_BUG_FOUND: '当前测试范围内未发现问题', BUG_CONFIRMED: '已确认存在缺陷', INCONCLUSIVE: '证据不足，无法确定',
  INFRA_FAILURE: '运行环境故障', REPAIR_EXHAUSTED: '修复次数已用尽', POLICY_BLOCKED: '被策略阻止', LOOP_DETECTED: '检测到循环',
  PREPARE: '准备', EXPLORE: '探索', REPRODUCE: '复现', DIAGNOSE: '诊断', PATCH: '修补', VERIFY: '验证', REVIEW: '审批', FINALIZE: '收尾',
};
const FIELD_LABELS: Record<string, string> = {
  ref: '引用', staged_ref: '暂存补丁', overlay_revision: '工作区版本', before_hash: '修改前校验', patch_hash: '补丁校验',
  snapshot_ref: '内容快照', manifest_ref: '上下文清单', report_ref: '报告', html_ref: '报告页面', evidence_ref: '证据',
  observation_ref: '页面观察', approval_ref: '审批', continuation_ref: '恢复记录', skill_ref: 'Skill', version: '版本',
  skill_version: 'Skill 版本', content_hash: '内容校验', skill_hash: 'Skill 校验', source_hash: '来源校验', frozen_source_hash: '冻结来源校验',
  error_code: '错误码', operation_status: '操作状态', coverage: '覆盖范围', feedback_class: '反馈类别', validation_class: '验证类别',
};

function record(value: unknown): PublicRecord {return value && typeof value === 'object' && !Array.isArray(value) ? value as PublicRecord : {};}
function text(value: unknown): string {return typeof value === 'string' || typeof value === 'number' ? String(value) : '';}
function label(value: unknown, code = false): string {const raw = text(value); return LABELS[raw.toUpperCase()] ? LABELS[raw.toUpperCase()] + (code ? `（${raw}）` : '') : raw;}
function payload(message: TraceFixMessage): PublicRecord {return message.event?.payload || {};}
function runIdentity(message: TraceFixMessage): string | null {const event = message.event; return event?.run_id ? `${event.scope_id || ''}:${event.run_id}` : null;}
function status(message: TraceFixMessage): string {const value = payload(message); return text(value.status || value.run_status).toUpperCase();}
function events(message: UiMessage): TraceFixEvent[] {return message.publicEvents || (message.event ? [message.event] : []);}

export function appendUiMessage(messages: UiMessage[], next: UiMessage): UiMessage[] {
  const chatType = next.event?.type || '';
  if (chatType.startsWith('chat.') && chatType !== 'chat.user') {
    const index = messages.findIndex(message => message.id === next.id && runIdentity(message) === runIdentity(next));
    const previous = index >= 0 ? messages[index] : null;
    const previousPayload = previous ? payload(previous) : {};
    const nextPayload = payload(next);
    const previousText = text(previousPayload.content);
    const content = chatType === 'chat.delta' ? previousText + text(nextPayload.delta) :
      nextPayload.content !== undefined ? text(nextPayload.content) : previousText;
    const combined: UiMessage = {...next, text: content, metadata: {...previous?.metadata, ...next.metadata},
      event: next.event ? {...next.event, payload: {...previousPayload, ...nextPayload, content}} : undefined};
    if (index < 0) return [...messages, combined];
    return messages.map((message, position) => position === index ? combined : message);
  }
  if (messages.some(message => message.id === next.id && runIdentity(message) === runIdentity(next))) return messages;
  const type = next.event?.type || '';
  const run = runIdentity(next);
  let index = -1;
  if (type === 'state.changed' && run) {
    const latest = messages.findLastIndex(message => runIdentity(message) === run && (message.event?.type === 'state.changed' || message.event?.type === 'run.finished'));
    if (latest >= 0 && messages[latest].event?.type === 'state.changed' && status(messages[latest]) === status(next) && messages[latest].event?.phase === next.event?.phase) index = latest;
  }
  if (index < 0) return [...messages, next];
  const previous = messages[index];
  const previousPayload = payload(previous);
  const nextPayload = payload(next);
  const combined: UiMessage = {...next, id: previous.id, metadata: {...previous.metadata, ...next.metadata}, publicEvents: [...events(previous), ...events(next)]};
  if (next.event && type.startsWith('tool.')) combined.event = {...next.event, payload: {...previousPayload, ...nextPayload}};
  return messages.map((message, position) => position === index ? combined : message);
}

function toolName(value: PublicRecord): string {const intent = record(value.intent); const receipt = record(value.receipt); return text(value.tool_name || value.name || intent.tool_name || receipt.name);}
function toolSummary(value: PublicRecord): string {
  const intent = record(value.intent);
  const args = record(intent.arguments);
  const source = Object.keys(args).length ? args : intent;
  return ['kind', 'path', 'file_path', 'url', 'selector', 'query', 'command'].map(key => text(source[key])).filter(Boolean).map(value => value.length > 100 ? value.slice(0, 100) + '…' : value).join(' · ');
}

export function messageText(message: TraceFixMessage): string {
  const value = payload(message);
  const type = message.event?.type || '';
  if (type === 'chat.user') return `你：${text(value.message)}`;
  if (type.startsWith('chat.')) {
    const content = text(value.content);
    if (type === 'chat.error') return `${content}${content ? '\n' : ''}对话失败：${text(value.message || value.error) || '响应未完成'}`;
    if (type === 'chat.cancelled') return `${content}${content ? '\n' : ''}对话已取消`;
    return content || (type === 'chat.started' || type === 'chat.sources' ? '正在回答…' : '');
  }
  if (type === 'cli.output' || type === 'cli.error') return text(value.message).replace(/\n$/, '');
  if (type === 'run.finished') {
    const result = label(value.outcome, true);
    const state = status(message);
    const ending = state === 'CANCELLED' || state === 'FAILED' || state === 'ABNORMAL' ? label(state) + (result ? ` · ${result}` : '') : result || label(state) || '运行已结束';
    return ending + (text(value.error) ? `：${text(value.error)}` : '');
  }
  if (type === 'state.changed' || type === 'run.started') return `${label(message.event?.phase) || 'Run'} · ${label(status(message)) || '状态已更新'}`;
  if (type === 'run.continued' || type.includes('resume')) return '已继续执行' + (status(message) ? ` · ${label(status(message))}` : '');
  if (type.startsWith('tool.')) {
    const name = toolName(value);
    const receipt = record(value.receipt);
    const result = record(receipt.result);
    const error = text(value.error || receipt.error || result.error);
    const unknown = [value.operation_status, value.error_code, receipt.operation_status, receipt.error_code, result.operation_status, result.error_code].some(item => text(item).toUpperCase().includes('UNKNOWN'));
    const state = type === 'tool.started' ? '正在执行' : type === 'tool.requested' ? '已请求执行' : type === 'tool.rejected' ? '已拒绝执行' : type === 'tool.error' || receipt.is_error === true ? '执行失败' : type === 'tool.unknown' || unknown ? '结果未知，需核对' : type === 'tool.reconciled' ? '已核对结果' : receipt.passed === false ? '完成，结果未通过' : receipt.passed === true ? '完成，结果通过' : '调用完成';
    const summary = toolSummary(value);
    return `${name || '工具'} · ${state}${summary ? ` · ${summary}` : ''}${error ? `：${error}` : ''}`;
  }
  if (type === 'gate.decided') {
    const result = value.passed === true ? '通过' : value.passed === false ? '未通过' : '尚未确定';
    return `验证门禁 · ${result}` + (text(value.validation || value.message) ? `：${text(value.validation || value.message)}` : '');
  }
  if (type === 'action.business.outcome') return `业务验证 · ${value.passed === true ? '通过' : value.passed === false ? '未通过' : label(value.status) || '尚未确定'}`;
  if (message.kind === 'approval') return '等待审批 · 使用 /approve 或 /reject 处理';
  if (type === 'skill.loaded') return `已加载 Skill${text(value.name) ? ` · ${text(value.name)}` : ''}`;
  if (type === 'skills.injected') return 'Skill 已注入上下文';
  if (type === 'context.assembled' || type === 'context.compacted') return type === 'context.compacted' ? '上下文已压缩' : '上下文已组装';
  if (type.includes('feedback')) return `公开验证反馈${text(value.feedback_class || value.message) ? ` · ${text(value.feedback_class || value.message)}` : ''}`;
  if (type === 'run.error') return `运行错误：${text(value.error || value.message) || '请展开详情'}`;
  if (type === 'model.error.persisted') {const details = record(value.details); const issue = text(details.message || details.type) || label(value.category); return `模型请求遇到问题${issue ? `：${issue}` : ''}${text(value.status || details.status) ? ` · ${label(value.status || details.status)}` : ''}`;}
  if (type === 'run.cancelled') return `运行已取消${text(value.message) ? ` · ${text(value.message)}` : ''}`;
  if (text(value.message)) return text(value.message);
  if (type.includes('observation')) return '已记录页面观察';
  return message.text;
}

function compactValue(value: unknown, key: string): string {
  if (Array.isArray(value)) return value.map(item => {
    if (!item || typeof item !== 'object') return text(item);
    const source = record(item);
    const ref = text(source.ref || source.path || source.id || source.field);
    return ref + (source.version !== undefined ? `@${text(source.version)}` : '');
  }).filter(Boolean).join('、');
  const raw = text(value);
  if (key.endsWith('_hash') && raw.length > 16) return raw.slice(0, 12) + '…';
  return raw;
}

export function metadataLines(message: TraceFixMessage): string[] {
  const values: PublicRecord = {...message.metadata};
  for (const key of ['manifest_ref', 'report_ref', 'html_ref', 'evidence_ref', 'observation_ref', 'approval_ref', 'continuation_ref']) {
    if (payload(message)[key] !== undefined) values[key] = payload(message)[key];
  }
  const receipt = record(payload(message).receipt);
  if (receipt.observation_ref !== undefined) values.observation_ref = receipt.observation_ref;
  if (message.ref && !Object.values(values).includes(message.ref)) values.ref = message.ref;
  const lines: string[] = [];
  const seen = new Set<string>();
  for (const [path, value] of Object.entries(values)) {
    const key = path.split('.').at(-1) || path;
    let rendered = '';
    if ((key === 'selected' || key === 'dropped') && Array.isArray(value)) rendered = `${key === 'selected' ? '选用' : '未选用'} ${value.length} 项${value.length ? `：${compactValue(value, key)}` : ''}`;
    else if (key === 'off' && typeof value === 'boolean') rendered = `工作集${value ? '已关闭' : '已启用'}`;
    else if (FIELD_LABELS[key]) rendered = `${FIELD_LABELS[key]}：${compactValue(value, key)}`;
    else if ((key.endsWith('_ref') || key.endsWith('_refs')) && compactValue(value, key)) rendered = `引用：${compactValue(value, key)}`;
    else if (key === 'references' && Array.isArray(value) && value.length) rendered = `参考文件：${compactValue(value, key)}`;
    if (rendered && !seen.has(rendered)) {seen.add(rendered); lines.push(rendered);}
  }
  return lines;
}

export function messageLines(message: UiMessage, width: number, expanded: boolean): string[] {
  if (!expanded && detailOnly(message)) return [];
  if (message.compact && !expanded) return wrapAnsi(message.text, Math.max(1, width), {hard: true, trim: false}).split('\n');
  const lines = [messageText(message), ...metadataLines(message).map(line => '  ' + line)];
  if (expanded) for (const event of events(message)) {
    lines.push(`公开事件：${event.type}`);
    lines.push(JSON.stringify(event, null, 2));
  }
  return lines.flatMap(line => wrapAnsi(line, Math.max(1, width), {hard: true, trim: false}).split('\n'));
}

export function clampScroll(scroll: number, totalRows: number, viewport: number): number {return Math.max(0, Math.min(scroll, Math.max(0, totalRows - viewport)));}

export function visibleMessageRows(messages: UiMessage[], width: number, expanded: boolean, scroll: number, viewport: number): Array<{message: UiMessage; start: number; count: number}> {
  const sizes = messages.map(message => messageLines(message, width, expanded).length);
  const totalRows = sizes.reduce((total, rows) => total + rows, 0);
  const end = totalRows - clampScroll(scroll, totalRows, viewport);
  const beginning = Math.max(0, end - viewport);
  const visible: Array<{message: UiMessage; start: number; count: number}> = [];
  let position = 0;
  for (const [index, message] of messages.entries()) {
    const start = Math.max(beginning, position);
    const stop = Math.min(end, position + sizes[index]);
    if (start < stop) visible.push({message, start: start - position, count: stop - start});
    position += sizes[index];
  }
  return visible;
}
