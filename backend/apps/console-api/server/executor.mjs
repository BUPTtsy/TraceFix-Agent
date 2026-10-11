import {spawn as spawnProcess} from 'node:child_process';
import {randomUUID} from 'node:crypto';
import path from 'node:path';
import {existsSync} from 'node:fs';
import {terminateChildTree} from './agent-stop.mjs';

const active = new Set(['starting', 'running', 'paused', 'waiting_input', 'waiting_approval', 'stopping']);
const alive = pid => {try {process.kill(pid, 0); return true;} catch {return false;}};
const failure = (code, message, status = 409, retryable = false) => Object.assign(new Error(message), {code, status, retryable});

export function executionRequest(task, run) {
  return {schema_version: 1, task_id: task.id, run_id: run.id, repo_id: task.definition.projectId,
    mode: task.definition.mode, goal: task.definition.goal, execution: task.definition.execution,
    additional_rule_ids: task.definition.additionalRuleIds, repository_snapshot: run.repositorySnapshot,
    source_run_id: run.source_run_id ?? null, source_finding_ids: task.definition.source_finding_ids,
    github_reads: task.definition.github_reads, finding_acceptance: null};
}

export function createExecutor({bridge, projectRoot, spawn = spawnProcess, isAlive = alive,
  terminate = terminateChildTree, graceMs = 15000, reportError = console.error, pythonCommand}) {
  const ownerId = randomUUID(), owned = new Map(), stopping = new Map();
  const python = pythonCommand || process.env.TRACEFIX_PYTHON || (existsSync(path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')) ?
    path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python') : process.platform === 'win32' ? 'py' : 'python3');
  const redact = value => {
    let text = String(value).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]');
    for (const name of ['TRACEFIX_API_KEY', 'TRACEFIX_DATABASE_URL', 'TRACEFIX_GITHUB_TOKEN', 'TRACEFIX_CONTROL_TOKEN'])
      if (process.env[name]) text = text.replaceAll(process.env[name], '[REDACTED]');
    return text;
  };
  async function start(id, body = {}, key, namespace = 'explicit') {
    await bridge('task.recover', {isAlive});
    const value = await bridge('task.start', {id, body, key, namespace, ownerId});
    if (value.replayed) {
      const run = await bridge('run.record', {id: value.run.id});
      return {task: {...value.task, status: run.status, updatedAt: run.updatedAt || value.task.updatedAt}, run, replayed: true};
    }
    const {task, run} = value;
    await bridge('task.launching', {id, runId: run.id, ownerId});
    let child;
    try {
      child = spawn(python, [...(python === 'py' ? ['-3.12'] : []), '-m', 'tracefix.service.run', '--request-stdin'],
        {cwd: projectRoot, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'],
          env: {...process.env, TRACEFIX_CONSOLE_RUN_ID: run.id, TRACEFIX_PROCESS_CONTROL_ID: run.processControlId,
            TRACEFIX_DATA: run.dataRoot, TRACEFIX_CONSOLE_DB: run.consoleDatabasePath,
            TRACEFIX_SERVICE_DATA_ROOT: run.dataRoot, TRACEFIX_SERVICE_CONSOLE_DB: run.consoleDatabasePath,
            PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1',
            PYTHONPATH: [path.join(projectRoot, 'backend/packages/agent/src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)}});
    } catch {
      await bridge('task.finish', {id, runId: run.id, ownerId, changes: {status: 'failed', outcome: 'INFRA_FAILURE', finishedAt: new Date().toISOString(),
        error: {code: 'SPAWN_FAILED', message: '执行进程启动失败', retryable: true}}});
      throw Object.assign(failure('SPAWN_FAILED', '执行进程启动失败', 503, true), {taskId: id, runId: run.id});
    }
    let output = '', overflow = false, processError = null, logs = [], flushTimer;
    let resolveDone;
    const done = new Promise(resolve => {resolveDone = resolve;});
    const entry = {child, id, runId: run.id, done, timer: null};
    owned.set(run.id, entry);
    const flush = () => bridge('run.update', {id: run.id, changes: {logs}});
    const capture = chunk => {
      logs = [...logs, redact(chunk).slice(-8000)].slice(-120);
      if (!flushTimer) flushTimer = setTimeout(() => {flushTimer = null; flush().catch(reportError);}, 500);
    };
    child.stderr.on('data', capture);
    child.stdout.on('data', chunk => {
      if (Buffer.byteLength(output) + chunk.length <= 1048576 && !overflow) output += chunk;
      else {output = ''; overflow = true;}
    });
    child.on('error', () => {processError = {code: 'SPAWN_FAILED', message: '执行进程启动失败', retryable: true};});
    child.stdin.on('error', () => {processError = {code: 'EXECUTOR_INPUT_FAILED', message: '执行请求写入失败', retryable: false};});
    child.once('close', async (code, signal) => {
      clearTimeout(flushTimer); clearTimeout(entry.timer);
      try {
        await flush();
        const latest = await bridge('run.record', {id: run.id});
        let result = null;
        try {
          const parsed = JSON.parse(output);
          if (!overflow) result = await bridge('run.executor-result', {id: run.id, result: parsed});
        } catch {}
        const cancelled = latest.status === 'stopping' || latest.stopRequestedAt;
        const changes = {exitCode: code, signal, finishedAt: new Date().toISOString(), processControlId: null,
          stopRequestedAt: null, stopPreviousStatus: null,
          ...(processError ? {status: 'failed', outcome: 'INFRA_FAILURE', error: processError} :
            cancelled ? {status: 'cancelled', outcome: 'CANCELLED', error: {code: 'CANCELLED', message: '执行已取消', retryable: false}} :
              result ? result :
                {status: 'unknown', outcome: 'UNKNOWN', error: {code: 'EXECUTOR_RESULT_MISSING', message: '执行已退出，缺少可确认的最终结果', retryable: false}})};
        await bridge('task.finish', {id, runId: run.id, ownerId, changes});
      } catch (error) {reportError('保存执行结果失败：', error);}
      finally {owned.delete(run.id); resolveDone();}
    });
    try {
      await new Promise((resolve, reject) => {
        child.once('spawn', resolve);
        child.once('error', reject);
      });
      await bridge('task.spawned', {id, runId: run.id, ownerId, pid: child.pid});
      child.stdin.end(JSON.stringify(executionRequest(task, run)));
    } catch {
      if (child.pid) await terminate(child, {isAlive}).catch(reportError);
      await done;
      throw Object.assign(failure('SPAWN_FAILED', '执行进程启动失败', 503, true), {taskId: id, runId: run.id});
    }
    return {task: await bridge('task', {id}), run: await bridge('run', {id: run.id}), replayed: false};
  }
  async function stop(id) {
    if (stopping.has(id)) return stopping.get(id);
    const pending = (async () => {
      const task = await bridge('task', {id});
      if (!task.currentRunId) return {status: 200, value: {task, run: null, replayed: true}};
      const run = await bridge('run.record', {id: task.currentRunId});
      if (!active.has(run.status)) return {status: 200, value: {task, run, replayed: true}};
      if (run.status === 'stopping') return {status: 202, value: {task, run, replayed: true}};
      const entry = owned.get(run.id);
      if (!entry || entry.id !== id) throw failure('STOP_UNAVAILABLE', '当前 API 不拥有该 Task 的执行进程');
      const record = await bridge('run.stop.request', {id: run.id, processControlId: run.processControlId});
      if (record.status === 'stopping') {
        entry.timer = setTimeout(async () => {
          try {
            const latest = await bridge('run.record', {id: run.id});
            if (owned.get(run.id) !== entry || latest.status !== 'stopping' || latest.stopRequestedAt !== record.stopRequestedAt) return;
            await terminate(entry.child, {isAlive});
          } catch (error) {
            await bridge('run.stop.failed', {id: run.id, processControlId: run.processControlId,
              stopRequestedAt: record.stopRequestedAt, error: '停止执行失败'}).catch(reportError);
            reportError(error);
          }
        }, graceMs);
        entry.timer.unref?.();
      }
      return {status: 202, value: {task: await bridge('task', {id}), run: record, replayed: false}};
    })();
    stopping.set(id, pending);
    try {return await pending;} finally {stopping.delete(id);}
  }
  return {start, stop, recover: () => bridge('task.recover', {isAlive}),
    async drain() {await Promise.all([...owned.values()].map(entry => entry.done));},
    async close() {for (const entry of owned.values()) await terminate(entry.child, {isAlive}); await this.drain();}};
}
