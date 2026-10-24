import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {createApi} from '../server/api.mjs';
import {createExecutor} from '../server/executor.mjs';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';

const projectRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const bundle = await build({stdin: {contents: "export * from './src/results.ts'; export * from './src/config.ts';",
  resolveDir: path.join(projectRoot, 'backend/packages/console-service')}, bundle: true, platform: 'node', format: 'esm', write: false});
const {executorResult, resultView, projectCatalog} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const headers = {authorization: 'Bearer repository-test'};

test('仓库管理正式路由公开 OpenAPI 并校验注册更新字段', async context => {
  const calls = [];
  const bridge = async (operation, fields) => {calls.push({operation, fields}); return operation === 'repositories' ? [] : fields;};
  const app = await createApi({bridge, controller: {}, token: 'repository-test'});
  context.after(() => app.close());
  const request = (method, url, payload) => app.inject({method, url, headers, payload});
  const profile = {url: 'http://app:3000', allowed_origins: ['http://app:3000'],
    commands: Object.fromEntries(['prepare', 'start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', '--version']]))};
  const input = {repo_id: 'demo', owner: 'team', repo: 'demo', default_ref: 'main',
    checkout_mode: 'managed', publish_branch: false, require_approval: true, profile, allowed_files: ['src/**'], allowed_urls: ['http://app:3000']};
  assert.equal((await request('POST', '/api/v1/repositories', input)).statusCode, 201);
  assert.deepEqual(calls.at(-1), {operation: 'repository.create', fields: input});
  assert.equal((await request('PATCH', '/api/v1/repositories/demo', {publish_branch: true})).statusCode, 200);
  assert.deepEqual(calls.at(-1), {operation: 'repository.update', fields: {id: 'demo', patch: {publish_branch: true}}});
  assert.equal((await request('PATCH', '/api/v1/repositories/demo', {require_approval: false})).statusCode, 200);
  assert.deepEqual(calls.at(-1), {operation: 'repository.update', fields: {id: 'demo', patch: {require_approval: false}}});
  for (const invalid of [{require_approval: 'true'}, {require_approval: 1}, {repair_mode: 'hybrid'}])
    assert.equal((await request('PATCH', '/api/v1/repositories/demo', invalid)).statusCode, 400);
  for (const invalid of [{...input, checkout_mode: 'existing'}, {...input, token: 'secret'},
    {...input, publish_branch: 'true'}, {...input, require_approval: 'yes'}, {...input, repair_mode: 'pipeline'}, {...input, profile: {...profile, commands: {...profile.commands, prepare: 'npm ci'}}}]) {
    assert.equal((await request('POST', '/api/v1/repositories', invalid)).statusCode, 400);
  }
  assert.equal((await request('PATCH', '/api/v1/repositories/demo', {unknown: true})).statusCode, 400);
  assert.equal((await request('GET', '/api/v1/repositories')).statusCode, 200);
  assert.equal((await request('GET', '/api/v1/repositories/demo')).statusCode, 200);
  const schema = (await request('GET', '/api/v1/openapi.json')).json();
  assert.ok(schema.paths['/api/v1/repositories'].post);
  assert.ok(schema.paths['/api/v1/repositories/{id}'].patch);
  assert.ok(schema.paths['/api/v1/runs/{id}/result'].get.responses['200'].content['application/json'].schema.properties.summary);
});

