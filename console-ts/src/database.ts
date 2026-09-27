import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';

type RecordData = Record<string, any>;

export class DataError extends Error {
  constructor(message: string, public status = 400) { super(message); }
}

export const timestamp = () => new Date().toISOString();
export const canContinue = (status: string, outcome?: string | null) => {
  const normalized = String(status).toLowerCase();
  return ['abnormal', 'failed', 'cancelled', 'completed'].includes(normalized) &&
    !(normalized === 'completed' && ['FIX_VERIFIED', 'NO_BUG_FOUND'].includes(outcome || ''));
};

function redact(value: any): any {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [key, ['api_key', 'apikey', 'password', 'authorization', 'proxy-authorization', 'secret', 'access_token',
      'refresh_token', 'cookie', 'set-cookie', 'x-api-key'].includes(key.toLowerCase()) ? '[REDACTED]' : redact(item)]));
  if (typeof value === 'string') return value.replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]');
  return value;
}

function terms(text: string): Set<string> {
  const result = new Set(text.toLowerCase().match(/[a-z0-9_]{2,}/g) || []);
  for (const phrase of text.match(/[\u4e00-\u9fff]+/g) || [])
    for (let index = 0; index < Math.max(1, phrase.length - 1); index++) result.add(phrase.slice(index, index + 2));
  return result;
}

const overlap = (left: Set<string>, right: Set<string>) => [...left].filter(term => right.has(term)).length;

