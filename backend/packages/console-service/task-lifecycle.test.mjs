import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {spawn} from 'node:child_process';

const directory = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({stdin: {contents: "export * from './src/task-lifecycle.ts'; export * from './src/database.ts';",
  resolveDir: directory}, bundle: true, platform: 'node', format: 'esm', write: false});
const {ConsoleDatabase, TaskLifecycle, normalizeTask} = await import('data:text/javascript;base64,' +
  Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const input = {repo_id: 'repo', mode: 'test', goal: '测试受控项目', execution: {executionMode: 'batch'}};
const projects = [{id: 'alpha', repoId: 'repo', ready: true}];

test('Task 修复模式保持可选、校验非法值并透传执行定义', context => {
  const {tasks, store} = fixture(context);
  assert.equal(normalizeTask(input, projects).execution.repairMode, undefined);
  for (const repairMode of ['loop', 'HYBRID', null, 1, true])
    assert.throws(() => normalizeTask({...input, execution: {...input.execution, repairMode}}, projects),
      error => error.code === 'INVALID_REPAIR_MODE' && error.status === 400);
  for (const repairMode of ['hybrid', 'pipeline']) {
    const {task} = tasks.create({...input, mode: 'repair', execution: {...input.execution, repairMode}});
    const started = tasks.start(task.id, {}, undefined, 'owner');
    assert.equal(started.run.execution.repairMode, repairMode);
    assert.equal(store.run(started.run.id).execution.repairMode, repairMode);
    tasks.finish(task.id, started.run.id, 'owner', {status: 'completed'});
  }
});

function fixture(context) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-beta-task-'));
  const stores = [];
  context.after(() => {for (const store of stores) store.db.close();});
  const open = () => {
    const store = new ConsoleDatabase(path.join(root, 'console.sqlite3'));
    stores.push(store);
    const tasks = new TaskLifecycle(store, fields => normalizeTask(fields, projects), (fields, id) =>
      ({id, projectId: fields.projectId, status: 'running', startedAt: new Date().toISOString(), outcome: null,
        repositorySnapshot: {commit: 'a'.repeat(40), fixture: true}, logs: []}));
    return {store, tasks};
  };
  return {open, ...open()};
}

test('Task 创建不运行；终态编辑后 start 新建 Run 并保留历史', context => {
  const {store, tasks} = fixture(context);
  const {task} = tasks.create(input, 'create');
  assert.equal(task.definition.projectId, 'alpha');
  assert.equal(task.status, 'created');
  assert.deepEqual(store.runs(), []);
  const first = tasks.start(task.id, {}, 'first', 'owner');
  assert.equal(tasks.task(task.id).status, 'starting');
  assert.throws(() => tasks.update(task.id, input), error => error.code === 'TASK_ACTIVE');
  tasks.spawned(task.id, first.run.id, 'owner', 1234);
  tasks.finish(task.id, first.run.id, 'owner', {status: 'failed', outcome: 'INFRA_FAILURE'});
  assert.equal(tasks.update(task.id, {...input, goal: '第二轮执行目标'}).status, 'created');
  const second = tasks.start(task.id, {}, 'second', 'owner');
  assert.notEqual(first.run.id, second.run.id);
  assert.equal(store.run(first.run.id).outcome, 'INFRA_FAILURE');
  assert.deepEqual(tasks.task(task.id).runIds, [first.run.id, second.run.id]);
});

test('创建与 start 持久幂等、正文冲突、跨连接重启和 busy', async context => {
  const {tasks, open} = fixture(context);
  const other = open().tasks;
  const created = await Promise.all([Promise.resolve().then(() => tasks.create(input, 'key')),
    Promise.resolve().then(() => other.create(input, 'key'))]);
  assert.equal(created[0].task.id, created[1].task.id);
  assert.equal(other.tasks().length, 1);
  assert.throws(() => other.create({...input, goal: '正文不同'}, 'key'), error => error.code === 'IDEMPOTENCY_CONFLICT');
  const taskId = created[0].task.id;
  const started = tasks.start(taskId, {}, 'start', 'owner');
  assert.equal(other.start(taskId, {}, 'start', 'other').run.id, started.run.id);
  assert.equal(other.start(taskId, {}, 'start', 'other').replayed, true);
  assert.throws(() => other.start(taskId, {extra: true}, 'start', 'other'), error => error.code === 'IDEMPOTENCY_CONFLICT');
  const next = tasks.create(input).task;
  assert.throws(() => other.start(next.id, {}, undefined, 'other'), error => error.code === 'EXECUTOR_BUSY' && error.retryable);
  assert.equal(open().tasks.create(input, 'key').task.id, taskId);
  assert.equal(open().tasks.start(taskId, {}, 'start', 'restart').run.id, started.run.id);
});

test('启动预留期间崩溃为 UNKNOWN；同键不重新执行', context => {
  const {tasks, store, open} = fixture(context);
  const {task} = tasks.create(input);
  const started = tasks.start(task.id, {}, 'reserved', 'dead-owner');
  store.db.prepare('UPDATE console_executor SET owner_pid=-1 WHERE slot=1').run();
  const restarted = open().tasks;
  assert.equal(restarted.recover(() => false), null);
  assert.equal(store.run(started.run.id).outcome, 'UNKNOWN');
  assert.equal(store.run(started.run.id).error.retryable, false);
  assert.equal(restarted.start(task.id, {}, 'reserved', 'new-owner').run.id, started.run.id);
  assert.equal(store.runs().length, 1);
  assert.notEqual(restarted.start(task.id, {}, 'new', 'new-owner').run.id, started.run.id);
});

