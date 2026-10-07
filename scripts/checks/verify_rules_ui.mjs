import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import net from 'node:net';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {createConsoleService} from '../../backend/packages/console-service/dist/dispatch.mjs';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
const root = process.cwd();
await mkdir(path.join(root, '.tracefix'), {recursive: true});
const temporary = await mkdtemp(path.join(root, '.tracefix', 'rules-ui-'));
const target = path.join(temporary, 'target');
await mkdir(path.join(target, '.git'), {recursive: true});
const registry = path.join(temporary, 'projects.yaml');
const databasePath = path.join(temporary, 'console.sqlite3');
await writeFile(registry, JSON.stringify({projects: [{id: 'rules-ui', repo_id: 'fixture', root: target}]}));
await writeFile(path.join(temporary, 'rules-ui.yaml'), JSON.stringify({project: 'rules-ui', source_commit: 'HEAD',
  commands: Object.fromEntries(['start', 'reset', 'unit', 'static', 'build'].map(name => [name, ['node', 'fixture']]))}));
const service = createConsoleService({projectsPath: registry, dataRoot: temporary, databasePath});
const parent = service('run.create', {projectId: 'rules-ui', goal: '规则继承验收父任务', mode: 'test'});
service('run.update', {id: parent.id, changes: {agentRunId: 'run_ui_parent', status: 'completed', outcome: 'NO_BUG_FOUND'}});
const refs = service('rules').map(rule => ({id: rule.id, version: rule.version}));
service.close();
const database = new DatabaseSync(databasePath);
database.prepare('INSERT INTO rule_run_snapshots VALUES (?,?,?,?,?)').run('run_ui_parent', null,
  createHash('sha256').update(JSON.stringify(refs)).digest('hex'), JSON.stringify(refs), new Date().toISOString());
database.close();
const listener = net.createServer();
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ['backend/apps/console-api/server/index.mjs'], {cwd: root, windowsHide: true,
  stdio: ['ignore', 'ignore', 'pipe'], env: {...process.env, PORT: String(port), TRACEFIX_PROJECTS: registry,
    TRACEFIX_DATA: temporary, TRACEFIX_CONSOLE_DB: databasePath, TRACEFIX_API_KEY: '', TRACEFIX_DATABASE_URL: '',
    TRACEFIX_CONTROL_TOKEN: '', NODE_ENV: 'production', BUGBOARD_DATA: path.join(temporary, 'board.json')}});
const closed = new Promise(resolve => server.once('close', resolve));
let serverErrors = '', browser;
server.stderr.on('data', chunk => {serverErrors += chunk;});
async function waitFor(check) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error(serverErrors);
    try {if (await check()) return;} catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('验收等待超时：' + serverErrors);
}
try {
  await waitFor(async () => (await fetch(origin + '/health')).ok);
  const initialRules = await (await fetch(origin + '/api/rules')).json();
  assert.equal(initialRules.length, 5);
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(origin + '/#rules');
  await page.getByRole('button', {name: '＋ 新建规则'}).click();
  const id = page.getByLabel('规则 ID', {exact: true});
  await id.pressSequentially('rule_ui_added');
  assert.equal(await id.inputValue(), 'rule_ui_added');
  await page.getByLabel('规则名称', {exact: true}).fill('追加空状态验收');
  await page.getByLabel('检测类型').selectOption('guided');
  await page.getByLabel('检测配置（JSON）').fill(JSON.stringify({type: 'guided', guided: {prompt: '确认空状态有创建入口'}}));
  await page.getByRole('button', {name: '保存规则', exact: true}).click();
  await page.getByRole('status').filter({hasText: '已保存 v1'}).waitFor();
  await page.locator('.rule-prompt code').filter({hasText: '确认空状态有创建入口'}).waitFor();
  await page.getByRole('button', {name: '发布启用', exact: true}).click();
  await page.getByRole('status').filter({hasText: '规则已发布'}).waitFor();
  await page.getByRole('button', {name: '归档', exact: true}).click();
  await page.getByRole('status').filter({hasText: '规则已归档'}).waitFor();
  await page.getByLabel('规则状态', {exact: true}).selectOption('archived');
  await page.locator('.rule-row').filter({hasText: '追加空状态验收'}).click();
  await page.getByRole('button', {name: '发布启用', exact: true}).click();
  await page.getByRole('status').filter({hasText: '规则已发布'}).waitFor();
  await page.reload();
  await page.locator('.rule-row').filter({hasText: '追加空状态验收'}).click();
  await page.locator('.rule-prompt').filter({hasText: '不得自行忽略'}).waitFor();
  await page.getByRole('link', {name: '运行记录'}).click();
  await page.locator('.run-row').filter({hasText: parent.goal}).click();
  await page.getByRole('heading', {name: '本次规则快照'}).waitFor();
  const extra = page.getByRole('checkbox', {name: /追加空状态验收/});
  await extra.check();
  await page.getByLabel('派生 Run 目标').fill('规则继承验收子任务');
  await page.waitForTimeout(2800);
  assert.equal(await extra.isChecked(), true);
  assert.equal(await page.getByLabel('派生 Run 目标').inputValue(), '规则继承验收子任务');
  await page.getByRole('button', {name: '创建派生 Run', exact: true}).click();
  await waitFor(async () => {
    const runs = await (await fetch(origin + '/api/runs')).json();
    const child = runs.find(run => run.parentRunId === 'run_ui_parent');
    if (!child) return false;
    assert.deepEqual(child.additionalRuleIds, ['rule_ui_added']);
    return child.status === 'failed';
  });
  await page.goto(origin + '/#rules');
  await page.setViewportSize({width: 390, height: 844});
  await page.locator('.rule-row').filter({hasText: '追加空状态验收'}).waitFor();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({path: path.join(temporary, 'rules-mobile.png'), fullPage: true});
  assert.deepEqual(pageErrors, []);
  console.log('通过：内置规则、新建编辑、提示词预览、归档筛选、刷新持久化、派生追加、移动端布局');
  console.log('派生进程使用未配置模型/数据库的失败路径；真实 Agent 修复不在本脚本验证范围。');
  console.log(path.relative(root, temporary));
} finally {
  await browser?.close();
  server.kill();
  await closed;
}
