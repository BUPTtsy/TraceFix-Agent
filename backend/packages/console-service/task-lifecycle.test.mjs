import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {spawn} from 'node:child_process';
import Ajv from 'ajv';
import {testSpec} from '../../apps/console-api/server/schemas.mjs';

const directory = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({stdin: {contents: "export * from './src/task-lifecycle.ts'; export * from './src/database.ts';",
  resolveDir: directory}, bundle: true, platform: 'node', format: 'esm', write: false});
const {ConsoleDatabase, TaskLifecycle, normalizeTask} = await import('data:text/javascript;base64,' +
  Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const input = {repo_id: 'repo', mode: 'test', goal: '测试受控项目', execution: {executionMode: 'batch'}};
const projects = [{id: 'alpha', repoId: 'repo', ready: true}];

test('非 GUI TestSpec 冻结命令目标并拒绝混用浏览器计划，GUI 仍须双断言', () => {
  const validate = new Ajv({strict: false}).compile(testSpec);
  const normalize = spec => normalizeTask({...input, execution: {...input.execution, spec}}, projects);
  for (const mode of ['unit', 'static', 'build']) {
    const spec = {mode, goal: '修复冻结命令的失败'};
    assert.equal(validate(spec), true, JSON.stringify(validate.errors));
    assert.deepEqual(normalize(spec).execution.spec, spec);
    for (const key of ['assertions', 'regression_assertions', 'regression_plan',
      'executable_preconditions', 'behavior_scenarios', 'authorized_actions']) {
      const mixed = {...spec, [key]: key === 'authorized_actions' ? ['click'] : [{}]};
      assert.equal(validate(mixed), false, key);
      assert.throws(() => normalize(mixed), error => error.code === 'INVALID_SPEC', key);
    }
  }
  assert.equal(validate({goal: '旧 GUI 仍须双断言'}), false);
  assert.throws(() => normalize({goal: '旧 GUI 仍须双断言'}), error => error.code === 'INVALID_SPEC');
  const assertion = {locator: {role: 'status', name: '已保存'}};
  assert.equal(validate({goal: '旧 GUI 保持现有兼容', assertions: [assertion], regression_assertions: [assertion]}), true);
});

test('legacy 后端与 repairMode 被拒绝；repair 归一化为 claude_sdk，test 不带后端', context => {
  const {tasks, store} = fixture(context);
  const repair = {...input, mode: 'repair'};
  for (const backend of ['legacy', 'other', null, 1])
    for (const key of ['backend', 'coding_backend'])
      assert.throws(() => normalizeTask({...repair, execution: {...input.execution, [key]: backend}}, projects),
        error => error.code === 'INVALID_CODING_BACKEND' && error.status === 400);
  for (const repairMode of ['hybrid', 'pipeline', 'loop', null])
    assert.throws(() => normalizeTask({...repair, execution: {...input.execution, repairMode}}, projects),
      error => error.code === 'INVALID_EXECUTION' && error.status === 400);
  for (const key of ['backend', 'coding_backend'])
    assert.throws(() => normalizeTask({...input, execution: {...input.execution, [key]: 'claude_sdk'}}, projects),
      error => error.code === 'INVALID_CODING_BACKEND' && error.status === 400);
  assert.equal(normalizeTask(repair, projects).execution.backend, 'claude_sdk');
  assert.equal(normalizeTask({...repair, execution: {...input.execution, coding_backend: 'claude_sdk'}}, projects).execution.coding_backend, undefined);
  assert.equal(Object.hasOwn(normalizeTask(input, projects).execution, 'backend'), false);
  const {task} = tasks.create(repair);
  const started = tasks.start(task.id, {}, undefined, 'owner');
  assert.equal(started.run.execution.backend, 'claude_sdk');
  assert.equal(Object.hasOwn(store.run(started.run.id).execution, 'repairMode'), false);
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

test('交付审批：校验前置条件、写入决定、幂等重放；waiting_approval 不阻塞执行器', context => {
  const {tasks, store} = fixture(context);
  const {task} = tasks.create({...input, mode: 'repair'});
  const first = tasks.start(task.id, {}, undefined, 'owner');
  store.updateRun(first.run.id, {coding_backend: 'claude_sdk', harness_job_id: 'job', harness_session_id: 'session'});
  const approval = {id: 'approval_1', status: 'pending', job_id: 'job', accepted_commit: 'a'.repeat(40), patch_hash: 'h',
    publish_branch: false, diff_ref: 'd.diff', requested_at: new Date().toISOString()};
  assert.throws(() => tasks.approve(task.id, {approval_id: 'approval_1', decision: 'approve'}, 'k', 'owner'),
    error => error.code === 'APPROVAL_UNAVAILABLE' && error.status === 409);
  tasks.finish(task.id, first.run.id, 'owner', {status: 'waiting_approval', outcome: 'FIX_VERIFIED', approval, delivery_diff: 'diff'});
  assert.equal(tasks.lease(), null);
  assert.equal(tasks.task(task.id).status, 'waiting_approval');
  assert.throws(() => tasks.start(task.id, {}, undefined, 'owner'), error => error.code === 'TASK_ACTIVE');
  assert.throws(() => tasks.update(task.id, {...input, mode: 'repair'}), error => error.code === 'TASK_ACTIVE');
  // waiting_approval 且无进程：其他 Task 仍可使用执行器；有进程时才占用。
  tasks.assertIdle();
  store.updateRun(first.run.id, {pid: 4321});
  assert.throws(() => tasks.assertIdle(), error => error.code === 'EXECUTOR_BUSY');
  store.updateRun(first.run.id, {pid: null});
  const other = tasks.create(input).task;
  const otherStarted = tasks.start(other.id, {}, undefined, 'owner');
  tasks.finish(other.id, otherStarted.run.id, 'owner', {status: 'completed'});
  for (const body of [{}, {approval_id: 'approval_1'}, {approval_id: 'approval_1', decision: 'maybe'},
    {approval_id: 'approval_1', decision: 'approve', extra: 1}, {approval_id: 7, decision: 'approve'}])
    assert.throws(() => tasks.approve(task.id, body, undefined, 'owner'), error => error.code === 'INVALID_APPROVAL' && error.status === 400);
  assert.throws(() => tasks.approve(task.id, {approval_id: 'other', decision: 'approve'}, undefined, 'owner'),
    error => error.code === 'APPROVAL_MISMATCH' && error.status === 409);
  const body = {approval_id: 'approval_1', decision: 'reject'};
  const approved = tasks.approve(task.id, body, 'approve-key', 'owner');
  assert.equal(approved.run.status, 'starting');
  assert.equal(approved.task.status, 'starting');
  assert.equal(approved.run.approval.decision, 'reject');
  assert.equal(approved.run.approval.decided_by, 'web');
  assert.equal(approved.run.approval.status, 'pending');
  assert.ok(approved.run.approval.decided_at);
  assert.equal(tasks.lease().run_id, first.run.id);
  assert.equal(tasks.approve(task.id, body, 'approve-key', 'owner').replayed, true);
  assert.equal(tasks.checkApproval(task.id, body, 'approve-key').run.id, first.run.id);
  assert.throws(() => tasks.approve(task.id, {...body, decision: 'approve'}, 'approve-key', 'owner'), error => error.code === 'IDEMPOTENCY_CONFLICT');
  assert.throws(() => tasks.approve(task.id, body, 'second-key', 'owner'), error => error.code === 'APPROVAL_UNAVAILABLE');
});
