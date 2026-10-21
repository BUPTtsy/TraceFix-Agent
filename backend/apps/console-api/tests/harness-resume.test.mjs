import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {build} from 'esbuild';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {createApi} from '../server/api.mjs';
import {createExecutor, executionRequest} from '../server/executor.mjs';

const bundle = await build({stdin: {contents: `export {ConsoleDatabase, controlProjection} from './src/database.ts';
export {TaskLifecycle, normalizeTask} from './src/task-lifecycle.ts';
export {createConsoleService} from './src/dispatch.ts';
export {executorResult} from './src/results.ts';`, resolveDir: path.resolve('backend/packages/console-service')},
  bundle: true, platform: 'node', format: 'esm', write: false});
const {ConsoleDatabase, controlProjection, TaskLifecycle, normalizeTask, createConsoleService, executorResult} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));

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

test('Task 定义拒绝伪造权威 Job 和 Session 字段', () => {
  for (const field of ['harness_job_id', 'harness_session_id', 'harness_task_id', 'harness_stage', 'sdk_execution_id'])
    assert.throws(() => normalizeTask({projectId: 'scope', mode: 'repair', goal: '修复固定缺陷问题',
      execution: {executionMode: 'batch', backend: 'claude_sdk', [field]: 'forged'}}, [{id: 'scope', ready: true}]),
      error => error.code === 'INVALID_EXECUTION');
});

test('控制视图刷新权威记录，恢复先核验 PG 且同键重放不再核验', context => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tf-authority-'));
  const projectsPath = path.join(root, 'projects.yaml'), databasePath = path.join(root, 'console.sqlite3');
  fs.writeFileSync(projectsPath, JSON.stringify({projects: []}));
  let state = 'STOPPED';
  const calls = [];
  const service = createConsoleService({projectsPath, databasePath, dataRoot: root,
    harnessControl(operation, fields) {
      calls.push({operation, fields});
      if (operation === 'resume.check' && state !== 'STOPPED') throw Object.assign(new Error('冲突'), {status: 409, code: 'RESUME_CONFLICT'});
      return {coding_backend: 'claude_sdk', control_status: state, can_stop: state === 'RUNNING',
        can_resume: state === 'STOPPED', resume_blockers: [], harness_job_id: 'authority-job', harness_session_id: 'authority-session'};
    }});
  const database = new ConsoleDatabase(databasePath);
  const now = new Date().toISOString();
  database.updateRun('run', {id: 'run', projectId: 'scope', taskId: 'task', status: 'stopped', coding_backend: 'claude_sdk',
    control_status: 'RUNNING', can_resume: false, dataRoot: root,
    frozenDefinition: {execution: {backend: 'claude_sdk'}}}, true);
  database.db.prepare('INSERT INTO console_tasks VALUES (?,?)').run('task', JSON.stringify({id: 'task',
    definition: {execution: {backend: 'claude_sdk'}}, status: 'stopped', currentRunId: 'run', runIds: ['run'],
    version: 1, createdAt: now, updatedAt: now}));
  context.after(() => {database.db.close(); service.close();});
  assert.equal(service('task', {id: 'task'}).harness_job_id, 'authority-job');
  assert.equal(service('run', {id: 'run'}).can_resume, true);
  const value = service('task.resume', {id: 'task', body: {}, key: 'same', ownerId: 'owner'});
  assert.equal(value.run.id, 'run');
  assert.equal(calls.at(-1).operation, 'resume.check');
  assert.deepEqual(calls.at(-1).fields, {task_id: 'task', scope_id: 'scope', run_id: 'run', job_id: 'authority-job'});
  state = 'RUNNING';
  const count = calls.length;
  assert.equal(service('task.resume', {id: 'task', body: {}, key: 'same', ownerId: 'owner'}).replayed, true);
  assert.equal(calls.length, count);
  assert.throws(() => service('task.resume', {id: 'task', body: {}, key: 'other', ownerId: 'owner'}),
    error => error.code === 'RESUME_CONFLICT');
});

test('SDK executor 用恢复入口并通过权威桥停止，收到 STOPPED 才标记恢复能力', async () => {
  let task = {id: 'task', currentRunId: 'run', definition: {projectId: 'scope', mode: 'repair', goal: '修复固定缺陷', execution: {backend: 'claude_sdk'}}};
  let run = {id: 'run', projectId: 'scope', taskId: 'task', status: 'stopped', coding_backend: 'claude_sdk', control_status: 'STOPPED'};
  const calls = [];
  const bridge = async (operation, fields) => {
    calls.push(operation);
    if (operation === 'task.resume') {run = {...run, status: 'starting', control_status: 'STARTING'}; return {task, run, replayed: false};}
    if (operation === 'task') return task;
    if (operation === 'run' || operation === 'run.record') return run;
    if (operation === 'run.harness.stop') {run = {...run, control_status: 'STOPPING'}; return run;}
    if (operation === 'run.control') return {...run, control_status: 'STOPPED', can_resume: true, can_stop: false, resume_blockers: []};
    if (operation === 'task.finish') {run = {...run, ...fields.changes}; return {task, run};}
  };
  let child, args, input = '', killed = false;
  const controller = createExecutor({bridge, projectRoot: process.cwd(), pythonCommand: 'test-python',
    spawn(command, arguments_) {
      args = arguments_; child = new EventEmitter(); child.pid = 12345;
      child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
      child.stdin.on('data', chunk => {input += chunk;});
      child.kill = () => {killed = true;};
      process.nextTick(() => child.emit('spawn'));
      return child;
    }, terminate: async () => {killed = true;}, reportError: error => {throw error;}});
  await controller.resume('task', {}, 'resume');
  assert.ok(args.includes('--resume-stdin'));
  assert.equal(JSON.parse(input).execution.backend, 'claude_sdk');
  assert.equal(Object.hasOwn(JSON.parse(input), 'resume'), false);
  assert.equal((await controller.stop('task')).status, 202);
  assert.equal(killed, false);
  assert.ok(calls.includes('run.harness.stop'));
  child.emit('close', 0, null);
  await controller.drain();
  assert.equal(run.status, 'stopped');
  assert.equal(run.can_resume, true);
});

