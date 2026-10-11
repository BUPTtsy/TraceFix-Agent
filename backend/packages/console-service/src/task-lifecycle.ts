import {createHash, randomUUID} from 'node:crypto';
import {ConsoleDatabase, DataError, timestamp} from './database.js';

type Data = Record<string, any>;
const active = new Set(['starting', 'running', 'paused', 'waiting_input', 'waiting_approval', 'stopping']);
export const taskError = (code: string, message: string, status = 409, retryable = false) =>
  Object.assign(new DataError(message, status), {code, retryable});

function canonical(value: any): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key =>
    JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}

export function normalizeTask(fields: Data, projects: Data[]): Data {
  const allowed = ['projectId', 'repo_id', 'repositoryId', 'mode', 'goal', 'execution', 'additionalRuleIds',
    'rule_ids', 'source_run_id', 'source_finding_ids', 'github_reads', 'finding_acceptance', 'create_and_start'];
  if (!fields || typeof fields !== 'object' || Array.isArray(fields) || Object.keys(fields).some(key => !allowed.includes(key)))
    throw taskError('INVALID_TASK', 'Task 包含不允许的字段', 400);
  const ids = [fields.projectId, fields.repo_id, fields.repositoryId].filter(value => value !== undefined);
  if (!ids.length || ids.some(id => typeof id !== 'string' || !id || id.length > 128))
    throw taskError('INVALID_PROJECT', '必须指定已注册项目', 400);
  const matches = ids.map(id => projects.filter(item => item.id === id || item.repoId === id));
  if (matches.some(items => items.length !== 1) || new Set(matches.map(items => items[0].id)).size !== 1)
    throw taskError('INVALID_PROJECT', '项目不存在、存在歧义或别名冲突', 400);
  const project = matches[0][0];
  if (!project.ready) throw taskError('PROJECT_NOT_READY', '项目尚未就绪或已禁用', 409);
  if (!['test', 'repair'].includes(fields.mode) || typeof fields.goal !== 'string' || !fields.goal.trim() || fields.goal.trim().length > 4000)
    throw taskError('INVALID_TASK', '目标或模式无效', 400);
  if (/\b(?:sk|rk)-[a-z0-9_-]{16,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]{8,}/i.test(JSON.stringify(fields)))
    throw taskError('TASK_CONTAINS_CREDENTIALS', 'Task 不允许包含真实凭据', 400);
  const execution = fields.execution;
  if (!execution || typeof execution !== 'object' || Array.isArray(execution) ||
      Object.keys(execution).some(key => !['executionMode', 'url', 'spec'].includes(key)))
    throw taskError('INVALID_EXECUTION', '必须指定 batch 执行配置', 400);
  if (execution.executionMode !== 'batch') throw taskError('UNSUPPORTED_EXECUTION_MODE', '首版仅支持 batch 执行', 400);
  if (execution.url !== undefined) {
    let url;
    try {url = new URL(execution.url);} catch {throw taskError('INVALID_URL', '执行 URL 无效', 400);}
    if (typeof execution.url !== 'string' || execution.url.length > 2048 || !['http:', 'https:'].includes(url.protocol) || url.username || url.password)
      throw taskError('INVALID_URL', '执行 URL 不允许携带凭据', 400);
  }
  if (execution.spec !== undefined && (typeof execution.spec !== 'string' || !execution.spec.trim() || execution.spec.length > 32000))
    throw taskError('INVALID_SPEC', 'spec 必须是最多 32000 字的文本', 400);
  const list = (value: any, label: string, limit = 50) => {
    if (value === undefined) return [];
    if (!Array.isArray(value) || value.length > limit || value.some(id => typeof id !== 'string' || !id || id.length > 200))
      throw taskError('INVALID_TASK', label + ' 无效', 400);
    return [...new Set(value)];
  };
  const additionalRuleIds = list(fields.additionalRuleIds ?? fields.rule_ids, '追加规则');
  if (fields.additionalRuleIds !== undefined && fields.rule_ids !== undefined &&
      canonical(list(fields.rule_ids, '追加规则').sort()) !== canonical([...additionalRuleIds].sort()))
    throw taskError('INVALID_TASK', '追加规则别名冲突', 400);
  if (fields.source_run_id !== undefined && (typeof fields.source_run_id !== 'string' || !fields.source_run_id || fields.source_run_id.length > 200))
    throw taskError('INVALID_SOURCE', '来源 Run 无效', 400);
  const sourceFindingIds = list(fields.source_finding_ids, '来源 Finding', 100);
  if (sourceFindingIds.length && !fields.source_run_id || fields.source_run_id && fields.mode !== 'repair')
    throw taskError('INVALID_SOURCE', '来源 Finding 必须绑定 Repair 的来源 Run', 400);
  const reads = fields.github_reads ?? [];
  if (!Array.isArray(reads) || reads.length > 50 || reads.some(read => {
    if (!read || typeof read !== 'object' || Array.isArray(read) ||
        Object.keys(read).some(key => !['source_kind', 'path', 'number'].includes(key))) return true;
    if (!['metadata', 'commit', 'file', 'issue', 'pull_request'].includes(read.source_kind)) return true;
    if (['metadata', 'commit'].includes(read.source_kind)) return read.path !== undefined || read.number !== undefined;
    if (read.source_kind === 'file') return read.number !== undefined || typeof read.path !== 'string' ||
      !read.path || read.path.length > 1024 || /[\\:\x00-\x1f]/.test(read.path) || read.path.startsWith('/') ||
      read.path.split('/').some((part: string) => !part || part === '.' || part === '..' || /^\.env(?:\.|$)/i.test(part));
    return read.path !== undefined || !Number.isSafeInteger(read.number) || read.number < 1;
  })) throw taskError('INVALID_GITHUB_READ', 'GitHub 读取仅允许受控只读引用', 400);
  if (fields.finding_acceptance !== undefined)
    throw taskError('ACCEPTANCE_NOT_INTEGRATED', 'finding_acceptance 等待 C 提供严格验收适配', 422);
  if (fields.create_and_start !== undefined && typeof fields.create_and_start !== 'boolean')
    throw taskError('INVALID_TASK', 'create_and_start 必须是布尔值', 400);
  return {projectId: project.id, mode: fields.mode, goal: fields.goal.trim(), execution: {...execution},
    additionalRuleIds, source_run_id: fields.source_run_id ?? null, source_finding_ids: sourceFindingIds,
    github_reads: reads};
}

