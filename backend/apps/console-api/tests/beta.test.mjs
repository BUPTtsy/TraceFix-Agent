import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';
import {createApi} from '../server/api.mjs';
import {createExecutor} from '../server/executor.mjs';

const projectRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const bundle = await build({entryPoints: [path.join(projectRoot, 'backend/packages/console-service/src/dispatch.ts')],
  bundle: true, platform: 'node', format: 'esm', write: false});
const {createConsoleService} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const spec = {goal: '检查页面保存反馈', assertions: [{locator: {role: 'status', name: 'Saved'}, condition: 'visible'}],
  regression_assertions: [{locator: {role: 'status', name: 'Saved'}, condition: 'visible'}]};
const body = {projectId: 'alpha', mode: 'test', goal: '测试页面输入行为', execution: {executionMode: 'batch', url: 'http://app:3000', spec}};
const headers = {authorization: 'Bearer beta-test-token'};

function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8', windowsHide: true});
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

function fakeExecutor() {
  const calls = [];
  let failSpawn = false;
  function spawn(command, args, options) {
    const child = new EventEmitter();
    child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    child.pid = failSpawn ? undefined : 91000 + calls.length;
    child.kill = () => true;
    child.exitCode = null; child.signalCode = null;
    const call = {command, args, options, child, request: null};
    calls.push(call);
    let input = '';
    child.stdin.on('data', chunk => {input += chunk;});
    child.stdin.on('finish', () => {call.request = JSON.parse(input);});
    child.finish = (result, code = 0) => {
      if (result !== undefined) child.stdout.write(JSON.stringify(result));
      child.exitCode = code; child.emit('close', code, null);
    };
    process.nextTick(() => {
      if (failSpawn) {child.emit('error', new Error('private deployment diagnostics')); child.finish(undefined, -1);}
      else child.emit('spawn');
    });
    return child;
  }
  return {calls, spawn, fail() {failSpawn = true;},
    isAlive: pid => pid === process.pid || calls.some(call => call.child.pid === pid && call.child.exitCode === null),
    terminate: async child => child.finish(undefined, null)};
}