export class ConsoleDatabase {
  readonly db: DatabaseSync;
  constructor(readonly filename: string) {
    fs.mkdirSync(path.dirname(filename), {recursive: true});
    this.db = new DatabaseSync(filename, {timeout: 15000});
    this.db.exec('PRAGMA busy_timeout=15000');
    this.db.exec('CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, data TEXT NOT NULL)');
    this.db.exec('CREATE TABLE IF NOT EXISTS console_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)');
    this.db.exec('CREATE TABLE IF NOT EXISTS console_events (run_id TEXT, seq INTEGER, event_key TEXT, data TEXT NOT NULL, PRIMARY KEY (run_id, seq), UNIQUE (run_id, event_key))');
  }
  transaction<T>(work: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = work(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }
  documents(projectId?: string, includeDisabled = true): RecordData[] {
    return (this.db.prepare('SELECT data FROM documents').all() as {data: string}[])
      .map(row => JSON.parse(row.data))
      .filter(record => (!projectId || record.projectId === null || record.projectId === projectId) &&
        (includeDisabled || record.enabled))
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
  }
  document(id: string): RecordData {
    const row = this.db.prepare('SELECT data FROM documents WHERE id=?').get(id) as {data: string} | undefined;
    if (!row) throw new DataError('文档不存在', 404);
    return JSON.parse(row.data);
  }
  saveDocument(fields: RecordData, id?: string): RecordData {
    const {title, content, tags, kind, enabled} = fields;
    if (typeof title !== 'string' || !title.trim() || title.trim().length > 120) throw new DataError('文档标题须为 1-120 字');
    if (typeof content !== 'string' || !content.trim() || Buffer.byteLength(content) > 262144 || content.includes('\0'))
      throw new DataError('文档须为非空 UTF-8 文本，最大 256 KiB');
    if (!Array.isArray(tags) || tags.length > 12 || tags.some(tag => typeof tag !== 'string' || !tag.trim() || tag.trim().length > 30))
      throw new DataError('最多 12 个标签，每个标签 1-30 字');
    if (!['repair', 'testing', 'experience'].includes(kind) || typeof enabled !== 'boolean') throw new DataError('文档类型或启用状态无效');
    return this.transaction(() => {
      const previous = id ? this.document(id) : null;
      if (previous && fields.version !== previous.version) throw new DataError('文档已被其他窗口更新，请重新打开后编辑', 409);
      const now = timestamp();
      const record = {
        id: id || randomUUID(), title: title.trim(), content, tags: [...new Set(tags.map(tag => tag.trim()))], kind, enabled,
        projectId: fields.projectId || null, sourceRunId: previous?.sourceRunId || fields.sourceRunId || null,
        version: previous ? previous.version + 1 : 1, createdAt: previous?.createdAt || now, updatedAt: now,
      };
      this.db.prepare('INSERT OR REPLACE INTO documents VALUES (?,?)').run(record.id, JSON.stringify(record));
      return record;
    });
  }
  search(query: string, projectId: string, limit = 8): RecordData[] {
    const queryTerms = terms(query.slice(0, 2000));
    if (!queryTerms.size) return [];
    const matches: RecordData[] = [];
    for (const record of this.documents(projectId, false)) {
      const titleScore = overlap(queryTerms, terms(record.title + ' ' + record.tags.join(' '))) * 3;
      let best: RecordData | null = null;
      for (let offset = 0, index = 0; offset < record.content.length; offset += 1200, index++) {
        const excerpt = record.content.slice(offset, offset + 1400);
        const score = titleScore + overlap(queryTerms, terms(excerpt));
        if (!best || score > best.score) best = {excerpt, chunk: index, score};
      }
      if (best?.score) { const {content, ...summary} = record; matches.push({...summary, ...best}); }
    }
    return matches.sort((left, right) => right.score - left.score || left.id.localeCompare(right.id)).slice(0, Math.max(1, Math.min(limit, 20)));
  }
  runs(projectId?: string): RecordData[] {
    return (this.db.prepare('SELECT data FROM console_runs').all() as {data: string}[]).map(row => JSON.parse(row.data))
      .filter(record => !projectId || record.projectId === projectId)
      .sort((left, right) => right.startedAt.localeCompare(left.startedAt));
  }
  run(id: string): RecordData {
    const row = this.db.prepare('SELECT data FROM console_runs WHERE id=?').get(id) as {data: string} | undefined;
    if (!row) throw new DataError('运行记录不存在', 404);
    return JSON.parse(row.data);
  }
  updateRun(id: string, fields: RecordData, create = false): RecordData {
    return this.transaction(() => {
      let previous: RecordData;
      try { previous = this.run(id); }
      catch (error) { if (!create || !(error instanceof DataError) || error.status !== 404) throw error;
        previous = {id, startedAt: timestamp(), knowledge: [], logs: []}; }
      const record = {...previous, ...fields, updatedAt: timestamp()};
      this.db.prepare('INSERT OR REPLACE INTO console_runs VALUES (?,?)').run(id, JSON.stringify(record));
      return record;
    });
  }
  private appendEvent(runId: string, key: string, event: RecordData): void {
    const row = this.db.prepare('SELECT COALESCE(MAX(seq),0)+1 AS seq FROM console_events WHERE run_id=?').get(runId) as {seq: number};
    this.db.prepare('INSERT OR IGNORE INTO console_events VALUES (?,?,?,?)')
      .run(runId, row.seq, key, JSON.stringify(redact({...event, seq: row.seq})));
  }
  continueRun(id: string, instruction: string): RecordData {
    if (typeof instruction !== 'string' || !instruction.trim() || instruction.trim().length > 4000)
      throw new DataError('请输入 1-4000 字的继续指令');
    return this.transaction(() => {
      const record = this.run(id);
      if (!canContinue(record.status, record.outcome)) throw new DataError('只有非成功结束的任务可以继续；请刷新任务状态', 409);
      const marker = redact({id: randomUUID(), instruction: instruction.trim(), at: timestamp(), previous_status: record.status,
        previous_phase: record.phase, previous_outcome: record.outcome, previous_error: record.error,
        previous_finished_at: record.finishedAt, previous_report_ref: record.reportRef});
      const markers = [...(record.continuationMarkers || []), marker];
      Object.assign(record, {continuationMarkers: markers, continuationCount: markers.length, abnormalTermination: true,
        continuationInstruction: marker.instruction, continuationId: marker.id, status: 'running', phase: 'STARTING',
        outcome: null, error: null, reportRef: null, branch: null, finishedAt: null, exitCode: null,
        processEndedAt: null, pid: null, updatedAt: timestamp()});
      this.appendEvent(id, 'continuation:' + marker.id, {type: 'run.continuation_requested', phase: 'STARTING',
        at: Date.parse(marker.at) / 1000, payload: marker});
      this.db.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify(record), id);
      return record;
    });
  }
  addEvent(id: string, event: RecordData): void {
    this.transaction(() => this.appendEvent(id, 'agent:' + event.seq, {...event, agentSeq: event.seq}));
  }
  runEvents(id: string, after = 0): RecordData[] {
    this.run(id);
    return (this.db.prepare('SELECT data FROM console_events WHERE run_id=? AND seq>? ORDER BY seq').all(id, after) as {data: string}[])
      .map(row => JSON.parse(row.data));
  }
}