export class TaskLifecycle {
  constructor(readonly store: ConsoleDatabase, readonly normalize: (fields: Data) => Data,
    readonly prepareRun: (fields: Data, id: string) => Data) {
    store.db.exec(`
      CREATE TABLE IF NOT EXISTS console_tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS console_idempotency (scope TEXT NOT NULL, key TEXT NOT NULL,
        request_hash TEXT NOT NULL, task_id TEXT NOT NULL, run_id TEXT, data TEXT NOT NULL, PRIMARY KEY(scope,key));
      CREATE TABLE IF NOT EXISTS console_executor (slot INTEGER PRIMARY KEY CHECK(slot=1),
        task_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE, owner_pid INTEGER NOT NULL, owner_id TEXT NOT NULL,
        child_pid INTEGER, phase TEXT NOT NULL);
    `);
  }
  private writeTask(task: Data): void {
    this.store.db.prepare('INSERT OR REPLACE INTO console_tasks VALUES (?,?)').run(task.id, JSON.stringify(task));
  }
  private writeRun(run: Data): void {
    this.store.db.prepare('INSERT OR REPLACE INTO console_runs VALUES (?,?)').run(run.id, JSON.stringify(run));
  }
  private replay(scope: string, key: string | undefined, fields: Data): Data | null {
    if (!key) return null;
    if (typeof key !== 'string' || key.length > 200 || !/^[\x21-\x7e]+$/.test(key))
      throw taskError('INVALID_IDEMPOTENCY_KEY', '幂等键须为 1-200 个可见 ASCII 字符', 400);
    const row = this.store.db.prepare('SELECT * FROM console_idempotency WHERE scope=? AND key=?').get(scope, key) as Data | undefined;
    if (!row) return null;
    if (row.request_hash !== createHash('sha256').update(canonical(fields)).digest('hex'))
      throw taskError('IDEMPOTENCY_CONFLICT', '同一幂等键已绑定不同正文', 409);
    return {...JSON.parse(row.data), replayed: true};
  }
  private remember(scope: string, key: string | undefined, fields: Data, value: Data): void {
    if (key) this.store.db.prepare('INSERT INTO console_idempotency VALUES (?,?,?,?,?,?)')
      .run(scope, key, createHash('sha256').update(canonical(fields)).digest('hex'), value.task.id,
        value.run?.id ?? null, JSON.stringify(value));
  }
  task(id: string): Data {
    const row = this.store.db.prepare('SELECT data FROM console_tasks WHERE id=?').get(id) as Data | undefined;
    if (!row) throw taskError('TASK_NOT_FOUND', 'Task 不存在', 404);
    const task = JSON.parse(row.data);
    if (task.currentRunId && active.has(task.status)) {
      const run = this.store.run(task.currentRunId);
      if (!active.has(run.status)) {
        task.status = run.status; task.updatedAt = run.updatedAt || timestamp();
      } else if (task.status !== 'starting') task.status = run.status;
    }
    return task;
  }
  tasks(projectId?: string): Data[] {
    return (this.store.db.prepare('SELECT id FROM console_tasks ORDER BY rowid DESC').all() as Data[])
      .map(row => this.task(row.id)).filter(task => !projectId || task.definition.projectId === projectId);
  }
  create(fields: Data, key?: string): Data {
    return this.store.transaction(() => {
      const previous = this.replay('create', key, fields);
      if (previous) return previous;
      const definition = this.normalize(fields), now = timestamp();
      const task = {id: randomUUID(), definition, status: 'created', version: 1,
        currentRunId: null, runIds: [], createdAt: now, updatedAt: now};
      this.writeTask(task);
      const value = {task, replayed: false};
      this.remember('create', key, fields, value);
      return value;
    });
  }
  update(id: string, fields: Data): Data {
    return this.store.transaction(() => {
      const task = this.task(id);
      if (active.has(task.status) || this.lease()?.task_id === id)
        throw taskError('TASK_ACTIVE', '活动执行不可编辑');
      if (fields.create_and_start !== undefined) throw taskError('INVALID_TASK', '编辑不能隐式启动', 400);
      const updated = {...task, definition: this.normalize(fields), status: 'created', version: task.version + 1, updatedAt: timestamp()};
      this.writeTask(updated);
      return updated;
    });
  }
  lease(): Data | null {
    return this.store.db.prepare('SELECT * FROM console_executor WHERE slot=1').get() as Data || null;
  }
  assertIdle(): void {
    if (this.lease() || this.store.runs().some(run => active.has(run.status) || run.pid))
      throw taskError('EXECUTOR_BUSY', '单执行器正在运行其他 Run', 409, true);
  }
  start(id: string, fields: Data, key: string | undefined, ownerId: string, namespace = 'explicit'): Data {
    return this.store.transaction(() => {
      const scope = 'start:' + namespace + ':' + id;
      const previous = this.replay(scope, key, fields);
      if (previous) return previous;
      const task = this.task(id);
      if (Object.keys(fields).length) throw taskError('INVALID_START', 'start 正文必须为空对象', 400);
      this.assertIdle();
      if (active.has(task.status)) throw taskError('TASK_ACTIVE', 'Task 已在执行');
      const runId = randomUUID(), now = timestamp();
      const prepared = this.prepareRun(task.definition, runId);
      const run = {...prepared, taskId: task.id, execution: task.definition.execution,
        source_run_id: prepared.source_run_id ?? task.definition.source_run_id, source_finding_ids: task.definition.source_finding_ids,
        github_reads: task.definition.github_reads, processControlId: randomUUID(), updatedAt: now};
      this.writeRun(run);
      const updated = {...task, status: 'starting', currentRunId: runId,
        runIds: [...task.runIds, runId], updatedAt: now};
      this.writeTask(updated);
      this.store.db.prepare('INSERT INTO console_executor VALUES (1,?,?,?,?,NULL,?)').run(id, runId, process.pid, ownerId, 'reserved');
      const value = {task: updated, run, replayed: false};
      this.remember(scope, key, fields, value);
      return value;
    });
  }
  launchIntent(id: string, runId: string, ownerId: string): void {
    const result = this.store.db.prepare("UPDATE console_executor SET phase='launching' WHERE slot=1 AND task_id=? AND run_id=? AND owner_id=? AND phase='reserved'")
      .run(id, runId, ownerId);
    if (result.changes !== 1) throw taskError('EXECUTOR_OWNERSHIP_LOST', '执行预留已改变');
  }
  spawned(id: string, runId: string, ownerId: string, pid: number): Data {
    return this.store.transaction(() => {
      const lease = this.lease();
      if (!lease || lease.task_id !== id || lease.run_id !== runId || lease.owner_id !== ownerId)
        throw taskError('EXECUTOR_OWNERSHIP_LOST', '执行预留已改变');
      this.store.db.prepare("UPDATE console_executor SET child_pid=?, phase='spawned' WHERE slot=1").run(pid);
      const run = {...this.store.run(runId), pid, updatedAt: timestamp()};
      this.writeRun(run);
      const task = {...this.task(id), status: 'running', updatedAt: timestamp()};
      this.writeTask(task);
      return {task, run};
    });
  }
  finish(id: string, runId: string, ownerId: string, changes: Data): Data {
    return this.store.transaction(() => {
      const lease = this.lease();
      if (!lease || lease.task_id !== id || lease.run_id !== runId || lease.owner_id !== ownerId)
        throw taskError('EXECUTOR_OWNERSHIP_LOST', '执行预留已改变');
      const run = {...this.store.run(runId), ...changes, pid: null, processEndedAt: timestamp(), updatedAt: timestamp()};
      this.writeRun(run);
      const task = this.task(id);
      if (task.currentRunId === runId) this.writeTask({...task, status: run.status, updatedAt: timestamp()});
      this.store.db.prepare('DELETE FROM console_executor WHERE slot=1 AND run_id=? AND owner_id=?').run(runId, ownerId);
      return {task: this.task(id), run};
    });
  }
  recover(isAlive: (pid: number) => boolean): Data | null {
    return this.store.transaction(() => {
      const lease = this.lease();
      if (!lease || isAlive(lease.owner_pid)) return lease;
      const run = {...this.store.run(lease.run_id), status: 'unknown', outcome: 'UNKNOWN', finishedAt: timestamp(),
        error: {code: 'EXECUTOR_INTERRUPTED', message: 'API 在启动或执行期间退出；执行结果不确定', retryable: false}, updatedAt: timestamp()};
      this.writeRun(run);
      this.writeTask({...this.task(lease.task_id), status: 'unknown', updatedAt: timestamp()});
      if (lease.phase === 'reserved' || lease.phase === 'spawned' && lease.child_pid && !isAlive(lease.child_pid)) {
        this.writeRun({...run, pid: null, processEndedAt: timestamp()});
        this.store.db.prepare('DELETE FROM console_executor WHERE slot=1 AND run_id=?').run(lease.run_id);
        return null;
      }
      return lease;
    });
  }
}
