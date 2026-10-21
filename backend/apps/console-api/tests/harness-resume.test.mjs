import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {build} from 'esbuild';
import {createApi} from '../server/api.mjs';

const bundle = await build({stdin: {contents: `export {ConsoleDatabase, controlProjection} from './src/database.ts';
export {TaskLifecycle} from './src/task-lifecycle.ts';`, resolveDir: path.resolve('backend/packages/console-service')},
  bundle: true, platform: 'node', format: 'esm', write: false});
const {ConsoleDatabase, controlProjection, TaskLifecycle} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));

test('resume HTTP 正文严格为空对象，未知提示字段返回 400', async context => {
  const calls = [];
  const app = await createApi({token: 'resume-test', bridge: async () => undefined,
    controller: {resume: async (id, body, key) => {calls.push({id, body, key});
      return {task: {id, definition: {}, status: 'running', version: 1, currentRunId: 'run', runIds: ['run'],
        createdAt: new Date().toISOString(), updatedAt: new Date().toISOString()}, run: {id: 'run', projectId: 'scope', status: 'running'}, replayed: false};}}});
  context.after(() => app.close());
  const headers = {authorization: 'Bearer resume-test', 'idempotency-key': 'same'};
  for (const field of ['prompt', 'instruction', 'goal', 'anything']) {
    const response = await app.inject({method: 'POST', url: '/api/v1/tasks/task/resume', headers, payload: {[field]: 'inject'}});
    assert.equal(response.statusCode, 400, response.body);
  }
  const accepted = await app.inject({method: 'POST', url: '/api/v1/tasks/task/resume', headers, payload: {}});
  assert.equal(accepted.statusCode, 202, accepted.body);
  assert.deepEqual(calls, [{id: 'task', body: {}, key: 'same'}]);
});

test('恢复事务保留 Run 并按幂等键防重，仅权威 STOPPED 可以继续', context => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-harness-resume-'));
  const database = new ConsoleDatabase(path.join(root, 'console.sqlite3'));
  context.after(() => database.db.close());
  const lifecycle = new TaskLifecycle(database, fields => fields, (fields, id) => ({id, projectId: 'scope',
    status: 'running', startedAt: new Date().toISOString(), coding_backend: 'claude_sdk', ...fields}));
  const created = lifecycle.create({execution: {backend: 'claude_sdk'}});
  const started = lifecycle.start(created.task.id, {}, 'start', 'owner');
  lifecycle.finish(created.task.id, started.run.id, 'owner', {status: 'cancelled'});
  assert.throws(() => lifecycle.resume(created.task.id, {}, 'resume', 'owner'), error => error.code === 'RESUME_CONFLICT');
  database.updateRun(started.run.id, {control_status: 'STOPPED', can_resume: true, resume_blockers: []});
  const resumed = lifecycle.resume(created.task.id, {}, 'resume', 'owner');
  assert.equal(resumed.run.id, started.run.id);
  assert.deepEqual(resumed.task.runIds, [started.run.id]);
  assert.equal(lifecycle.resume(created.task.id, {}, 'resume', 'owner').replayed, true);
  assert.throws(() => lifecycle.resume(created.task.id, {prompt: 'inject'}, 'resume', 'owner'), error => error.code === 'IDEMPOTENCY_CONFLICT');
  assert.throws(() => lifecycle.resume(created.task.id, {}, 'other', 'owner'), error => error.code === 'TASK_ACTIVE');
});

test('控制投影不从 cancelled/failed/UNKNOWN 猜测恢复能力', () => {
  for (const status of ['cancelled', 'failed', 'unknown', 'blocked', 'running', 'stopping'])
    assert.equal(controlProjection({status, coding_backend: 'claude_sdk'}).can_resume, false);
  assert.equal(controlProjection({status: 'stopped', coding_backend: 'claude_sdk', control_status: 'STOPPED', can_resume: true}).can_resume, true);
  assert.equal(controlProjection({status: 'stopped', coding_backend: 'claude_sdk', control_status: 'STOPPED', can_resume: true, resume_blockers: ['UNKNOWN_OPERATION']}).can_resume, false);
});
