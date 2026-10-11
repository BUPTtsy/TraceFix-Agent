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
const body = {projectId: 'alpha', mode: 'test', goal: '测试页面输入行为', execution: {executionMode: 'batch', url: 'http://app:3000', spec: '检查保存反馈'}};
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
    isAlive: pid => calls.some(call => call.child.pid === pid && call.child.exitCode === null),
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
    call.child.finish({schema_version: 1, task_id: started.task.id, run_id: started.run.id, repo_id: 'alpha',
      outcome, settled, findings: [], ruleCoverage: {summary: {total: 1, passed: 1}}, patch: null,
      verification: {overall_status: 'PASSED'}, error: null, provenance: {executor: 'explicit-test-double'}});
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
  assert.equal((await request('PUT', '/api/v1/tasks/' + created.task.id, {...body, goal: '新目标'})).json().status, 'created');
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
  assert.equal((await request('POST', '/api/v1/tasks', {...body, goal: '冲突'}, 'create')).json().code, 'IDEMPOTENCY_CONFLICT');
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
  assert.equal((await request('POST', '/api/v1/tasks/' + id + '/start', {goal: 'inject'}, 'start')).statusCode, 400);
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

test('Task schema 拒绝快照、命令、凭据、交互模式和未集成验收；执行 URL 须符合冻结白名单', async context => {
  const {request, fake, create} = await fixture(context);
  for (const extra of [{repository_snapshot: {}}, {profile: 'host'}, {commands: ['sh']}, {env: {TOKEN: 'fake'}},
    {credential_ref: 'real'}, {github_reads: [{source_kind: 'file', path: 'src/App.tsx', server: 'sh'}]},
    {execution: {executionMode: 'interactive'}}, {goal: 'token=abcdefghijklm'},
    {execution: {executionMode: 'batch', url: 'https://user:password@example.com'}}]) {
    const response = await request('POST', '/api/v1/tasks', {...body, ...extra});
    assert.equal(response.statusCode, 400, response.body); assert.equal(response.json().retryable, false);
    assert.ok(response.json().requestId); assert.ok(response.json().code);
  }
  assert.equal((await request('POST', '/api/v1/tasks', {...body, finding_acceptance: {schema_version: 1}})).statusCode, 422);
  const unapproved = await create({...body, execution: {executionMode: 'batch', url: 'https://unapproved.example'}});
  assert.equal((await request('POST', '/api/v1/tasks/' + unapproved.task.id + '/start', {})).statusCode, 403);
  assert.equal(fake.calls.length, 0);
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

test('OpenAPI、统一错误与 token/loopback 边界', async context => {
  const {app, bridge, controller, request} = await fixture(context);
  assert.equal((await app.inject({url: '/api/v1/tasks'})).statusCode, 401);
  assert.equal((await app.inject({url: '/api/v1/tasks', headers: {...headers, origin: 'https://foreign.example'}})).statusCode, 401);
  const schema = (await request('GET', '/api/v1/openapi.json')).json();
  assert.match(schema.openapi, /^3\./);
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
