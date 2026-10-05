import type {PublicRecord, TraceFixEvent} from '../tracefix-events.js';
import type {UiMessage} from './presentation.js';

type ToolState = 'pending' | 'success' | 'failure' | 'unknown';
interface ToolCall {key: string; group: string; aliases: Set<string>; state: ToolState; name: string; message: string; ledgerOnly: boolean; events: TraceFixEvent[]}
interface ToolGroup {key: string; calls: Set<string>; message: UiMessage; events: TraceFixEvent[]}

function record(value: unknown): PublicRecord {return value && typeof value === 'object' && !Array.isArray(value) ? value as PublicRecord : {};}
function text(value: unknown): string {return typeof value === 'string' || typeof value === 'number' ? String(value) : '';}
function runKey(event: TraceFixEvent): string {return `${event.scope_id || ''}:${event.run_id || ''}`;}
function name(value: PublicRecord): string {return text(value.tool_name || value.name || record(value.intent).tool_name || record(value.receipt).name || record(record(value.receipt).result).name);}
function callIds(value: PublicRecord): string[] {
  const receipt = record(value.receipt);
  const intent = record(value.intent);
  const nested = record(receipt.receipt);
  const result = record(receipt.result);
  return [...new Set([value.tool_call_id, value.call_id, intent.tool_call_id, intent.call_id,
    receipt.tool_call_id, receipt.call_id, nested.tool_call_id, nested.call_id, result.tool_call_id, result.call_id].map(text).filter(Boolean))];
}
function statuses(value: PublicRecord): string[] {
  const receipt = record(value.receipt);
  const result = record(receipt.result);
  const nested = record(receipt.receipt);
  const details = record(value.details);
  return [value.status, value.operation_status, value.error_code, receipt.status, receipt.operation_status, receipt.error_code,
    receipt.outcome, result.status, result.operation_status, result.error_code, nested.status, nested.operation_status, nested.error_code,
    details.status, details.operation_status].map(item => text(item).toUpperCase());
}
function resultState(event: TraceFixEvent, previous: ToolState): ToolState {
  const value = event.payload;
  const receipt = record(value.receipt);
  const nested = record(receipt.receipt);
  const state = statuses(value);
  if (event.type === 'tool.unknown' || state.some(item => item.includes('UNKNOWN'))) return 'unknown';
  if (event.type === 'tool.reconciled') {
    const result = record(receipt.result);
    if (receipt.outcome === 'not_applied' || result.executed === false || receipt.executed === false ||
        receipt.isError === true || receipt.is_error === true || result.isError === true || result.is_error === true || nested.isError === true || nested.is_error === true ||
        state.some(item => ['FAILED', 'ERROR', 'REJECTED', 'NOT_APPLIED'].includes(item))) return 'failure';
    if (receipt.outcome === 'completed' || receipt.isError === false || receipt.is_error === false || receipt.executed === true ||
        state.some(item => ['DONE', 'COMPLETED', 'SUCCESS', 'SUCCEEDED'].includes(item)))
      return receipt.isError === true || receipt.is_error === true || result.isError === true || result.is_error === true ? 'failure' : 'success';
    return previous;
  }
  if (previous === 'unknown') return previous;
  if (state.includes('WAITING_NETWORK') || value.request_status === 'not_sent' || record(value.details).request_status === 'not_sent') return 'pending';
  if (event.type === 'tool.error' || event.type === 'tool.rejected' || value.isError === true || value.is_error === true ||
      receipt.isError === true || receipt.is_error === true || nested.isError === true || nested.is_error === true || receipt.executed === false ||
      state.some(item => ['FAILED', 'ERROR', 'REJECTED', 'NOT_APPLIED'].includes(item))) return 'failure';
  return event.type === 'tool.completed' ? 'success' : previous;
}
function issue(value: PublicRecord): string {
  const receipt = record(value.receipt);
  const result = record(receipt.result);
  const error = value.error || receipt.error || result.error;
  return text(error) || text(record(error).message) || text(value.reason || record(value.details).message);
}

export function detailOnly(message: UiMessage): boolean {
  const type = message.event?.type || '';
  return (type.startsWith('model.') && type !== 'model.error.persisted') || type === 'operation.called' ||
    type === 'context.assembled' || type === 'skills.injected';
}

