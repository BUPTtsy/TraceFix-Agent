import {StringDecoder} from 'node:string_decoder';

export type PublicRecord = Record<string, unknown>;

export interface TraceFixEvent {
  run_id?: string;
  scope_id?: string;
  seq?: number;
  agentSeq?: number;
  phase?: string;
  type: string;
  revision?: number;
  at?: string | number;
  payload: PublicRecord;
  [key: string]: unknown;
}
export interface EventBatch {
  events: unknown[];
  cursor?: string | null;
  high_watermark?: number;
  snapshot?: unknown;
  requires_new_observation?: boolean;
  scope_id?: string;
  run_id?: string;
}

export interface TraceFixMessage {
  id: string;
  kind: 'run' | 'tool' | 'error' | 'cancel' | 'resume' | 'approval' | 'context' | 'skill' | 'feedback' | 'event';
  text: string;
  event?: TraceFixEvent;
  ref?: string;
  metadata: PublicRecord;
}

export interface EventGap {
  expected: number;
  received: number;
  reason: 'sequence_gap' | 'cursor_invalid' | 'cursor_ahead' | 'cursor_expired' | 'durable_gap';
}

const BLOCKED_KEY = /(?:^|[_.-])(hidden|private|oracle|held[_-]?out|final[_-]?scor(?:e|ing)|scoring)(?:$|[_.-])/i;
const BLOCKED_VALUE = /^(hidden|private|secret|oracle|held[_-]?out|heldout|final[_-]?oracle|final[_-]?scor(?:e|ing)(?:[_-]?only)?)$/i;
const VISIBILITY_KEYS = new Set(['visibility', 'audience', 'record_class', 'record_type', 'classification',
  'source', 'source_type', 'provenance', 'evidence_source', 'split', 'evaluation_split', 'purpose']);

function blockedKey(key: string): boolean {
  return BLOCKED_KEY.test(key.replace(/([a-z])([A-Z])/g, '$1_$2'));
}

function blockedRecord(value: PublicRecord): boolean {
  if (value.hidden === true || value.private === true || value.public === false ||
      value.learnable === false || value.final_scoring_only === true ||
      value.held_out === true || value.heldout === true) return true;
  for (const [key, item] of Object.entries(value)) {
    if (VISIBILITY_KEYS.has(key.toLowerCase()) && typeof item === 'string' && BLOCKED_VALUE.test(item)) return true;
    if (key === 'type' && typeof item === 'string' && blockedKey(item)) return true;
  }
  return false;
}

function clonePublic(value: unknown): unknown {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === 'object') return clonePublic(parsed) === undefined ? undefined : value;
    } catch { return value; }
  }
  if (Array.isArray(value)) {
    const result = value.map(clonePublic);
    return result.some(item => item === undefined) ? undefined : result;
  }
  if (!value || typeof value !== 'object') return value;
  const source = value as PublicRecord;
  if (blockedRecord(source)) return undefined;
  const result: PublicRecord = {};
  for (const [key, item] of Object.entries(source)) {
    if (blockedKey(key)) continue;
    const cloned = clonePublic(item);
    if (cloned === undefined) return undefined;
    result[key] = cloned;
  }
  return result;
}

export function publicPayload(value: unknown): unknown {
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      return typeof parsed === 'string' ? parsed : clonePublic(parsed);
    } catch {
      return value;
    }
  }
  return clonePublic(value);
}

function parsePayload(row: PublicRecord): PublicRecord {
  const candidate = row.payload !== undefined ? row.payload : row.payload_json;
  if (typeof candidate === 'string') {
    try {
      const parsed = JSON.parse(candidate);
      return (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) ? parsed as PublicRecord : {value: parsed};
    } catch {
      return {raw_payload_json: candidate, payload_parse_error: true};
    }
  }
  if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) return candidate as PublicRecord;
  return candidate === undefined ? {} : {value: candidate};
}

