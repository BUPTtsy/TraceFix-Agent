import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {spawn} from 'node:child_process';

const projectRoot = fileURLToPath(new URL('../../', import.meta.url));
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || path.join(projectRoot, '.tracefix/playwright/node_modules/playwright'));
const port = Number(process.env.TRACEFIX_UI_PORT || '3109');
const baseUrl = `http://127.0.0.1:${port}`;
const validationRoot = path.resolve(process.env.TRACEFIX_UI_VALIDATION_ROOT || path.join(projectRoot, '.tracefix/repository-ui-validation'));
const evidenceRoot = path.join(projectRoot, 'artifacts/repository-ui', path.basename(validationRoot));
const profiles = path.join(validationRoot, 'profiles');
const registry = path.join(profiles, 'projects.yaml');
fs.mkdirSync(profiles, {recursive: true});
fs.mkdirSync(evidenceRoot, {recursive: true});
if (!fs.existsSync(registry)) fs.writeFileSync(registry, JSON.stringify({projects: []}));
const env = {...process.env, PORT: String(port), TRACEFIX_CONTROL_HOST: '127.0.0.1', TRACEFIX_CONTROL_TOKEN: '',
  TRACEFIX_PROJECTS: registry, TRACEFIX_DATA: path.join(validationRoot, 'data'),
  TRACEFIX_CONSOLE_DB: path.join(validationRoot, 'console.sqlite3'), BUGBOARD_DATA: path.join(validationRoot, 'tasks.json'),
  TRACEFIX_REPOSITORY_ROOTS: JSON.stringify([validationRoot]),
  TRACEFIX_PYTHON: process.env.TRACEFIX_PYTHON || path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')};
const log = fs.openSync(path.join(evidenceRoot, 'server.log'), 'a');
const child = spawn(process.execPath, [path.join(projectRoot, 'backend/apps/console-api/server/index.mjs')],
  {cwd: projectRoot, windowsHide: true, env, stdio: ['ignore', log, log]});
let browser;
try {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`验收服务启动失败：${child.exitCode}`);
    try {const response = await fetch(baseUrl + '/health'); if (response.ok) break;} catch {}
    if (attempt === 79) throw new Error('验收服务未就绪');
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(30000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(baseUrl + '/#projects');
  await page.waitForLoadState('networkidle');
  const projectId = 'repository-ui-' + Date.now();
  await page.getByRole('button', {name: '＋ 注册仓库'}).click();
  await page.getByLabel('项目 ID', {exact: true}).fill(projectId);
  await page.getByLabel('允许修改的文件').fill('src/**\nserver/**');
  const savedResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/repositories') && response.request().method() === 'POST', {timeout: 180000});
  await page.getByRole('button', {name: '保存仓库配置', exact: true}).click();
  const response = await savedResponse;
  assert.equal(response.status(), 201, await response.text());
  const saved = await response.json();
  assert.equal(saved.checkout_mode, 'managed');
  assert.equal(saved.publish_branch, false);
  assert.equal(saved.owner.toLowerCase(), 'bupttsy');
  assert.ok(fs.existsSync(path.join(saved.root, '.git')));
  await page.getByRole('heading', {name: projectId + ' 的运行配置'}).waitFor();
  await page.reload();
  await page.waitForLoadState('networkidle');
  await page.getByLabel('当前项目', {exact: true}).selectOption(projectId);
  await page.getByRole('button', {name: '编辑仓库配置', exact: true}).click();
  assert.equal(await page.getByLabel('代码获取方式').inputValue(), 'managed');
  assert.equal(await page.getByLabel('默认分支或 ref').inputValue(), 'main');
  await page.getByLabel('默认分支或 ref').fill('missing-ref-for-ui-validation');
  const failedResponse = page.waitForResponse(item => item.url().endsWith('/api/v1/repositories/' + projectId) && item.request().method() === 'PATCH', {timeout: 180000});
  await page.getByRole('button', {name: '保存仓库配置', exact: true}).click();
  const failed = await failedResponse;
  assert.ok(failed.status() >= 400, await failed.text());
  const failure = await failed.json();
  await page.locator('.repository-editor').waitFor();
  assert.ok((await page.locator('body').innerText()).includes(failure.message));
  await page.getByLabel('默认分支或 ref').fill('main');
  await page.getByRole('button', {name: '保存仓库配置', exact: true}).click();
  await page.getByRole('status').filter({hasText: '仓库配置已保存'}).waitFor();
  await page.getByRole('button', {name: '管理检测规则', exact: true}).click();
  await page.getByRole('heading', {name: /检测规则/, level: 1}).waitFor();
  await page.getByRole('navigation').getByRole('link', {name: /项目空间/}).click();
  await page.getByRole('button', {name: '编辑仓库配置', exact: true}).click();
  await page.getByLabel('默认分支或 ref').focus();
  await page.keyboard.press('Tab');
  assert.equal(await page.getByLabel('GitHub 仓库地址').evaluate(element => element === document.activeElement), true);
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: path.join(evidenceRoot, 'repository-mobile.png'), fullPage: true});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.getByRole('button', {name: '关闭', exact: true}).click();
  let taskPayload;
  await page.route('**/api/v1/tasks', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    taskPayload = route.request().postDataJSON();
    return route.fulfill({status: 201, contentType: 'application/json', body: JSON.stringify({task: {id: 'mock-task'}, run: {id: 'mock-run', taskId: 'mock-task', projectId, mode: 'repair', goal: taskPayload.goal, status: 'completed', phase: 'DONE', knowledge: [], startedAt: new Date().toISOString()}, replayed: false})});
  });
  await page.getByLabel('新的运行目标', {exact: true}).fill('验证远程仓库配置与规则传递');
  const taskResponse = page.waitForResponse(item => item.url().endsWith('/api/v1/tasks') && item.request().method() === 'POST');
  await page.getByRole('button', {name: '启动 Run ↑', exact: true}).click();
  await taskResponse;
  assert.equal(taskPayload.projectId, projectId);
  assert.equal(taskPayload.execution.executionMode, 'batch');
  assert.equal(taskPayload.create_and_start, true);
  assert.deepEqual(taskPayload.additionalRuleIds, []);
  await page.setViewportSize({width: 1440, height: 1000});
  const mockRun = {id: 'mock-run', taskId: 'mock-task', projectId, mode: 'repair', goal: '浏览器模拟：修复状态持久化', status: 'completed', phase: 'DONE', outcome: 'FIX_VERIFIED', knowledge: [], artifacts: [], startedAt: new Date().toISOString()};
  await page.route('**/api/runs', route => route.fulfill({status: 200, json: [mockRun]}));
  await page.route('**/api/runs/mock-run', route => route.fulfill({status: 200, json: mockRun}));
  await page.route('**/api/runs/mock-run/trace?*', route => route.fulfill({status: 200, json: []}));
  await page.route('**/api/v1/runs/mock-run/result', route => route.fulfill({status: 200, json: {runId: 'mock-run', ready: true, outcome: 'FIX_VERIFIED', summary: '浏览器模拟：状态持久化修复摘要', branch: 'tracefix/ui-fixture', commit: 'a'.repeat(40), publication: {status: 'local', branch: 'tracefix/ui-fixture', commit: 'a'.repeat(40), error: null}, patch: {ref: 'fixture.diff', diff: '--- a/src/App.tsx\n+++ b/src/App.tsx\n+fixture repair', available: true, verification: 'verified'}}}));
  await page.getByRole('navigation').getByRole('link', {name: /运行记录/}).click();
  await page.locator('.run-row').filter({hasText: mockRun.goal}).click();
  await page.getByText('浏览器模拟：状态持久化修复摘要', {exact: true}).waitFor();
  await page.getByText('已生成本地提交', {exact: true}).waitFor();
  await page.getByText('查看补丁 diff · 已验证', {exact: true}).click();
  await page.locator('.patch-diff').filter({hasText: 'fixture repair'}).waitFor();
  await page.screenshot({path: path.join(evidenceRoot, 'repository-desktop.png'), fullPage: true});
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(evidenceRoot, 'result.json'), JSON.stringify({passed: true, projectId, repository: {root: saved.root, commit: saved.commit}, checks: ['真实远程 clone 与注册', '保存与全新加载持久化', '真实无效 ref 错误', '规则入口', '键盘 Tab', '390px 无横向溢出', '模拟任务启动 payload，未调用模型', '模拟完成 Run 的摘要、commit、发布状态与 diff 展示'], taskPayload}, null, 2));
  console.log('通过：真实仓库注册/clone、刷新持久化、错误回显、规则导航、键盘与窄屏；Task POST 使用 mock，未启动模型。');
  console.log(evidenceRoot);
} finally {
  await browser?.close();
  if (child.exitCode === null) {child.kill(); await new Promise(resolve => child.once('exit', resolve));}
  fs.closeSync(log);
}