test('修复结果只接受与权威报告一致的摘要分支提交及发布证据', () => {
  const commit = 'a'.repeat(40), localCommit = 'b'.repeat(40);
  const run = {id: 'run-1', taskId: 'task-1', projectId: 'demo', reportRef: 'report.json', agentRunId: 'run-1'};
  const publication = {status: 'failed', branch: 'tracefix/run-1', commit, error: {code: 'publication_failed'}};
  const report = {run_status: 'COMPLETED', outcome: 'FIX_VERIFIED', patch_diff_ref: 'patch.diff', patch_available: true,
    patch_verification: 'verified', result_summary: '修复筛选状态持久化', branch: 'local/run-1', repair_commit: localCommit, publication};
  const value = {schema_version: 1, task_id: run.taskId, run_id: run.id, repo_id: run.projectId, terminal: true,
    run_status: report.run_status, outcome: report.outcome, report_ref: run.reportRef,
    patch_diff_ref: 'patch.diff', patch_available: true, patch_verification: 'verified', diff: 'verified patch',
    summary: report.result_summary, branch: publication.branch, commit, publication, report};
  const changes = executorResult(value, run, {issueReport: report}, Buffer.from(value.diff));
  assert.equal(changes.executionResult.patch.commit, commit);
  const result = resultView({...run, ...changes, issueReport: report, repositorySnapshot: {commit: 'c'.repeat(40)}}, []);
  assert.equal(result.summary, report.result_summary);
  assert.equal(result.branch, publication.branch);
  assert.equal(result.commit, commit);
  assert.equal(result.provenance.commit, 'c'.repeat(40));
  assert.deepEqual(result.publication, publication);
  for (const [key, forged] of Object.entries({summary: '伪造摘要', branch: 'forged', commit: localCommit, publication: {...publication, status: 'published'}})) {
    assert.throws(() => executorResult({...value, [key]: forged}, run, {issueReport: report}, Buffer.from(value.diff)),
      error => error.code === 'EXECUTOR_RESULT_INVALID', key);
  }
  assert.throws(() => executorResult({...value, report: {...report, repair_commit: 'd'.repeat(40), publication: null}}, run,
    {issueReport: report}, Buffer.from(value.diff)), error => error.code === 'EXECUTOR_RESULT_INVALID');
  const noReport = executorResult({schema_version: 1, task_id: run.taskId, run_id: run.id, repo_id: run.projectId,
    terminal: true, run_status: 'FAILED', outcome: 'INFRA_FAILURE'}, run);
  assert.equal(noReport.executionResult.summary, null);
  assert.throws(() => executorResult({schema_version: 1, task_id: run.taskId, run_id: run.id, repo_id: run.projectId,
    terminal: true, run_status: 'FAILED', outcome: 'INFRA_FAILURE', commit}, run), error => error.code === 'EXECUTOR_RESULT_INVALID');
});

test('托管仓库目录尚未拉取时项目配置可启动并展示来源', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-managed-catalog-'));
  const registry = path.join(root, 'projects.yaml');
  const profile = {project: 'demo', source_commit: 'a'.repeat(40), url: 'http://app:3000',
    commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', '--version']]))};
  fs.writeFileSync(registry, JSON.stringify({projects: [{id: 'demo', repo_id: 'team/demo', root: './missing',
    allowed_files: ['src/**'], repository_config: {version: 1, checkout_mode: 'managed', owner: 'team', repo: 'demo',
      default_ref: 'main', publish_branch: false, require_approval: true, profile}}]}));
  const [record] = projectCatalog(registry, path.join(root, 'data'), root);
  assert.equal(record.ready, true);
  assert.equal(record.checkoutMode, 'managed');
  assert.equal(record.cloneUrl, 'https://github.com/team/demo.git');
  assert.equal(record.defaultRef, 'main');
  assert.equal(record.requireApproval, true);
  assert.equal(record.publishBranch, false);
});

test('执行器将显式服务数据库配置或部署数据库回退传给子进程', async context => {
  const previous = {service: process.env.TRACEFIX_SERVICE_DATABASE_URL, database: process.env.TRACEFIX_DATABASE_URL};
  context.after(() => {
    for (const [name, value] of [['TRACEFIX_SERVICE_DATABASE_URL', previous.service], ['TRACEFIX_DATABASE_URL', previous.database]]) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
  });
  process.env.TRACEFIX_DATABASE_URL = 'postgresql://fixture/fallback';
  for (const explicit of ['', 'postgresql://fixture/explicit']) {
    process.env.TRACEFIX_SERVICE_DATABASE_URL = explicit;
    const task = {id: 'task', definition: {projectId: 'demo', mode: 'test', goal: '验证数据库配置', execution: {executionMode: 'batch'}}};
    const run = {id: 'run', taskId: task.id, projectId: 'demo', status: 'running'};
    let options, child;
    const bridge = async operation => operation === 'task.start' ? {task, run} : operation === 'task' ? task : run;
    const controller = createExecutor({projectRoot, bridge, spawn: (command, args, spawnedOptions) => {
      options = spawnedOptions; child = new EventEmitter(); child.pid = 12345;
      child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
      process.nextTick(() => child.emit('spawn')); return child;
    }});
    await controller.start(task.id);
    assert.equal(options.env.TRACEFIX_SERVICE_DATABASE_URL, explicit || process.env.TRACEFIX_DATABASE_URL);
    child.emit('close', 1, null); await controller.drain();
  }
});