function unwrapRow(input: unknown): PublicRecord | null {
  if (!input || typeof input !== 'object') {
    if (typeof input !== 'string') return null;
    try { return unwrapRow(JSON.parse(input)); } catch { return null; }
  }
  const row = input as PublicRecord;
  if (row.data !== undefined && (typeof row.data === 'string' || typeof row.data === 'object')) {
    const unwrapped = unwrapRow(row.data);
    if (unwrapped) return {...unwrapped, ...Object.fromEntries(Object.entries(row).filter(([key]) => key !== 'data'))};
  }
  return row;
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(stable).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.entries(value as PublicRecord).sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => JSON.stringify(key) + ':' + stable(item)).join(',') + '}';
  return JSON.stringify(value);
}

function decodeCursor(cursor: string, scopeId: string, runId?: string): {scope_id: string; run_id: string; seq: number} {
  let parsed: unknown;
  try {
    if (!/^[A-Za-z0-9_-]+$/.test(cursor)) throw new Error('cursor');
    const padded = cursor + '='.repeat((4 - cursor.length % 4) % 4);
    parsed = JSON.parse(Buffer.from(padded, 'base64url').toString('utf8'));
  } catch {
    throw new Error('事件游标格式无效');
  }
  if (!parsed || typeof parsed !== 'object') throw new Error('事件游标字段无效');
  const value = parsed as PublicRecord;
  if (Object.keys(value).sort().join(',') !== 'run_id,scope_id,seq' || typeof value.scope_id !== 'string' || typeof value.run_id !== 'string' || !value.run_id) throw new Error('事件游标字段无效');
  if (value.scope_id !== scopeId || (runId && value.run_id !== runId)) throw new Error('事件游标作用域与 Run 不匹配');
  if (!Number.isInteger(value.seq) || Number(value.seq) < 0) throw new Error('事件游标序号无效');
  const canonical = Buffer.from(JSON.stringify({scope_id: value.scope_id, run_id: value.run_id, seq: value.seq})).toString('base64url');
  if (canonical !== cursor) throw new Error('事件游标编码不规范');
  return {scope_id: String(value.scope_id), run_id: String(value.run_id), seq: Number(value.seq)};
}

function eventKind(type: string, payload: PublicRecord): TraceFixMessage['kind'] {
  const lower = type.toLowerCase();
  const status = String(payload.status || payload.run_status || '').toLowerCase();
  if (lower.includes('approval') || lower === 'patch.proposed' || status === 'waiting_approval') return 'approval';
  if (lower.includes('cancel') || status === 'cancelled' || status === 'canceled') return 'cancel';
  if (lower.includes('resume') || lower.includes('continuation') || lower.includes('continued')) return 'resume';
  if (lower.includes('error') || lower.includes('failed') || payload.error !== undefined) return 'error';
  if (lower.startsWith('tool.')) return 'tool';
  if (lower.startsWith('skill.') || lower.includes('skill')) return 'skill';
  if (lower.includes('validation') || lower.includes('feedback') || lower === 'gate.decided') return 'feedback';
  if (lower.includes('context') || lower.includes('knowledge') || lower.includes('memory') || lower.includes('workset')) return 'context';
  if (lower.startsWith('run.') || lower === 'state.changed' || status) return 'run';
  return 'event';
}

function messageText(type: string, payload: PublicRecord): string {
  const detail = payload.error ?? payload.message ?? payload.status ?? payload.outcome ?? payload.operation ?? payload.tool_name;
  return detail === undefined ? type : `${type}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`;
}

function reference(payload: PublicRecord): string | undefined {
  for (const key of ['ref', 'evidence_ref', 'artifact_ref', 'report_ref', 'patch_ref', 'skill_ref', 'context_ref']) {
    if (typeof payload[key] === 'string' && payload[key]) return payload[key] as string;
  }
  return undefined;
}

