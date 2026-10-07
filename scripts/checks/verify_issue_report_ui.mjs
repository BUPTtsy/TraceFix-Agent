import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || '../../.tracefix/playwright/node_modules/playwright');
const projectRoot = process.cwd();
const temporary = await mkdtemp(path.join(projectRoot, '.tracefix', 'issue-report-ui-'));
const fixture = path.join(temporary, 'fixture');
const python = path.join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const script = `import asyncio,sys
from pathlib import Path
from tracefix.runtime.smoke import make_engine
from tracefix.knowledge.documents import DocumentLibrary
root=Path(sys.argv[1])
engine,state=make_engine(root)
state.mode='test'
asyncio.run(engine.run(state))
state=engine.store.load(state.run_id,state.scope_id)
DocumentLibrary(root/'console.sqlite3').update_run('report_demo', {
    'projectId':state.scope_id,'agentRunId':state.run_id,'goal':'前端问题报告浏览器验收',
    'mode':'test','status':'completed','phase':'FINALIZE','outcome':str(state.outcome),
    'reportRef':state.report_ref},create=True)
`;
const generated = spawnSync(python, ['-c', script, fixture], {cwd: projectRoot, windowsHide: true, encoding: 'utf8',
  env: {...process.env, PYTHONIOENCODING: 'utf-8', PYTHONPATH: path.join(projectRoot, 'backend/packages/agent/src')}});
assert.equal(generated.status, 0, generated.stderr);
const profiles = path.join(temporary, 'profiles');
await mkdir(profiles);
const registry = path.join(profiles, 'projects.yaml');
await writeFile(registry, JSON.stringify({projects: [{id: 'b', repo_id: 'fixture', root: path.join(fixture, 'code')}]}));
await writeFile(path.join(profiles, 'b.yaml'), JSON.stringify({project: 'b', source_commit: 'HEAD',
  commands: Object.fromEntries(['start', 'reset', 'unit', 'static', 'build'].map(name => [name, ['node', 'fixture']]))}));
const listener = net.createServer();
await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
const port = listener.address().port;
await new Promise(resolve => listener.close(resolve));
const origin = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ['backend/apps/console-api/server/index.mjs'], {cwd: projectRoot, windowsHide: true,
  stdio: 'ignore', env: {...process.env, PORT: String(port), TRACEFIX_PROJECTS: registry,
    TRACEFIX_CONSOLE_DB: path.join(fixture, 'console.sqlite3'), TRACEFIX_DATA: fixture,
    TRACEFIX_DATABASE_URL: '', TRACEFIX_API_KEY: '', NODE_ENV: 'production', BUGBOARD_DATA: path.join(temporary, 'board.json')}});
const closed = new Promise(resolve => server.once('close', resolve));
let browser;
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try {ready = (await fetch(origin + '/health')).ok;} catch {}
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, 'HTTP 服务未就绪');
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(origin + '/#runs');
  await page.locator('.run-row').filter({hasText: '前端问题报告浏览器验收'}).click();
  const issue = page.locator('.continuation-detail article').first();
  await issue.waitFor();
  assert.match(await issue.innerText(), /Complete task.*未选中/);
  assert.match(await issue.innerText(), /保持选中/);
  assert.match(await issue.innerText(), /已确认/);
  assert.match(await issue.innerText(), /打开页面 http:\/\/app:3000/);
  const evidence = issue.locator('a').first();
  const evidenceResponse = await page.request.get(new URL(await evidence.getAttribute('href'), origin).href);
  assert.equal(evidenceResponse.status(), 200);
  assert.equal((await evidenceResponse.json()).passed, false);
  const trajectory = page.locator('.artifact-links a').filter({hasText: '阶段轨迹'}).first();
  const trajectoryResponse = await page.request.get(new URL(await trajectory.getAttribute('href'), origin).href);
  assert.equal(trajectoryResponse.status(), 200);
  assert.ok((await trajectoryResponse.json()).events.length > 1);
  await page.reload();
  await page.locator('.run-row').filter({hasText: '前端问题报告浏览器验收'}).click();
  await issue.waitFor();
  await page.screenshot({path: path.join(temporary, 'report-desktop.png'), fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({path: path.join(temporary, 'report-mobile.png'), fullPage: true});
  assert.deepEqual(errors, []);
  console.log('通过：具体问题描述、预期与实际表现、复现步骤、确认状态、证据下载、分段轨迹、刷新持久化、移动端布局。使用 Fake 引擎生成验收报告。');
  console.log(path.relative(projectRoot, temporary));
} finally {
  await browser?.close();
  server.kill();
  await closed;
}
