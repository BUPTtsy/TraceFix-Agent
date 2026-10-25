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

// 编码后端只有 claude_sdk；test 检测流程没有编码后端（null），历史记录中的其他取值仅原样展示。
export const codingBackend = (record: Record<string, any>): string | null =>
  record.coding_backend || record.codingBackend || record.execution?.backend || null;

// waiting_approval 不再占用执行进程；仅当进程仍在（如交互式 CLI 等待 /approve）时视为占用执行器。
const executingStatuses = ['starting', 'running', 'paused', 'waiting_input', 'stopping'];
export const executorBusy = (record: Record<string, any>): boolean =>
  executingStatuses.includes(String(record.status).toLowerCase()) || Boolean(record.pid);

export const controlProjection = (record: Record<string, any>) => {
  const backend = codingBackend(record);
  const status = String(record.status).toLowerCase();
  const blockers = Array.isArray(record.resume_blockers) ? record.resume_blockers : [];
  const canStop = backend === 'claude_sdk' ? record.can_stop === true && status !== 'waiting_approval' :
    status !== 'stopping' && (executingStatuses.includes(status) || status === 'waiting_approval' && Boolean(record.pid));
  const canResume = backend === 'claude_sdk' && record.control_status === 'STOPPED' && record.can_resume === true &&
    !blockers.length && record.status !== 'unknown';
  const approval = record.approval && typeof record.approval === 'object' && !Array.isArray(record.approval) ? record.approval : null;
  return {
    ...(backend ? {coding_backend: backend} : {}),
    control_status: status === 'stopping' ? 'STOPPING' : record.control_status || String(record.status || '').toUpperCase(),
    can_stop: canStop,
    can_resume: canResume,
    can_approve: status === 'waiting_approval' && approval?.status === 'pending' && Boolean(record.taskId),
    approval,
    resume_blockers: blockers,
    harness_job_id: record.harness_job_id ?? record.harnessJobId ?? null,
    harness_session_id: record.harness_session_id ?? record.harnessSessionId ?? null,
    current_run_id: record.current_run_id ?? record.agentRunId ?? null,
    accepted_commit: record.accepted_commit ?? null,
    quality: record.quality ?? null,
    owner_epoch: record.owner_epoch ?? null,
    harness_revision: record.harness_revision ?? null,
  };
};

function redact(value: any): any {
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([key, item]) =>
    [key, ['api_key', 'apikey', 'password', 'authorization', 'proxy-authorization', 'secret', 'access_token',
      'refresh_token', 'cookie', 'set-cookie', 'x-api-key'].includes(key.toLowerCase()) ? '[REDACTED]' : redact(item)]));
  if (typeof value === 'string') return value.replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]');
  return value;
}

export class ConsoleDatabase {
  readonly db: DatabaseSync;
  constructor(readonly filename: string) {
    fs.mkdirSync(path.dirname(filename), {recursive: true});
    this.db = new DatabaseSync(filename, {timeout: 15000});
    this.db.exec('PRAGMA busy_timeout=15000');
    this.db.exec('CREATE TABLE IF NOT EXISTS console_runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)');
    this.db.exec('CREATE TABLE IF NOT EXISTS console_events (run_id TEXT, seq INTEGER, event_key TEXT, data TEXT NOT NULL, PRIMARY KEY (run_id, seq), UNIQUE (run_id, event_key))');
  }
  transaction<T>(work: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = work(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
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
        previous = {id, startedAt: timestamp(), logs: []}; }
      const record = {...previous, ...fields, updatedAt: timestamp()};
      this.db.prepare('INSERT OR REPLACE INTO console_runs VALUES (?,?)').run(id, JSON.stringify(record));
      return record;
    });
  }
  requestStop(id: string, processControlId?: string): RecordData {
    return this.transaction(() => {
      const record = this.run(id);
      if (!['running', 'paused', 'waiting_input', 'stopping'].includes(record.status) &&
          !(record.status === 'waiting_approval' && record.pid)) return record;
      if (record.processControlId !== processControlId) throw new DataError('任务进程已改变，请刷新后重试', 409);
      if (record.status === 'stopping') return record;
      const updated = {...record, status: 'stopping', control_status: 'STOPPING', stopRequestedAt: timestamp(),
        stopPreviousStatus: record.status, stopError: null, updatedAt: timestamp()};
      this.db.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify(updated), id);
      return updated;
    });
  }
  stopFailed(id: string, fields: RecordData): RecordData {
    return this.transaction(() => {
      const record = this.run(id);
      if (record.status !== 'stopping' || record.processControlId !== fields.processControlId ||
          record.stopRequestedAt !== fields.stopRequestedAt) return record;
      const updated = {...record, status: record.stopPreviousStatus || 'running', control_status: record.stopPreviousStatus || 'RUNNING', stopRequestedAt: null,
        stopPreviousStatus: null, stopError: fields.error, updatedAt: timestamp()};
      this.db.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify(updated), id);
      return updated;
    });
  }
  private appendEvent(runId: string, key: string, event: RecordData): void {
    const row = this.db.prepare('SELECT COALESCE(MAX(seq),0)+1 AS seq FROM console_events WHERE run_id=?').get(runId) as {seq: number};
    this.db.prepare('INSERT OR IGNORE INTO console_events VALUES (?,?,?,?)')
      .run(runId, row.seq, key, JSON.stringify(redact({...event, seq: row.seq})));
  }
  continueRun(id: string, instruction: string, before?: () => void): RecordData {
    if (typeof instruction !== 'string' || !instruction.trim() || instruction.trim().length > 4000)
      throw new DataError('请输入 1-4000 字的继续指令');
    return this.transaction(() => {
      before?.();
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