const METADATA_KEYS = new Set([
  'ref', 'refs', 'staged_ref', 'staged_refs', 'overlay_revision', 'before_hash', 'disk_before_hash', 'patch_hash',
  'error_code', 'operation_status', 'status', 'outcome', 'unknown', 'skill_ref', 'skill_refs', 'skill_hash',
  'skill_version', 'snapshot_ref', 'content_hash', 'source_hash', 'frozen_source_hash', 'source_redacted',
  'byte_count', 'version', 'references', 'reference_hash', 'selected', 'dropped', 'off', 'coverage', 'span',
  'character_span', 'line_count', 'feedback', 'feedback_class', 'validation_class', 'classification', 'public',
]);

function metadataFrom(value: PublicRecord, prefix = '', result: PublicRecord = {}): PublicRecord {
  for (const [key, item] of Object.entries(value)) {
    const normalized = key.replace(/([a-z])([A-Z])/g, '$1_$2').toLowerCase();
    const path = prefix ? `${prefix}.${key}` : key;
    if (METADATA_KEYS.has(normalized)) result[path] = item;
    if (item && typeof item === 'object' && !Array.isArray(item)) metadataFrom(item as PublicRecord, path, result);
  }
  return result;
}

function normalizeEvent(raw: unknown, fallbackScope: string, fallbackRun?: string): TraceFixEvent | null {
  const row = unwrapRow(raw);
  if (!row || typeof row.type !== 'string' || !row.type) return null;
  if (publicPayload(row) === undefined || (row.scope_id && row.scope_id !== fallbackScope) || (row.run_id && fallbackRun && row.run_id !== fallbackRun)) return null;
  const payload = publicPayload(parsePayload(row));
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const event: TraceFixEvent = {
    ...(row.run_id || fallbackRun ? {run_id: row.run_id || fallbackRun} : {}), scope_id: row.scope_id || fallbackScope,
    ...(row.seq !== undefined ? {seq: row.seq as number} : {}), ...(row.agentSeq !== undefined ? {agentSeq: row.agentSeq as number} : {}),
    ...(row.phase !== undefined ? {phase: String(row.phase)} : {}), type: row.type,
    ...(row.revision !== undefined ? {revision: row.revision as number} : {}), ...(row.at !== undefined ? {at: row.at as string | number} : {}),
    payload: payload as PublicRecord,
  };
  return event;
}

export class EventIntake {
  readonly scopeId: string;
  readonly runId?: string;
  after = 0;
  highWatermark = 0;
  cursor: string | null = null;
  snapshot: PublicRecord | null = null;
  requiresNewObservation = false;
  gap: EventGap | null = null;
  private readonly seen = new Set<string>();

  constructor(scopeId: string, runId?: string) {
    if (!scopeId) throw new Error('事件作用域不能为空');
    this.scopeId = scopeId;
    this.runId = runId;
  }