async function fixture(context) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-beta-api-'));
  const projectsPath = path.join(root, 'profiles', 'projects.yaml');
  fs.mkdirSync(path.dirname(projectsPath), {recursive: true});
  fs.writeFileSync(projectsPath, JSON.stringify({projects: []}));
  const dataRoot = path.join(root, 'data'), databasePath = path.join(dataRoot, 'console.sqlite3');
  const serviceOptions = {projectsPath, dataRoot, databasePath, engineRoot: projectRoot,
    pythonCommand: process.platform === 'win32' ? 'py' : 'python3', repositoryRoots: [root]};
  const service = createConsoleService(serviceOptions);
  const target = path.join(root, 'checkout');
  fs.mkdirSync(path.join(target, 'src'), {recursive: true});
  fs.writeFileSync(path.join(target, 'src', 'App.tsx'), 'export const App = () => null;\n');
  git(target, 'init'); git(target, 'config', 'user.name', 'TraceFix beta'); git(target, 'config', 'user.email', 'beta@example.invalid');
  git(target, 'add', 'src/App.tsx'); git(target, 'commit', '-m', '接口测试初始版本');
  git(target, 'remote', 'add', 'origin', 'https://github.com/fixture/alpha.git');
  service('repository.create', {repo_id: 'alpha', projectId: 'alpha', root: target, owner: 'fixture', repo: 'alpha',
    default_ref: 'HEAD', allowed_files: ['src/**'], allowed_urls: ['http://app:3000'], credential_ref: 'fixture_ref', enabled: true,
    profile: {url: 'http://app:3000', allowed_origins: ['http://app:3000'],
      commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', 'fixture.js']]))}});
  const bridge = (operation, fields) => Promise.resolve().then(() => service(operation, fields));
  const fake = fakeExecutor();
  const controller = createExecutor({bridge, projectRoot, ...fake, graceMs: 1,
    pythonCommand: 'fixture-python', reportError: error => {throw error;}});
  const app = await createApi({bridge, controller, token: 'beta-test-token', logger: process.env.TRACEFIX_BETA_TEST_LOG === '1'});
  context.after(async () => {await controller.close(); await app.close(); service.close();});
  const request = (method, url, payload, key) => app.inject({method, url, headers: {...headers, ...(key ? {'idempotency-key': key} : {})}, payload});
  const create = async (payload = body, key) => {
    const response = await request('POST', '/api/v1/tasks', payload, key);
    assert.equal(response.statusCode, 201, response.body);
    return response.json();
  };
  const finish = async (started, outcome = 'FIX_VERIFIED', settled = true) => {
    const call = fake.calls.find(item => item.request?.run_id === started.run.id);
    const runDirectory = path.join(dataRoot, 'artifacts', 'alpha', started.run.id);
    fs.mkdirSync(runDirectory, {recursive: true});
    const index = {};
    const artifact = (ref, value, label) => {
      const bytes = Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
      fs.writeFileSync(path.join(runDirectory, ref), bytes);
      index[ref] = {'用途': label, '字节数': bytes.length, SHA256: createHash('sha256').update(bytes).digest('hex')};
    };
    const diff = '--- a/src/App.tsx\n+++ b/src/App.tsx\n@@ -1 +1 @@\n-export const App = () => null;\n+export const App = () => "fixed";\n';
    const patchRef = outcome === 'FIX_VERIFIED' ? '0002_patch.diff' : null;
    if (patchRef) artifact(patchRef, diff, '收尾_补丁');
    const runStatus = settled ? 'COMPLETED' : 'ABNORMAL';
    const report = {run_id: started.run.id, scope_id: 'alpha', run_status: runStatus, outcome,
      patch_diff_ref: patchRef, patch_available: Boolean(patchRef), patch_verification: patchRef ? 'verified' : 'none',
      check_summary: {total: 1, passed: 1}, check_results: [], overall_status: 'PASSED'};
    artifact('0001_report.json', report, '收尾_修复报告数据');
    fs.writeFileSync(path.join(runDirectory, '文件索引.json'), JSON.stringify(index));
    const database = new DatabaseSync(databasePath);
    const record = JSON.parse(database.prepare('SELECT data FROM console_runs WHERE id=?').get(started.run.id).data);
    database.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify({...record,
      agentRunId: started.run.id, reportRef: '0001_report.json', status: runStatus.toLowerCase(), outcome}), started.run.id);
    database.close();
    call.child.finish({schema_version: 1, task_id: started.task.id, run_id: started.run.id, repo_id: 'alpha',
      run_status: runStatus, outcome, terminal: true, report_ref: '0001_report.json', report,
      patch_diff_ref: patchRef, diff: patchRef ? diff : null, patch_available: Boolean(patchRef),
      patch_verification: patchRef ? 'verified' : 'none', error: null});
    await controller.drain();
  };
  return {root, target, dataRoot, databasePath, service, serviceOptions, bridge, app, controller, fake, request, create, finish};
}

test('Fastify Task lifecycle: 创建不执行、start、活动编辑、终态编辑、新 Run 和历史查询', async context => {
  const {fake, request, create, finish, target} = await fixture(context);
  const created = await create();
  assert.equal(created.task.status, 'created'); assert.equal(created.run, null); assert.equal(fake.calls.length, 0);
  assert.equal((await request('GET', '/api/v1/tasks')).json().length, 1);
  const start = await request('POST', '/api/v1/tasks/' + created.task.id + '/start', {}, 'start');
  assert.equal(start.statusCode, 202, start.body);
  const started = start.json();
  assert.equal(started.task.status, 'running'); assert.equal(fake.calls.length, 1);
  const call = fake.calls[0];
  assert.equal(call.command, 'fixture-python');
  assert.deepEqual(call.args, ['-m', 'tracefix.service.run', '--request-stdin']);
  assert.deepEqual(call.options.stdio, ['pipe', 'pipe', 'pipe']);
  assert.equal(call.request.repository_snapshot.commit, git(target, 'rev-parse', 'HEAD'));
  assert.equal(call.request.repository_snapshot.repo_id, 'alpha');
  assert.equal(call.request.run_id, started.run.id);
  assert.deepEqual(call.request.execution, body.execution);
  assert.equal((await request('PUT', '/api/v1/tasks/' + created.task.id, body)).statusCode, 409);
  const pending = await request('GET', '/api/v1/runs/' + started.run.id + '/result');
  assert.equal(pending.statusCode, 202); assert.equal(pending.json().error.code, 'RESULT_NOT_READY');
  assert.equal(pending.json().settled, false);
  await finish(started);
  const result = (await request('GET', '/api/v1/runs/' + started.run.id + '/result')).json();
  assert.equal(result.outcome, 'FIX_VERIFIED'); assert.equal(result.settled, true);
  assert.equal((await request('GET', '/api/v1/tasks/' + created.task.id)).json().status, 'completed');
  assert.equal((await request('PUT', '/api/v1/tasks/' + created.task.id, {...body, goal: '新一轮测试目标'})).json().status, 'created');
  const second = (await request('POST', '/api/v1/tasks/' + created.task.id + '/start', {}, 'second')).json();
  assert.notEqual(second.run.id, started.run.id);
  assert.equal((await request('GET', '/api/v1/runs/' + started.run.id + '/result')).json().outcome, 'FIX_VERIFIED');
  await finish(second);
});

test('并发创建/start、响应重放、正文冲突和 API 重建均只绑定一个 Run', async context => {
  const {request, create, fake, bridge, controller, finish, serviceOptions} = await fixture(context);
  const responses = await Promise.all(Array.from({length: 8}, () => request('POST', '/api/v1/tasks', body, 'create')));
  assert.ok(responses.every(response => response.statusCode === 201));
  const id = responses[0].json().task.id;
  assert.equal(new Set(responses.map(response => response.json().task.id)).size, 1);
  assert.equal((await request('POST', '/api/v1/tasks', {...body, goal: '冲突的测试目标'}, 'create')).json().code, 'IDEMPOTENCY_CONFLICT');
  const starts = await Promise.all(Array.from({length: 8}, () => request('POST', '/api/v1/tasks/' + id + '/start', {}, 'start')));
  assert.ok(starts.every(response => response.statusCode === 202), starts.map(response => response.body).join('\n'));
  assert.equal(fake.calls.length, 1);
  const started = starts[0].json();
  assert.equal(new Set(starts.map(response => response.json().run.id)).size, 1);
  const secondService = createConsoleService(serviceOptions);
  context.after(() => secondService.close());
  const secondBridge = (operation, fields) => Promise.resolve().then(() => secondService(operation, fields));
  const rebuilt = await createApi({bridge: secondBridge, controller: createExecutor({bridge: secondBridge, projectRoot, ...fake}), token: 'beta-test-token'});
  context.after(() => rebuilt.close());
  const replay = await rebuilt.inject({method: 'POST', url: '/api/v1/tasks/' + id + '/start', headers: {...headers, 'idempotency-key': 'start'}, payload: {}});
  assert.equal(replay.statusCode, 202); assert.equal(replay.json().run.id, started.run.id); assert.equal(fake.calls.length, 1);
  const restartedCreate = await rebuilt.inject({method: 'POST', url: '/api/v1/tasks', headers: {...headers, 'idempotency-key': 'create'}, payload: body});
  assert.equal(restartedCreate.statusCode, 201); assert.equal(restartedCreate.json().task.id, id);
  assert.equal((await request('POST', '/api/v1/tasks/' + id + '/start', {goal: 'inject'}, 'start')).statusCode, 409);
  const other = await create();
  const busy = await request('POST', '/api/v1/tasks/' + other.task.id + '/start', {}, 'busy');
  assert.equal(busy.statusCode, 409); assert.equal(busy.json().code, 'EXECUTOR_BUSY');
  await finish(started);
  assert.equal((await bridge('runs')).length, 1);
  assert.equal(controller !== null, true);
});

test('create_and_start busy 返回已创建 Task 查询地址；重放可在空闲后绑定一次', async context => {
  const {request, create, finish, fake} = await fixture(context);
  const first = await create();
  const running = (await request('POST', '/api/v1/tasks/' + first.task.id + '/start', {})).json();
  const payload = {...body, create_and_start: true};
  const busy = await request('POST', '/api/v1/tasks', payload, 'combined');
  assert.equal(busy.statusCode, 409); assert.equal(busy.json().code, 'EXECUTOR_BUSY');
  const id = busy.json().taskId;
  assert.equal(busy.json().taskUrl, '/api/v1/tasks/' + id);
  assert.equal((await request('GET', busy.json().taskUrl)).json().status, 'created');
  await finish(running);
  const started = await create(payload, 'combined');
  assert.equal(started.task.id, id); assert.ok(started.run.id);
  assert.equal((await create(payload, 'combined')).run.id, started.run.id);
  assert.equal(fake.calls.length, 2);
  await finish(started);
});

test('create_and_start 未启动的 Task 编辑后，重放原请求不能启动修改后的定义', async context => {
  const {request, create, finish, fake} = await fixture(context);
  const first = await create();
  const running = (await request('POST', '/api/v1/tasks/' + first.task.id + '/start', {})).json();
  const payload = {...body, create_and_start: true};
  const busy = await request('POST', '/api/v1/tasks', payload, 'combined-edit');
  const id = busy.json().taskId;
  assert.equal((await request('PUT', '/api/v1/tasks/' + id, {...body, goal: '编辑之后的新目标'})).statusCode, 200);
  await finish(running);
  const replay = await request('POST', '/api/v1/tasks', payload, 'combined-edit');
  assert.equal(replay.statusCode, 409); assert.equal(replay.json().code, 'TASK_CHANGED');
  assert.equal(fake.calls.length, 1);
  const explicit = (await request('POST', '/api/v1/tasks/' + id + '/start', {})).json();
  assert.equal(explicit.task.definition.goal, '编辑之后的新目标');
  await finish(explicit);
});

test('错 Task 停止不影响其他运行；停止并发与重复请求幂等且精确定位 Run', async context => {
  const {request, create, fake, controller} = await fixture(context);
  const first = await create(), other = await create();
  const started = (await request('POST', '/api/v1/tasks/' + first.task.id + '/start', {})).json();
  assert.equal((await request('POST', '/api/v1/tasks/' + other.task.id + '/stop', {})).statusCode, 200);
  assert.equal(fake.calls[0].child.exitCode, null);
  assert.equal((await request('POST', '/api/v1/tasks/absent/stop', {})).statusCode, 404);
  const stops = await Promise.all(Array.from({length: 4}, () => request('POST', '/api/v1/tasks/' + first.task.id + '/stop', {})));
  assert.ok(stops.every(response => [200, 202].includes(response.statusCode)));
  await controller.drain();
  const final = await request('POST', '/api/v1/tasks/' + first.task.id + '/stop', {});
  assert.equal(final.statusCode, 200); assert.equal(final.json().run.id, started.run.id);
  const result = (await request('GET', '/api/v1/runs/' + started.run.id + '/result')).json();
  assert.equal(result.outcome, 'CANCELLED'); assert.equal(result.settled, true);
});

test('不同 Task 并发 start 和旧 run.create 都遵守同一执行互斥', async context => {
  const {request, create, finish, fake, service} = await fixture(context);
  const tasks = await Promise.all([create(), create()]);
  const responses = await Promise.all(tasks.map(value => request('POST', '/api/v1/tasks/' + value.task.id + '/start', {}, 'race')));
  assert.deepEqual(responses.map(response => response.statusCode).sort(), [202, 409]);
  assert.equal(fake.calls.length, 1);
  assert.throws(() => service('run.create', {projectId: 'alpha', mode: 'test', goal: '旧入口运行目标'}), error => error.code === 'EXECUTOR_BUSY');
  const started = responses.find(response => response.statusCode === 202).json();
  assert.throws(() => service('run.continue', {id: started.run.id, instruction: '旧路径继续执行'}), error => error.code === 'TASK_RESTART_REQUIRED');
  await finish(started);
});

test('B 形状失败输出即使退出码非零也保持明确结果；伪造成功不得替代证据', async context => {
  const {request, create, fake, controller} = await fixture(context);
  const {task} = await create();
  const first = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  fake.calls[0].child.finish({schema_version: 1, task_id: task.id, run_id: first.run.id, repo_id: 'alpha',
    terminal: true, run_status: 'FAILED', outcome: 'INFRA_FAILURE', report_ref: null, report: null,
    patch_diff_ref: null, diff: null, patch_available: false, patch_verification: 'none', error: {code: 'configuration_missing'}}, 1);
  await controller.drain();
  const failure = (await request('GET', '/api/v1/runs/' + first.run.id + '/result')).json();
  assert.equal(failure.outcome, 'INFRA_FAILURE'); assert.equal(failure.settled, true);
  assert.equal(failure.error.code, 'configuration_missing');
  const second = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  fake.calls[1].child.finish({schema_version: 1, task_id: task.id, run_id: second.run.id, repo_id: 'alpha',
    terminal: true, run_status: 'COMPLETED', outcome: 'FIX_VERIFIED', report_ref: null,
    patch_available: true, patch_verification: 'verified', diff: 'forged'}, 0);
  await controller.drain();
  const forged = (await request('GET', '/api/v1/runs/' + second.run.id + '/result')).json();
  assert.equal(forged.outcome, 'UNKNOWN'); assert.equal(forged.settled, false); assert.equal(forged.error.retryable, false);
});

test('v1 artifacts 保留闭包、归属与 hash 检查；events 使用稳定序号', async context => {
  const {request, create, finish, dataRoot, service} = await fixture(context);
  const {task} = await create();
  const first = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  await finish(first);
  const second = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  await finish(second, 'NO_BUG_FOUND');
  const patch = '/api/v1/runs/' + first.run.id + '/artifacts/0002_patch.diff';
  assert.equal((await request('GET', patch)).statusCode, 200);
  assert.equal((await request('GET', patch.replace(first.run.id, second.run.id))).statusCode, 400);
  assert.ok((await request('GET', '/api/v1/runs/' + first.run.id + '/artifacts/..%2F..%2Fprofiles.yaml')).statusCode >= 400);
  const filename = path.join(dataRoot, 'artifacts', 'alpha', first.run.id, '0002_patch.diff');
  fs.appendFileSync(filename, 'tampered');
  assert.equal((await request('GET', patch)).statusCode, 400);
  assert.equal((await request('GET', '/api/v1/runs/' + first.run.id + '/result')).statusCode, 400);
  const untouched = service('run.record', {id: second.run.id});
  assert.equal(untouched.outcome, 'NO_BUG_FOUND');
  const database = new DatabaseSync(path.join(dataRoot, 'console.sqlite3'));
  database.prepare('INSERT INTO console_events VALUES (?,?,?,?)').run(second.run.id, 1, 'fixture', JSON.stringify({seq: 1, type: 'fixture.finished'}));
  database.close();
  assert.equal((await request('GET', '/api/v1/runs/' + second.run.id + '/events')).json()[0].seq, 1);
  assert.deepEqual((await request('GET', '/api/v1/runs/' + second.run.id + '/events?after=1')).json(), []);
});

test('spawn 失败结构化保存；同 start 键重放不再次 spawn', async context => {
  const {request, create, fake} = await fixture(context);
  fake.fail();
  const {task} = await create();
  const failed = await request('POST', '/api/v1/tasks/' + task.id + '/start', {}, 'failure');
  assert.equal(failed.statusCode, 503, failed.body); assert.equal(failed.json().code, 'SPAWN_FAILED');
  assert.ok(failed.json().runId); assert.doesNotMatch(failed.body, /private deployment/);
  const result = (await request('GET', '/api/v1/runs/' + failed.json().runId + '/result')).json();
  assert.equal(result.outcome, 'INFRA_FAILURE'); assert.equal(result.error.code, 'SPAWN_FAILED');
  const replay = await request('POST', '/api/v1/tasks/' + task.id + '/start', {}, 'failure');
  assert.equal(replay.statusCode, 202); assert.equal(replay.json().run.id, failed.json().runId); assert.equal(fake.calls.length, 1);
});

test('exit 0 或日志措辞不推断成功；不确定结果不自动重试', async context => {
  const {request, create, fake, controller, finish} = await fixture(context);
  const {task} = await create();
  const started = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  fake.calls[0].child.stderr.write('repair success; verification PASSED');
  fake.calls[0].child.finish(undefined, 0); await controller.drain();
  const result = (await request('GET', '/api/v1/runs/' + started.run.id + '/result')).json();
  assert.equal(result.outcome, 'UNKNOWN'); assert.equal(result.settled, false); assert.equal(result.error.retryable, false);
  const next = (await request('POST', '/api/v1/tasks/' + task.id + '/start', {})).json();
  await finish(next, 'INCONCLUSIVE');
  assert.equal((await request('GET', '/api/v1/runs/' + next.run.id + '/result')).json().outcome, 'INCONCLUSIVE');
});

test('Task schema 拒绝快照、命令、凭据、交互模式和不完整验收；执行 URL 须符合冻结白名单', async context => {
  const {request, fake, create} = await fixture(context);
  for (const extra of [{repository_snapshot: {}}, {profile: 'host'}, {commands: ['sh']}, {env: {TOKEN: 'fake'}},
    {credential_ref: 'real'}, {github_reads: [{source_kind: 'file', path: 'src/App.tsx', server: 'sh'}]},
    {execution: {executionMode: 'interactive'}}, {goal: 'token=abcdefghijklm'},
    {execution: {executionMode: 'batch', url: 'https://user:password@example.com'}}]) {
    const response = await request('POST', '/api/v1/tasks', {...body, ...extra});
    assert.equal(response.statusCode, 400, response.body); assert.equal(response.json().retryable, false);
    assert.ok(response.json().requestId); assert.ok(response.json().code);
  }
  assert.equal((await request('POST', '/api/v1/tasks', {...body, finding_acceptance: {schema_version: 1}})).statusCode, 400);
  const unapproved = await create({...body, execution: {executionMode: 'batch', url: 'https://unapproved.example'}});
  assert.equal((await request('POST', '/api/v1/tasks/' + unapproved.task.id + '/start', {})).statusCode, 403);
  assert.equal(fake.calls.length, 0);
});

test('Task 项目缺失返回 404；来源条件在公开 schema 校验并保持无副作用', async context => {
  const {request, fake, service} = await fixture(context);
  const missing = await request('POST', '/api/v1/tasks', {...body, projectId: 'absent'});
  assert.equal(missing.statusCode, 404, missing.body);
  assert.equal(missing.json().code, 'PROJECT_NOT_FOUND');
  const malformed = await request('POST', '/api/v1/tasks', {...body, projectId: ''});
  assert.equal(malformed.statusCode, 400, malformed.body);
  for (const patch of [{source_run_id: 'source', source_finding_ids: ['finding']},
    {mode: 'repair', source_run_id: 'source'}, {mode: 'repair', source_run_id: 'source', source_finding_ids: []},
    {mode: 'repair', source_finding_ids: ['finding']}]) {
    const response = await request('POST', '/api/v1/tasks', {...body, projectId: 'absent', ...patch});
    assert.equal(response.statusCode, 400, response.body);
    assert.equal(response.json().code, 'VALIDATION_ERROR', response.body);
  }
  assert.equal(fake.calls.length, 0);
  assert.deepEqual(service('tasks'), []);
  const permitted = await request('POST', '/api/v1/tasks', {...body, source_finding_ids: []});
  assert.equal(permitted.statusCode, 201, permitted.body);
});

test('来源 Run/Finding 和追加规则经归属校验后传递 B；拒绝跨 Run 来源', async context => {
  const {request, create, fake, databasePath, finish, service} = await fixture(context);
  const source = service('run.create', {projectId: 'alpha', mode: 'test', goal: '来源测试'});
  const database = new DatabaseSync(databasePath);
  const sourceRecord = JSON.parse(database.prepare('SELECT data FROM console_runs WHERE id=?').get(source.id).data);
  const internal = 'run_source_fixture';
  database.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify({...sourceRecord, status: 'completed', agentRunId: internal, outcome: 'BUG_CONFIRMED'}), source.id);
  const finding = {id: 'finding_source', run_id: internal, scope_id: 'alpha', repo_id: 'alpha', status: 'reproduced'};
  database.prepare('INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)').run(finding.id, 'job', internal, null, null,
    'test', 'major', finding.status, 'fingerprint', JSON.stringify(finding), new Date().toISOString(), new Date().toISOString());
  database.close();
  const rule = service('rules')[0];
  const created = await create({...body, mode: 'repair', source_run_id: source.id, source_finding_ids: [finding.id],
    additionalRuleIds: [rule.id], github_reads: [{source_kind: 'file', path: 'src/App.tsx'}, {source_kind: 'issue', number: 7}]});
  const started = (await request('POST', '/api/v1/tasks/' + created.task.id + '/start', {})).json();
  assert.equal(fake.calls[0].request.source_run_id, internal);
  assert.deepEqual(fake.calls[0].request.source_finding_ids, [finding.id]);
  assert.deepEqual(fake.calls[0].request.additional_rule_ids, [rule.id]);
  assert.equal(fake.calls[0].request.github_reads[1].number, 7);
  assert.equal((await request('GET', '/api/v1/runs/' + source.id + '/findings')).json()[0].id, finding.id);
  await finish(started);
  const forged = await create({...body, mode: 'repair', source_run_id: source.id, source_finding_ids: ['wrong']});
  assert.equal((await request('POST', '/api/v1/tasks/' + forged.task.id + '/start', {})).statusCode, 404);
});

