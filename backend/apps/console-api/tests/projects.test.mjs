import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawn, spawnSync} from 'node:child_process';
import {createServer} from 'node:net';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';

const engineRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const python = process.env.TRACEFIX_PYTHON || path.join(engineRoot,
  process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const bundle = await build({entryPoints: [path.join(engineRoot, 'backend/packages/console-service/src/dispatch.ts')],
  bundle: true, platform: 'node', format: 'esm', write: false});
const {createConsoleService} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const configBundle = await build({entryPoints: [path.join(engineRoot, 'backend/packages/console-service/src/config.ts')],
  bundle: true, platform: 'node', format: 'esm', write: false});
const {createProject, configureProject, repositoryControl} = await import('data:text/javascript;base64,' + Buffer.from(configBundle.outputFiles[0].text).toString('base64'));

function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8', windowsHide: true});
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

function fixture(context) {
  if (!fs.existsSync(python)) {
    context.skip('需要部署 TRACEFIX_PYTHON 或本地 Python 环境');
    return null;
  }
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-repository-api-'));
  const profiles = path.join(root, 'profiles');
  fs.mkdirSync(profiles);
  const projectsPath = path.join(profiles, 'projects.yaml');
  fs.writeFileSync(projectsPath, JSON.stringify({projects: []}));
  const dataRoot = path.join(root, 'data');
  const options = {projectsPath, dataRoot, databasePath: path.join(dataRoot, 'console.sqlite3'),
    engineRoot, pythonCommand: python, repositoryRoots: [root]};
  const service = createConsoleService(options);
  context.after(() => service.close());
  function repository(id, owner = 'team', name = id) {
    const target = path.join(root, id);
    fs.mkdirSync(path.join(target, 'src'), {recursive: true});
    fs.writeFileSync(path.join(target, 'src/app.js'), 'export const project = ' + JSON.stringify(id) + ';\n');
    git(target, 'init');
    git(target, 'config', 'user.name', 'TraceFix Test');
    git(target, 'config', 'user.email', 'tracefix-test@example.invalid');
    git(target, 'add', '.');
    git(target, 'commit', '-m', '注册测试初始版本');
    git(target, 'remote', 'add', 'origin', 'https://github.com/' + owner + '/' + name + '.git');
    return {repo_id: id, projectId: id, root: target, owner, repo: name, default_ref: 'HEAD',
      profile: {url: 'http://app:3000', allowed_origins: ['http://app:3000'],
        commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(command => [command, ['node', 'fixture.js']]))},
      allowed_files: ['src/**'], allowed_urls: ['http://app:3000'], credential_ref: 'github_' + id, enabled: true};
  }
  return {root, profiles, projectsPath, dataRoot, options, service, repository};
}