  accept(input: unknown[] | EventBatch): TraceFixMessage[] {
    const batch = Array.isArray(input) ? null : input && typeof input === 'object' ? input as EventBatch : null;
    const rows = batch ? batch.events || [] : input as unknown[];
    if (!Array.isArray(rows)) throw new Error('事件批次必须是数组');
    let cursorSequence: number | null = null;
    const previousAfter = this.after;
    if (batch) {
      if (batch.scope_id && batch.scope_id !== this.scopeId) throw new Error('事件批次作用域不匹配');
      if (batch.run_id && this.runId && batch.run_id !== this.runId) throw new Error('事件批次 Run 不匹配');
      if (batch.cursor) {
        const decoded = decodeCursor(batch.cursor, this.scopeId, this.runId);
        cursorSequence = decoded.seq;
        if (batch.high_watermark !== undefined && Number(batch.high_watermark) !== decoded.seq) throw new Error('事件游标与水位不匹配');
        this.cursor = batch.cursor;
        this.highWatermark = Math.max(this.highWatermark, Number(batch.high_watermark ?? decoded.seq));
      } else if (Number.isInteger(batch.high_watermark)) {
        this.highWatermark = Math.max(this.highWatermark, Number(batch.high_watermark));
      }
      const publicSnapshot = publicPayload(batch.snapshot);
      const snapshotRecord = batch.snapshot && typeof batch.snapshot === 'object' ? batch.snapshot as PublicRecord : null;
      if (snapshotRecord?.scope_id && snapshotRecord.scope_id !== this.scopeId) throw new Error('快照作用域与请求不匹配');
      if (snapshotRecord?.run_id && this.runId && snapshotRecord.run_id !== this.runId) throw new Error('快照 Run 与请求不匹配');
      this.snapshot = publicSnapshot && typeof publicSnapshot === 'object' && !Array.isArray(publicSnapshot) ? publicSnapshot as PublicRecord : null;
      if (!this.snapshot && batch.snapshot && typeof batch.snapshot === 'object') {
        const raw = batch.snapshot as PublicRecord;
        if (typeof raw.reason === 'string' && ['cursor_invalid', 'cursor_ahead', 'cursor_expired', 'durable_gap'].includes(raw.reason)) this.snapshot = {reason: raw.reason};
      }
      this.requiresNewObservation = Boolean(batch.requires_new_observation || this.snapshot);
      if (this.snapshot && typeof this.snapshot.reason === 'string' && ['cursor_invalid', 'cursor_ahead', 'cursor_expired', 'durable_gap'].includes(this.snapshot.reason))
        this.gap = {expected: this.after + 1, received: this.highWatermark, reason: this.snapshot.reason as EventGap['reason']};
    }
    const parsedRows = rows.map(unwrapRow).filter((row): row is PublicRecord => Boolean(row));
    for (const row of parsedRows) {
      if (row.scope_id && row.scope_id !== this.scopeId) throw new Error('事件作用域与请求不匹配');
      if (row.run_id && this.runId && row.run_id !== this.runId) throw new Error('事件 Run 与请求不匹配');
      if (row.seq !== undefined && (!Number.isInteger(row.seq) || Number(row.seq) < 1)) throw new Error('事件序号无效');
    }
    const events = parsedRows.map(row => normalizeEvent(row, this.scopeId, this.runId)).filter((event): event is TraceFixEvent => Boolean(event));
    events.sort((left, right) => Number(left.seq ?? left.agentSeq ?? 0) - Number(right.seq ?? right.agentSeq ?? 0));
    const messages: TraceFixMessage[] = [];
    let expected = previousAfter + 1;
    for (const event of events) {
      const sequence = Number.isInteger(event.seq) ? Number(event.seq) : Number(event.agentSeq);
      const key = Number.isInteger(sequence) ? `${event.scope_id || this.scopeId}:${event.run_id || this.runId || ''}:${sequence}` : stable(event);
      if (this.seen.has(key)) continue;
      if (Number.isInteger(sequence) && sequence <= previousAfter) { this.seen.add(key); continue; }
      if (Number.isInteger(sequence) && sequence > expected && !this.gap) {
        this.gap = {expected, received: sequence, reason: 'sequence_gap'};
        this.requiresNewObservation = true;
      }
      if (Number.isInteger(sequence)) { this.after = Math.max(this.after, sequence); expected = this.after + 1; this.highWatermark = Math.max(this.highWatermark, sequence); }
      this.seen.add(key);
      const payload = event.payload;
      const metadata = metadataFrom(payload);
      messages.push({id: key, kind: eventKind(event.type, payload), text: messageText(event.type, payload), event, ref: reference(payload), metadata});
    }
    if (cursorSequence !== null) this.after = Math.max(this.after, cursorSequence);
    return messages;
  }
}

export class ChunkedTextDecoder {
  private readonly decoder = new StringDecoder('utf8');
  private partial = '';

  push(chunk: Buffer | string): string[] {
    this.partial += this.decoder.write(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
    const lines = this.partial.split('\n');
    this.partial = lines.pop() || '';
    return lines.map(line => line.endsWith('\r') ? line.slice(0, -1) : line);
  }

  flush(): string[] {
    this.partial += this.decoder.end();
    if (!this.partial) return [];
    const line = this.partial.endsWith('\r') ? this.partial.slice(0, -1) : this.partial;
    this.partial = '';
    return [line];
  }
}