test('Finding 验收模板存入 Task、绑定唯一内部来源并透传新的 Repair Run；非法绑定拒绝', async context => {
  const {request, create, fake, databasePath, finish, service, target} = await fixture(context);
  const source = service('run.create', {projectId: 'alpha', mode: 'test', goal: '来源测试'});
  const database = new DatabaseSync(databasePath);
  const sourceRecord = JSON.parse(database.prepare('SELECT data FROM console_runs WHERE id=?').get(source.id).data);
  const internal = 'run_acceptance_source';
  database.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify({...sourceRecord,
    status: 'completed', agentRunId: internal, outcome: 'BUG_CONFIRMED'}), source.id);
  const finding = {id: 'finding_acceptance_source', run_id: internal, scope_id: 'alpha', status: 'reproduced'};
  database.prepare('INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)').run(finding.id, 'job', internal, null, null,
    'user', 'major', finding.status, 'fingerprint', JSON.stringify(finding), new Date().toISOString(), new Date().toISOString());
  database.close();
  const goal = '验收任务完成取消和刷新持久化';
  const check = condition => ({locator: {role: 'checkbox', name: 'Complete task'}, condition});
  const navigate = {action: {kind: 'navigate', value: 'http://app:3000'}, assertions: [check('unchecked')]};
  const click = condition => ({action: {kind: 'click', locator: check(condition).locator}, assertions: [check(condition)]});
  const acceptanceSpec = {goal, authorized_actions: ['navigate', 'click', 'observe', 'finish'],
    assertions: [check('checked')], regression_plan: [navigate.action], regression_assertions: [check('unchecked')],
    behavior_scenarios: [{id: 'original', steps: [navigate, click('checked')]},
      {id: 'normal', steps: [navigate, {action: {kind: 'observe'}, assertions: [check('unchecked')]}]},
      {id: 'reverse', steps: [navigate, click('checked'), click('unchecked')]},
      {id: 'refresh', steps: [navigate, click('checked'), {action: navigate.action, assertions: [check('checked')]}]}]};
  const acceptance = {schema_version: 1, repo_id: 'alpha', commit: git(target, 'rev-parse', 'HEAD'),
    source_run_id: internal, source_scope_id: 'alpha', source_repo_id: 'fixture/alpha',
    source_commit: git(target, 'rev-parse', 'HEAD'), source_manifest: 'a'.repeat(64), source_rule_snapshot_hash: 'b'.repeat(64),
    findings: [{finding_id: finding.id, fingerprint: 'fingerprint', expected: '完成后保持勾选', actual: '没有保持',
      evidence_refs: ['0001_来源.png'], original_scenario_id: 'original', guards: [
        {kind: 'normal', scenario_id: 'normal'}, {kind: 'reverse', scenario_id: 'reverse'}, {kind: 'refresh', scenario_id: 'refresh'}]}],
    spec: acceptanceSpec, spec_hash: 'c'.repeat(64)};
  const input = {projectId: 'alpha', mode: 'repair', goal, execution: {executionMode: 'batch'},
    source_run_id: source.id, source_finding_ids: [finding.id], finding_acceptance: acceptance};
  for (const patch of [{mode: 'test'}, {source_run_id: undefined}, {source_finding_ids: []}]) {
    const response = await request('POST', '/api/v1/tasks', {...input, ...patch});
    assert.equal(response.statusCode, 400, response.body);
    assert.equal(response.json().code, 'VALIDATION_ERROR', response.body);
  }
  for (const patch of [{verified: true}, {schema_version: true}, {test_code: 'arbitrary code'}])
    assert.equal((await request('POST', '/api/v1/tasks', {...input, finding_acceptance: {...acceptance, ...patch}})).statusCode, 400);
  for (const patch of [{source_run_id: 'wrong_internal'}, {commit: 'd'.repeat(40)}, {run_id: 'historical_repair'}]) {
    const wrong = await create({...input, finding_acceptance: {...acceptance, ...patch}});
    assert.equal((await request('POST', '/api/v1/tasks/' + wrong.task.id + '/start', {})).statusCode, 409);
    assert.equal(fake.calls.length, 0);
  }
  const created = await create(input, 'acceptance-create');
  assert.deepEqual(created.task.definition.finding_acceptance, acceptance);
  assert.equal((await request('POST', '/api/v1/tasks', {...input,
    finding_acceptance: {...acceptance, source_manifest: 'd'.repeat(64)}}, 'acceptance-create')).statusCode, 409);
  const started = (await request('POST', '/api/v1/tasks/' + created.task.id + '/start', {}, 'acceptance-start')).json();
  assert.equal(fake.calls.length, 1);
  assert.deepEqual(fake.calls[0].request.finding_acceptance, {...acceptance, run_id: started.run.id});
  assert.equal(fake.calls[0].request.source_run_id, internal);
  assert.equal(fake.calls[0].request.execution.executionMode, 'batch');
  assert.equal(fake.calls[0].request.finding_acceptance.spec_hash, acceptance.spec_hash);
  await finish(started, 'INCONCLUSIVE');
});