test('受保护仓库 API 通过 Python 权威入口注册更新并保留旧接口', async context => {
  const fixtureData = fixture(context);
  if (!fixtureData) return;
  const {root, projectsPath, dataRoot, repository} = fixtureData;
  const listener = createServer();
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port;
  await new Promise(resolve => listener.close(resolve));
  const child = spawn(process.execPath, [path.join(engineRoot, 'backend/apps/console-api/server/index.mjs')],
    {cwd: engineRoot, windowsHide: true, stdio: 'ignore', env: {...process.env, PORT: String(port),
      TRACEFIX_CONTROL_HOST: '127.0.0.1', TRACEFIX_CONTROL_TOKEN: 'repository-control-test', TRACEFIX_PYTHON: python,
      TRACEFIX_PROJECTS: projectsPath, TRACEFIX_DATA: dataRoot, TRACEFIX_CONSOLE_DB: path.join(dataRoot, 'api.sqlite3'),
      TRACEFIX_REPOSITORY_ROOTS: JSON.stringify([root]), BUGBOARD_DATA: path.join(root, 'tasks.json')}});
  context.after(async () => {
    if (child.exitCode === null) {
      const closed = new Promise(resolve => child.once('close', resolve));
      child.kill();
      await closed;
    }
  });
  const base = 'http://127.0.0.1:' + port;
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {ready = (await fetch(base + '/health')).ok;} catch {}
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.equal(ready, true);
  async function request(route, method = 'GET', fields, authenticated = true) {
    return fetch(base + route, {method, headers: {'Content-Type': 'application/json',
      ...(authenticated ? {Authorization: 'Bearer repository-control-test'} : {})},
      ...(fields === undefined ? {} : {body: JSON.stringify(fields)})});
  }
  const route = '/api/v1/repositories';
  assert.equal((await request(route, 'GET', undefined, false)).status, 401);
  assert.equal((await request(route, 'POST', {}, false)).status, 401);
  assert.equal((await request(route + '/alpha', 'PATCH', {}, false)).status, 401);
  const alpha = repository('alpha'), beta = repository('beta');
  const registered = await request(route, 'POST', alpha);
  assert.equal(registered.status, 201, await registered.clone().text());
  const record = await registered.json();
  assert.equal(record.repo_id, 'alpha');
  assert.equal(record.projectId, 'alpha');
  assert.equal(record.commit, git(alpha.root, 'rev-parse', 'HEAD'));
  assert.equal((await request(route, 'POST', beta)).status, 201);
  const before = fs.readFileSync(projectsPath, 'utf8');
  for (const fields of [{...alpha}, {...alpha, repo_id: 'ALPHA', projectId: 'ALPHA'},
    {...beta, repo_id: 'alias', projectId: 'alias'}, {...beta, repo_id: 'mismatch', projectId: 'mismatch', repo: 'other'},
    {...beta, repo_id: 'bad-profile', projectId: 'bad-profile', profile: {commands: {start: ['node']}}},
    {...beta, repo_id: 'escape', projectId: 'escape', root: engineRoot,
      authorized_roots: [engineRoot], repositoryRoots: [engineRoot]},
    {...beta, repo_id: 'bad-path', projectId: 'bad-path', allowed_files: ['../secret']}]) {
    const response = await request(route, 'POST', fields);
    assert.ok([400, 403, 409].includes(response.status), await response.text());
    assert.equal(fs.readFileSync(projectsPath, 'utf8'), before);
  }
  assert.equal((await (await request(route)).json()).length, 2);
  assert.equal((await request(route + '/missing')).status, 404);
  assert.equal((await (await request(route + '/alpha')).json()).owner, 'team');
  const disabled = await request(route + '/alpha', 'PATCH', {enabled: false});
  assert.equal(disabled.status, 200, await disabled.clone().text());
  assert.equal((await disabled.json()).enabled, false);
  const projects = await (await request('/api/projects')).json();
  assert.ok(Array.isArray(projects), JSON.stringify(projects));
  assert.equal(projects.find(item => item.id === 'alpha').ready, false);
  assert.equal(projects.find(item => item.id === 'beta').ready, true);
  const rejectedRun = await request('/api/agent/start', 'POST', {projectId: 'alpha', mode: 'test', goal: '检查禁用仓库'});
  assert.ok([400, 409].includes(rejectedRun.status));
  assert.equal((await (await request(route)).json()).length, 2);
});