test('SDK 恢复重放首次冻结定义，Task 后续编辑不能注入新目标或指令', context => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tf-frozen-request-'));
  const database = new ConsoleDatabase(path.join(root, 'console.sqlite3'));
  context.after(() => database.db.close());
  const lifecycle = new TaskLifecycle(database, fields => fields, (fields, id) => ({id, projectId: fields.projectId,
    status: 'running', coding_backend: 'claude_sdk', repositorySnapshot: {frozen: id}}));
  const definition = {projectId: 'scope', mode: 'repair', goal: '修复首次冻结目标', execution: {backend: 'claude_sdk', executionMode: 'batch'},
    additionalRuleIds: ['original-rule'], source_finding_ids: [], github_reads: []};
  const created = lifecycle.create(definition);
  const started = lifecycle.start(created.task.id, {}, 'start', 'owner');
  const original = executionRequest(started.task, started.run);
  lifecycle.finish(created.task.id, started.run.id, 'owner', {status: 'stopped', control_status: 'STOPPED',
    can_resume: true, resume_blockers: [], harness_job_id: 'job'});
  const changed = lifecycle.update(created.task.id, {...definition, goal: '替换为其他目标', mode: 'test',
    execution: {backend: 'legacy', spec: {goal: '注入新规范'}}, additionalRuleIds: ['injected-rule'],
    github_reads: [{source_kind: 'issue', number: 99}], source_finding_ids: ['injected-finding']});
  assert.throws(() => lifecycle.start(created.task.id, {}, 'restart', 'owner'), error => error.code === 'SDK_NEW_TASK_REQUIRED');
  const resumed = lifecycle.resume(created.task.id, {}, 'resume', 'owner');
  assert.equal(changed.definition.goal, '替换为其他目标');
  assert.deepEqual(executionRequest(resumed.task, resumed.run), original);
  lifecycle.finish(created.task.id, started.run.id, 'owner', {status: 'stopped', control_status: 'STOPPED', can_resume: true,
    frozenDefinition: null});
  assert.throws(() => lifecycle.resume(created.task.id, {}, 'missing', 'owner'), error => error.code === 'RESUME_DEFINITION_MISSING');
});

test('SDK 终态以原控制台 Run 输出并绑定 PG 当前增量、Session 与权威报告', () => {
  const patch = Buffer.from('diff --git a/src/value.ts b/src/value.ts\n');
  const report = {run_id: 'increment', scope_id: 'scope', run_status: 'COMPLETED', outcome: 'FIX_VERIFIED',
    patch_diff_ref: 'patch.diff', patch_verification: 'verified', patch_available: true, repair_commit: 'commit'};
  const run = {id: 'console', taskId: 'task', projectId: 'scope', coding_backend: 'claude_sdk', agentRunId: 'increment',
    harness_job_id: 'job', harness_session_id: 'session', control_status: 'COMPLETED', reportRef: 'report.json'};
  const value = {schema_version: 1, task_id: 'task', run_id: 'console', repo_id: 'scope', terminal: true,
    current_run_id: 'increment', coding_backend: 'claude_sdk', harness_job_id: 'job', harness_session_id: 'session',
    control_status: 'COMPLETED', run_status: 'COMPLETED', outcome: 'FIX_VERIFIED', report_ref: 'report.json', report,
    patch_diff_ref: 'patch.diff', diff: patch.toString(), patch_available: true, patch_verification: 'verified',
    summary: null, branch: null, commit: 'commit', publication: null};
  const view = {issueReport: report};
  assert.equal(executorResult(value, run, view, patch).status, 'completed');
  for (const field of ['run_id', 'current_run_id', 'harness_job_id', 'harness_session_id', 'control_status'])
    assert.throws(() => executorResult({...value, [field]: 'foreign'}, run, view, patch),
      error => error.code === 'EXECUTOR_RESULT_INVALID', field);
  assert.throws(() => executorResult(value, {...run, control_status: 'RUNNING'}, view, patch),
    error => error.code === 'EXECUTOR_RESULT_INVALID');
  for (const field of ['run_id', 'scope_id', 'outcome'])
    assert.throws(() => executorResult(value, run, {issueReport: {...report, [field]: 'foreign'}}, patch),
      error => error.code === 'EXECUTOR_RESULT_INVALID', field);
  assert.throws(() => executorResult(value, run, view, Buffer.from('changed patch')),
    error => error.code === 'EXECUTOR_RESULT_INVALID');
});
