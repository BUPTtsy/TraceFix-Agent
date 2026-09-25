import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
const projectRoot = process.cwd();
const temporary = await mkdtemp(path.join(projectRoot, '.tracefix', 'continuation-ui-'));
const target = path.join(temporary, 'target');
await mkdir(path.join(target, '.git'), {recursive: true});
const registry = path.join(temporary, 'projects.yaml');
await writeFile(registry, JSON.stringify({projects: [{id: 'continuation-ui', repo_id: 'fixture', root: target}]}));
await writeFile(path.join(temporary, 'continuation-ui.yaml'), JSON.stringify({project: 'continuation-ui', source_commit: 'HEAD',
  commands: Object.fromEntries(['start', 'reset', 'unit', 'static', 'build'].map(name => [name, ['node', 'fixture']]))}));
const listener = net.createServer();
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ['demo/bugboard/server/index.mjs'], {cwd: projectRoot, windowsHide: true,
  stdio: 'ignore', env: {...process.env, PORT: String(port), TRACEFIX_PROJECTS: registry,
    TRACEFIX_CONSOLE_DB: path.join(temporary, 'console.sqlite3'), TRACEFIX_DATA: path.join(temporary, 'data'),
    TRACEFIX_DATABASE_URL: '', TRACEFIX_API_KEY: '', NODE_ENV: 'production', BUGBOARD_DATA: path.join(temporary, 'board.json')}});
const closed = new Promise(resolve => server.once('close', resolve));
let browser;
async function waitFor(check) {
  for (let attempt = 0; attempt < 100; attempt++) {
    try {if (await check()) return;} catch {}
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('验收等待超时');
}
try {
  await waitFor(async () => (await fetch(origin + '/health')).ok);
  const response = await fetch(origin + '/api/agent/start', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({projectId: 'continuation-ui', mode: 'test', goal: '续执行验收任务'})});
  assert.equal(response.status, 202);
  const original = await response.json();
  await waitFor(async () => {const state = await (await fetch(origin + '/api/agent/status')).json(); return state.status === 'failed' && !state.running;});
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin + '/#runs');
  await page.locator('.run-row').filter({hasText: '续执行验收任务'}).click();
  await page.getByLabel('继续此任务').fill('重试 "quoted command"\n保留此前轨迹');
  await page.getByRole('button', {name: '继续执行', exact: true}).click();
  await page.locator('.continuation-detail .notice').filter({hasText: '已继续执行 1 次'}).waitFor();
  await waitFor(async () => {const state = await (await fetch(origin + '/api/agent/status')).json(); return state.status === 'failed' && !state.running;});
  await page.reload();
  await page.locator('.run-row').filter({hasText: '续执行验收任务'}).click();
  await page.locator('.continuation-detail .notice').filter({hasText: '已继续执行 1 次'}).waitFor();
  await page.locator('.task-trace summary').filter({hasText: '用户请求继续原任务'}).waitFor();
  const records = await (await fetch(origin + '/api/runs')).json();
  assert.equal(records.length, 1);
  assert.equal(records[0].id, original.id);
  assert.equal(records[0].startedAt, original.startedAt);
  assert.equal(records[0].continuationCount, 1);
  assert.equal(await page.locator('.run-row').count(), 1);
  await page.setViewportSize({width: 390, height: 844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({path: path.join(temporary, 'continuation-mobile.png'), fullPage: true});
  assert.deepEqual(errors, []);
  console.log('通过：Web 继续原任务、CLI 进程回写、异常标记、轨迹、刷新保留、移动端布局');
  console.log(path.relative(projectRoot, temporary));
} finally {
  await browser?.close();
  server.kill();
  await closed;
}