test('Run 冻结仓库 Profile 与 commit，A 到 B 到 A 的证据和配置不串用', context => {
  const fixtureData = fixture(context);
  if (!fixtureData) return;
  const {service, repository, projectsPath} = fixtureData;
  const alpha = repository('alpha'), beta = repository('beta');
  service('repository.create', alpha);
  service('repository.create', beta);
  const first = service('run.create', {projectId: 'alpha', mode: 'test', goal: '检查 A'});
  assert.throws(() => service('run.create', {projectId: 'beta', mode: 'repair', goal: '检查 B'}), error => error.code === 'EXECUTOR_BUSY');
  service('run.ended', {id: first.id, error: '测试串行执行边界'});
  const middle = service('run.create', {projectId: 'beta', mode: 'repair', goal: '检查 B'});
  service('run.ended', {id: middle.id, error: '测试串行执行边界'});
  const frozenProfile = fs.readFileSync(first.profile, 'utf8');
  const frozenRegistry = fs.readFileSync(first.registry, 'utf8');
  assert.equal(first.repositorySnapshot.repo_id, 'alpha');
  assert.equal(middle.repositorySnapshot.repo_id, 'beta');
  assert.notEqual(first.registry, middle.registry);
  assert.notEqual(first.profile, middle.profile);
  assert.equal(first.repositorySnapshot.repository_config.credential_ref, 'github_alpha');
  assert.equal(middle.repositorySnapshot.repository_config.credential_ref, 'github_beta');
  assert.equal(service('runs', {projectId: 'alpha'}).length, 1);
  assert.equal(service('runs', {projectId: 'beta'}).length, 1);
  fs.writeFileSync(path.join(alpha.root, 'src/app.js'), 'export const version = 2;\n');
  git(alpha.root, 'add', '.');
  git(alpha.root, 'commit', '-m', '注册测试后续版本');
  service('repository.update', {id: 'alpha', patch: {default_ref: 'HEAD', credential_ref: 'github_updated',
    allowed_files: ['src/app.js'], profile: {...alpha.profile, health_path: '/ready'}}});
  const last = service('run.create', {projectId: 'alpha', mode: 'test', goal: '再次检查 A'});
  service('run.ended', {id: last.id, error: '测试串行执行边界'});
  assert.equal(last.repositorySnapshot.commit, git(alpha.root, 'rev-parse', 'HEAD'));
  assert.notEqual(last.repositorySnapshot.commit, first.repositorySnapshot.commit);
  assert.equal(last.repositorySnapshot.repository_config.credential_ref, 'github_updated');
  assert.equal(fs.readFileSync(first.profile, 'utf8'), frozenProfile);
  assert.equal(fs.readFileSync(first.registry, 'utf8'), frozenRegistry);
  assert.deepEqual(service('run', {id: first.id}).repositorySnapshot, first.repositorySnapshot);
  assert.deepEqual(service('run', {id: middle.id}).repositorySnapshot, middle.repositorySnapshot);
  for (const changes of [{repositorySnapshot: middle.repositorySnapshot}, {registry: projectsPath},
    {profile: middle.profile}, {projectId: 'beta'}, {dataRoot: 'other'}])
    assert.throws(() => service('run.update', {id: first.id, changes}), error => error.status === 409);
  assert.throws(() => service('repository.freeze', {id: 'beta', run_id: first.id}), error => error.status === 409);
  service('run.update', {id: first.id, changes: {status: 'failed', outcome: 'INFRA_FAILURE'}});
  const continued = service('run.continue', {id: first.id, instruction: '继续原快照'});
  assert.equal(continued.registry, first.registry);
  assert.equal(continued.profile, first.profile);
  assert.deepEqual(continued.repositorySnapshot, first.repositorySnapshot);
  service('run.update', {id: first.id, changes: {status: 'failed', outcome: 'INFRA_FAILURE'}});
  service('repository.update', {id: 'alpha', patch: {enabled: false}});
  assert.throws(() => service('run.create', {projectId: 'alpha', mode: 'test', goal: '禁止新启动'}));
  assert.throws(() => service('run.continue', {id: first.id, instruction: '禁止续启动'}), error => error.status === 409);
  assert.equal(fs.readFileSync(first.registry, 'utf8'), frozenRegistry);
});