test('API owner 退出但执行进程仍存活时保持单执行器预留', context => {
  const {tasks, store} = fixture(context);
  const {task} = tasks.create(input);
  const started = tasks.start(task.id, {}, 'reserved', 'owner');
  tasks.spawned(task.id, started.run.id, 'owner', 1234);
  store.db.prepare('UPDATE console_executor SET owner_pid=-1 WHERE slot=1').run();
  assert.ok(tasks.recover(pid => pid === 1234));
  assert.throws(() => tasks.start(tasks.create(input).task.id, {}, undefined, 'other'), error => error.code === 'EXECUTOR_BUSY');
  assert.throws(() => tasks.update(task.id, input), error => error.code === 'TASK_ACTIVE');
  tasks.recover(() => false);
  assert.equal(tasks.lease(), null);
});

test('spawn 与 PID 落盘间崩溃不能自动释放无法确认的执行器', context => {
  const {tasks, store} = fixture(context);
  const {task} = tasks.create(input);
  const started = tasks.start(task.id, {}, 'launching', 'owner');
  tasks.launchIntent(task.id, started.run.id, 'owner');
  store.db.prepare('UPDATE console_executor SET owner_pid=-1 WHERE slot=1').run();
  assert.ok(tasks.recover(() => false));
  assert.equal(store.run(started.run.id).outcome, 'UNKNOWN');
  assert.equal(store.run(started.run.id).error.retryable, false);
  assert.throws(() => tasks.start(task.id, {}, 'next', 'other'), error => error.code === 'EXECUTOR_BUSY' && error.retryable === false);
});

test('两个 API 进程竞争同 start 幂等键；进程退出后重建保留绑定', async context => {
  const {tasks, store} = fixture(context);
  const {task} = tasks.create(input, 'process-create');
  const execute = phase => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(directory, 'fixtures/task-process.mjs')], {windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
    let output = '', errors = '';
    child.stdout.on('data', chunk => {output += chunk;}); child.stderr.on('data', chunk => {errors += chunk;});
    child.once('error', reject);
    child.once('close', code => {if (code) reject(new Error(errors)); else {try {resolve(JSON.parse(output));} catch (error) {reject(error);}}});
    child.stdin.end(JSON.stringify({databasePath: store.filename, taskId: task.id, key: 'process-start', phase}));
  });
  const started = await Promise.all([execute('reserved'), execute('reserved')]);
  assert.equal(started[0].run.id, started[1].run.id);
  assert.equal(store.runs().length, 1);
  tasks.recover(pid => {try {process.kill(pid, 0); return true;} catch {return false;}});
  assert.equal(tasks.task(task.id).status, 'unknown');
  assert.equal(store.run(started[0].run.id).error.retryable, false);
  assert.equal((await execute('reserved')).run.id, started[0].run.id);
});

test('Task 正文拒绝执行权限注入、交互模式和不完整验收绑定', () => {
  const normalize = fields => normalizeTask(fields, projects);
  for (const field of ['repository_snapshot', 'repositorySnapshot', 'profile', 'commands', 'credential_ref', 'env', 'dataRoot'])
    assert.throws(() => normalize({...input, [field]: 'forged'}), error => error.status === 400, field);
  assert.throws(() => normalize({...input, execution: {executionMode: 'interactive'}}), error => error.code === 'UNSUPPORTED_EXECUTION_MODE');
  assert.throws(() => normalize({...input, execution: {executionMode: 'batch', url: 'https://user:pass@example.com'}}), error => error.code === 'INVALID_URL');
  assert.throws(() => normalize({...input, github_reads: [{source_kind: 'file', path: 'src/App.tsx', command: 'sh'}]}), error => error.code === 'INVALID_GITHUB_READ');
  assert.throws(() => normalize({...input, finding_acceptance: {schema_version: 1}}), error => error.status === 400);
  assert.throws(() => normalize({...input, projectId: 'different'}), error => error.code === 'PROJECT_NOT_FOUND' && error.status === 404);
});

test('项目缺失为 404；格式错误、别名冲突与歧义保持 400', () => {
  const catalog = [...projects, {id: 'beta', repoId: 'second', ready: true}];
  for (const fields of [{...input, repo_id: 'absent'}, {...input, projectId: 'absent'}])
    assert.throws(() => normalizeTask(fields, catalog), error => error.code === 'PROJECT_NOT_FOUND' && error.status === 404);
  for (const fields of [{...input, repo_id: ''}, {...input, repo_id: 123}, {...input, projectId: 'beta'}])
    assert.throws(() => normalizeTask(fields, catalog), error => error.code === 'INVALID_PROJECT' && error.status === 400);
  assert.throws(() => normalizeTask(input, [...catalog, {id: 'repo', repoId: 'third', ready: true}]),
    error => error.code === 'INVALID_PROJECT' && error.status === 400);
  assert.throws(() => normalizeTask(input, [{...projects[0], ready: false}]),
    error => error.code === 'PROJECT_NOT_READY' && error.status === 409);
  assert.equal(normalizeTask({...input, projectId: 'alpha', repositoryId: 'repo'}, catalog).projectId, 'alpha');
});