export function selectUiMessages(messages: UiMessage[], expanded: boolean): UiMessage[] {
  if (expanded) return messages;
  const output: UiMessage[] = [];
  const groups = new Map<string, ToolGroup>();
  const calls = new Map<string, ToolCall>();
  const aliases = new Map<string, string>();
  const currentGroups = new Map<string, string>();
  const segments = new Map<string, number>();
  const seenEvents = new Set<string>();
  let serial = 0;
  const groupFor = (event: TraceFixEvent): string => {
    const run = runKey(event);
    const value = event.payload;
    if (value.logical_exchange_id && value.tool_round !== undefined)
      return `${run}:exchange:${text(value.logical_exchange_id)}:round:${text(value.tool_round)}`;
    if (event.type === 'model.started' && value.logical_call !== undefined)
      return `${run}:call:${text(value.logical_call)}:round:${text(value.tool_round) || '0'}`;
    return currentGroups.get(run) || `${run}:phase:${event.phase || ''}:segment:${segments.get(run) || 0}`;
  };
  const ensureGroup = (key: string): ToolGroup => {
    const existing = groups.get(key);
    if (existing) return existing;
    const summary: UiMessage = {id: `tool-summary:${key}`, kind: 'tool', text: '', metadata: {}, compact: true};
    const group = {key, calls: new Set<string>(), message: summary, events: []};
    groups.set(key, group);
    output.push(summary);
    return group;
  };
  const mergeCalls = (target: ToolCall, duplicateKeys: string[]) => {
    for (const duplicateKey of duplicateKeys) {
      const duplicate = calls.get(duplicateKey);
      if (!duplicate || duplicate === target) continue;
      groups.get(duplicate.group)?.calls.delete(duplicate.key);
      for (const alias of duplicate.aliases) {target.aliases.add(alias); aliases.set(alias, target.key);}
      target.events.push(...duplicate.events);
      if (duplicate.state !== 'pending') target.state = duplicate.state;
      calls.delete(duplicate.key);
    }
  };
  for (const message of messages) {
    const rawEvents = message.publicEvents || (message.event ? [message.event] : []);
    if (!rawEvents.length) {output.push(message); continue;}
    let ordinary = true;
    for (const event of rawEvents) {
      const eventIdentity = `${runKey(event)}:${event.seq ?? event.agentSeq ?? message.id + ':' + JSON.stringify(event)}`;
      if (seenEvents.has(eventIdentity)) continue;
      seenEvents.add(eventIdentity);
      const run = runKey(event);
      const value = event.payload;
      if (event.type === 'model.started') currentGroups.set(run, groupFor(event));
      if (event.type === 'model.error.persisted') {
        const details = record(value.details);
        const ids = callIds({...value, ...details}).map(id => `${run}:call:${id}`);
        const operationAlias = text(details.operation_id || value.operation_id);
        if (operationAlias) ids.push(`${run}:operation:${operationAlias}`);
        const related = [...new Set(ids.map(id => aliases.get(id)).filter((id): id is string => Boolean(id)))];
        const call = related.length ? calls.get(related[0]) : undefined;
        if (call) {
          mergeCalls(call, related.slice(1));
          for (const id of ids) {call.aliases.add(id); aliases.set(id, call.key);}
          if (callIds({...value, ...details}).length) call.ledgerOnly = false;
          call.state = resultState({...event, type: 'tool.error', payload: {...value, ...details}}, call.state);
          call.message ||= issue({...value, ...details});
        }
      }
      if (event.type === 'model.called' || event.type === 'run.finished' || event.type === 'run.continued' || event.type === 'gate.decided') {
        segments.set(run, (segments.get(run) || 0) + 1);
        currentGroups.delete(run);
      }
      if (!event.type.startsWith('tool.')) continue;
      ordinary = false;
      const groupKey = groupFor(event);
      const ids = [...callIds(value).map(id => `${run}:call:${id}`), ...[value.operation_id].map(text).filter(Boolean).map(id => `${run}:operation:${id}`)];
      const matched = [...new Set(ids.map(id => aliases.get(id)).filter((id): id is string => Boolean(id)))];
      let call = matched.length ? calls.get(matched[0]) : undefined;
      if (!call) {
        serial += 1;
        call = {key: ids[0] || `${run}:anonymous:${serial}`, group: groupKey, aliases: new Set(), state: 'pending',
          name: name(value), message: '', ledgerOnly: Boolean(value.operation_id && record(value.intent).tool_name && !callIds(value).length), events: []};
        calls.set(call.key, call);
        ensureGroup(groupKey).calls.add(call.key);
      }
      mergeCalls(call, matched.slice(1));
      for (const id of ids) {call.aliases.add(id); aliases.set(id, call.key);}
      if (callIds(value).length) call.ledgerOnly = false;
      call.name ||= name(value);
      call.message = issue(value) || call.message;
      call.state = resultState(event, call.state);
      call.events.push(event);
      groups.get(call.group)?.events.push(event);
    }
    if (ordinary && !detailOnly(message)) output.push(message);
    if (message.event?.type === 'model.tool.result.persisted') {
      const event = message.event;
      const run = runKey(event);
      const key = aliases.get(`${run}:call:${text(event.payload.tool_call_id)}`);
      const call = key ? calls.get(key) : undefined;
      const expectedGroup = groupFor(event);
      if (call && event.payload.reused !== true && call.group !== expectedGroup) {
        groups.get(call.group)?.calls.delete(call.key);
        call.group = expectedGroup;
        ensureGroup(expectedGroup).calls.add(call.key);
      }
    }
  }
  const result: UiMessage[] = [];
  for (const message of output) {
    if (!message.compact) {result.push(message); continue;}
    const group = groups.get(message.id.slice('tool-summary:'.length));
    if (!group || !group.calls.size) continue;
    const recorded = [...group.calls].map(key => calls.get(key)).filter((call): call is ToolCall => Boolean(call));
    const knownCalls = recorded.filter(call => !call.ledgerOnly);
    const items = knownCalls.length ? knownCalls : recorded;
    const success = items.filter(call => call.state === 'success').length;
    const failure = items.filter(call => call.state === 'failure').length;
    const unknown = items.filter(call => call.state === 'unknown').length;
    const pending = items.filter(call => call.state === 'pending').length;
    const summary = `本次调用了${items.length}个工具，成功数${success}，失败数${failure}` +
      (unknown ? `，未知数${unknown}` : '') + (pending ? `，进行中${pending}` : '');
    result.push({...message, text: summary, publicEvents: group.events});
    const problem = recorded.find(call => call.state === 'unknown') || items.find(call => call.state === 'failure');
    if (problem) result.push({id: `${message.id}:problem`, kind: 'error', metadata: {}, compact: true,
      text: `${problem.state === 'unknown' ? '工具结果未知，需核对' : '工具执行遇到问题'}${problem.name ? ` · ${problem.name}` : ''}${problem.message ? `：${problem.message.slice(0, 140)}` : ' · Ctrl+O 查看公开详情'}`});
  }
  return result;
}