test('Run 控制入口拒绝调用方伪造身份、仓库快照和证据归属', context => {
  const fixtureData = fixture(context);
  if (!fixtureData) return;
  const {service, repository, dataRoot} = fixtureData;
  service('repository.create', repository('alpha'));
  service('repository.create', repository('beta'));
  const original = service('run.create', {projectId: 'alpha', mode: 'test', goal: '合法 A'});
  service('run.ended', {id: original.id, error: '测试串行执行边界'});
  const forged = {id: original.id, consoleRunId: original.id, agentRunId: 'run_other',
    reportRef: '0001_report.json', artifacts: [{ref: '0001_report.json'}],
    repositorySnapshot: original.repositorySnapshot, registry: original.registry, profile: original.profile,
    dataRoot: 'other', repoSnapshotRef: 'other/repository.json', memorySnapshotRef: 'other/memory.json',
    sourceManifest: {repo_id: 'alpha'}, evidenceRefs: ['0001_report.json'],
    inheritedEvidenceRefs: ['0001_report.json'], scopeId: 'alpha', startedAt: '1900-01-01',
    logs: ['伪造日志'], knowledge: ['伪造证据'], status: 'completed', phase: 'FINALIZE',
    outcome: 'FIX_VERIFIED', finishedAt: '1900-01-01', origin: 'artifact'};
  const created = service('run.create', {...forged, projectId: 'beta', mode: 'repair', goal: '  合法 B  ',
    parentRunId: '', additionalRuleIds: []});
  assert.notEqual(created.id, original.id);
  assert.equal(created.projectId, 'beta');
  assert.equal(created.mode, 'repair');
  assert.equal(created.goal, '合法 B');
  assert.equal(created.repositorySnapshot.repo_id, 'beta');
  assert.notEqual(created.registry, original.registry);
  assert.notEqual(created.profile, original.profile);
  assert.equal(created.dataRoot, dataRoot);
  assert.equal(created.status, 'running');
  assert.equal(created.phase, 'STARTING');
  assert.equal(created.outcome, null);
  assert.equal(created.finishedAt, null);
  assert.equal(created.origin, 'web');
  assert.notEqual(created.startedAt, forged.startedAt);
  assert.deepEqual(created.logs, []);
  assert.deepEqual(created.knowledge, []);
  assert.deepEqual(created.additionalRuleIds, []);
  const injectedKeys = ['consoleRunId', 'agentRunId', 'reportRef', 'artifacts', 'repoSnapshotRef',
    'memorySnapshotRef', 'sourceManifest', 'evidenceRefs', 'inheritedEvidenceRefs', 'scopeId', 'parentRunId'];
  for (const record of [created, service('runs', {projectId: 'beta'})[0]])
    for (const key of injectedKeys) assert.equal(Object.hasOwn(record, key), false, key);
  assert.deepEqual(service('run', {id: created.id}).artifacts, []);
  assert.deepEqual(service('run', {id: original.id}).repositorySnapshot, original.repositorySnapshot);
  assert.equal(service('runs', {projectId: 'alpha'}).length, 1);
  const beforeUpdate = service('runs', {projectId: 'beta'})[0];
  for (const key of ['agentRunId', 'reportRef', 'artifacts', 'repositorySnapshot', 'profile', 'registry',
    'projectId', 'dataRoot', 'id', 'consoleRunId', 'repoSnapshotRef', 'memorySnapshotRef', 'sourceManifest',
    'evidenceRefs', 'inheritedEvidenceRefs', 'scopeId', 'parentRunId']) {
    assert.throws(() => service('run.update', {id: created.id, changes: {[key]: null, status: 'completed'}}),
      error => error.status === 409, key);
    assert.deepEqual(service('runs', {projectId: 'beta'})[0], beforeUpdate);
  }
  assert.equal(service('run.update', {id: created.id, changes: {status: 'failed', outcome: 'INFRA_FAILURE'}}).status, 'failed');
});

test('旧注册 helper 委托同一权威入口并保持未就绪项目兼容', context => {
  const fixtureData = fixture(context);
  if (!fixtureData) return;
  const {profiles, projectsPath, service} = fixtureData;
  const previous = {python: process.env.TRACEFIX_PYTHON, root: process.env.TRACEFIX_ENGINE_ROOT};
  process.env.TRACEFIX_PYTHON = python;
  process.env.TRACEFIX_ENGINE_ROOT = engineRoot;
  context.after(() => {
    for (const [name, value] of [['TRACEFIX_PYTHON', previous.python], ['TRACEFIX_ENGINE_ROOT', previous.root]]) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
  });
  const created = createProject(projectsPath, 'legacy', './legacy', 'legacy');
  assert.equal(created.id, 'legacy');
  assert.equal(service('projects')[0].ready, false);
  assert.deepEqual(service('repositories'), []);
  fs.mkdirSync(path.join(profiles, 'legacy', '.git'));
  const configured = configureProject(projectsPath, 'legacy', {url: 'http://app:3000',
    commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', 'fixture.js']]))});
  assert.equal(configured.project, 'legacy');
  assert.equal(service('projects')[0].ready, true);
  assert.equal(service('run.create', {projectId: 'legacy', mode: 'test', goal: '旧路径运行'}).repositorySnapshot, null);
});

test('部署 Python 无法启动时返回稳定错误且不泄露子进程诊断', () => {
  assert.throws(() => repositoryControl('missing.yaml', 'repository.list', {},
    {engineRoot, pythonCommand: path.join(engineRoot, '.missing-python-secret')}),
  error => error.code === 'repository_control_unavailable' && error.status === 503 && !error.message.includes('secret'));
});