test('OpenAPI、统一错误与 token/loopback 边界', async context => {
  const {app, bridge, controller, request} = await fixture(context);
  assert.equal((await app.inject({url: '/api/v1/tasks'})).statusCode, 401);
  assert.equal((await app.inject({url: '/api/v1/tasks', headers: {...headers, origin: 'https://foreign.example'}})).statusCode, 401);
  const schema = (await request('GET', '/api/v1/openapi.json')).json();
  assert.equal(schema.openapi, '3.1.0');
  for (const endpoint of ['/api/v1/tasks', '/api/v1/tasks/{id}/start', '/api/v1/tasks/{id}/stop',
    '/api/v1/runs/{id}/findings', '/api/v1/runs/{id}/result', '/api/v1/runs/{id}/events', '/api/v1/rules']) assert.ok(schema.paths[endpoint], endpoint);
  const failed = await app.inject({method: 'POST', url: '/api/v1/tasks', headers: {...headers, 'content-type': 'application/json'}, payload: '{'});
  assert.equal(failed.statusCode, 400); assert.ok(failed.json().code); assert.equal(failed.json().retryable, false);
  assert.equal(failed.json().requestId, failed.headers['x-request-id']);
  const loopback = await createApi({bridge, controller, token: ''}); context.after(() => loopback.close());
  assert.equal((await loopback.inject({url: '/api/v1/tasks', headers: {host: 'localhost'}})).statusCode, 200);
  assert.equal((await loopback.inject({url: '/api/v1/tasks', headers: {host: 'evil.example'}})).statusCode, 401);
  assert.equal((await loopback.inject({url: '/api/v1/tasks', headers: {host: 'localhost'}, remoteAddress: '203.0.113.3'})).statusCode, 401);
});

test('v1 与旧规则入口共用版本检查和发布业务', async context => {
  const {request, service} = await fixture(context);
  const rule = service('rules')[0];
  const v1 = await request('GET', '/api/v1/rules/' + rule.id);
  assert.deepEqual(v1.json(), (await request('GET', '/api/rules/' + rule.id)).json());
  const stale = await request('POST', '/api/v1/rules/' + rule.id + '/publish', {expectedVersion: rule.version + 1});
  assert.equal(stale.statusCode, 409);
  const published = await request('POST', '/api/v1/rules/' + rule.id + '/publish', {expectedVersion: rule.version});
  assert.equal(published.statusCode, 200); assert.equal(published.json().status, 'enabled');
  assert.equal(published.json().version, rule.version + 1);
  assert.equal((await request('GET', '/api/v1/rules/' + rule.id + '/versions')).json().length, 2);
});
