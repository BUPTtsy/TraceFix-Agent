import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {DatabaseSync} from 'node:sqlite';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
test('artifact closure preserves bytes, authorizes siblings and rejects escapes', async context => {
  const repo = fileURLToPath(new URL('../../../../', import.meta.url));
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-artifact-test-'));
  const profiles = path.join(root, 'projects.yaml'), databasePath = path.join(root, 'console.sqlite3');
  fs.writeFileSync(profiles, JSON.stringify({projects: []}));
  const bundle = await build({entryPoints: [path.join(repo, 'backend/packages/console-service/src/dispatch.ts')], bundle: true, platform: 'node', format: 'esm', write: false});
  const {createConsoleService} = await import('data:text/javascript;base64,' + Buffer.from(bundle.outputFiles[0].text).toString('base64'));
  const service = createConsoleService({projectsPath: profiles, dataRoot: root, databasePath});
  context.after(() => service.close());
  const directory = path.join(root, 'artifacts', 'alpha', 'run_current'), index = {};
  fs.mkdirSync(directory, {recursive: true});
  function artifact(ref, value, label = '收尾_公开证据') {
    const raw = Buffer.isBuffer(value) ? value : Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
    fs.writeFileSync(path.join(directory, ref), raw);
    index[ref] = {'用途': label, SHA256: createHash('sha256').update(raw).digest('hex'), '字节数': raw.length};
    fs.writeFileSync(path.join(directory, '文件索引.json'), JSON.stringify(index));
  }
  const rule = {id: 'label', name: '输入标签', criteria: '输入控件具有可访问名称', source: 'rule', severity: 'blocker', detector: 'static'};
  const checkResult = {...rule, status: 'fail', actual: '输入框缺少标签', evidence_refs: ['0011_item_evidence.json'], stage: 'verify'};
  const report = {run_id: 'run_current', scope_id: 'alpha', evidence_refs: ['0002_check.json'], validation_refs: ['0003_validation.json'], issues: [{evidence_refs: ['0002_check.json']}], model_exchange_refs: ['0007_private.json'],
    check_plan: [rule], check_results: [checkResult], check_plan_ref: '0012_plan.json', check_result_refs: ['0013_result.json'], initial_check_result_refs: ['0015_initial_result.json'],
    initial_check_results: [{...checkResult, evidence_refs: ['0016_initial_evidence.json'], stage: 'explore'}],
    check_summary: {total: 1, executed: 1, failed: 1, blocker_failed: 1}, overall_status: 'FAILED',
    images: [{ref: '0014_image.png', mime: 'image/png', alt: '独立检查截图'}]};
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');
  artifact('0001_report.json', report, '收尾_修复报告数据');
  artifact('0002_check.json', {observation_ref: '0004_observation.json', artifact_ref: '0003_validation.json'});
  artifact('0003_validation.json', {artifact_ref: '0002_check.json'});
  artifact('0004_observation.json', {snapshot: 'Ready', screenshot_ref: '0005_image.png'});
  artifact('0005_image.png', png, '验证_页面截图');
  artifact('0006_report.html', '<a href="0002_check.json">Evidence</a><img src="0005_image.png"><img src="data:image/png;base64,' + png.toString('base64') + '">', '收尾_修复报告');
  artifact('0007_private.json', {request: 'private'}, '诊断_模型调用001_输入');
  artifact('0008_events.json', [{type: 'run.finished', run_id: 'run_current', scope_id: 'alpha', payload: {report_ref: '0001_report.json', html_ref: '0006_report.html'}}], '收尾_完整事件数据');
  artifact('0009_unrelated.diff', 'not evidence');
  artifact('0010_unrelated.html', '<p>not evidence</p>');
  artifact('0011_item_evidence.json', {observation_ref: '0004_observation.json'});
  artifact('0012_plan.json', {run_id: 'run_current', items: [rule]}, '探索_冻结检查计划');
  artifact('0013_result.json', checkResult, '验证_逐项检查结果');
  artifact('0014_image.png', png, '探索_页面截图');
  artifact('0015_initial_result.json', report.initial_check_results[0], '探索_逐项检查结果');
  artifact('0016_initial_evidence.json', {screenshot_ref: '0014_image.png'});
  for (const [projectId, agentRunId] of [['alpha', 'run_other'], ['beta', 'run_current']]) {
    const sibling = path.join(root, 'artifacts', projectId, agentRunId);
    fs.mkdirSync(sibling, {recursive: true});
    const raw = Buffer.from(JSON.stringify({run_id: agentRunId, scope_id: projectId, evidence_refs: []}));
    fs.writeFileSync(path.join(sibling, '0001_report.json'), raw); fs.writeFileSync(path.join(sibling, '文件索引.json'), JSON.stringify({'0001_report.json': {'用途': '收尾_修复报告数据', SHA256: createHash('sha256').update(raw).digest('hex'), '字节数': raw.length}}));
  }
  const database = new DatabaseSync(databasePath);
  for (const [id, projectId, agentRunId] of [['current', 'alpha', 'run_current'], ['other', 'alpha', 'run_other'], ['cross_scope', 'beta', 'run_current']]) database.prepare('INSERT INTO console_runs VALUES (?,?)').run(id, JSON.stringify({id, projectId, agentRunId, reportRef: '0001_report.json', status: 'completed', startedAt: new Date().toISOString(), knowledge: [], logs: []}));
  database.close();
  const encoded = JSON.parse(JSON.stringify(service('artifact', {id: 'current', ref: '0005_image.png'})));
  assert.equal(encoded.encoding, 'base64'); assert.deepEqual(Buffer.from(encoded.content, 'base64'), png);
  const detail = service('run', {id: 'current'});
  assert.deepEqual(detail.issueReport, report);
  for (const key of ['check_plan', 'check_results', 'check_summary', 'overall_status', 'images', 'initial_check_results']) assert.deepEqual(detail[key], report[key]);
  for (const ref of ['0011_item_evidence.json', '0012_plan.json', '0013_result.json', '0014_image.png', '0015_initial_result.json', '0016_initial_evidence.json']) assert.ok(detail.artifacts.some(item => item.ref === ref), ref);
  const listener = createServer(); await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  const port = listener.address().port; await new Promise(resolve => listener.close(resolve));
  const child = spawn(process.execPath, [path.join(repo, 'backend/apps/console-api/server/index.mjs')], {cwd: repo, windowsHide: true, stdio: 'ignore', env: {...process.env, PORT: String(port), TRACEFIX_CONTROL_HOST: '127.0.0.1', TRACEFIX_PROJECTS: profiles, TRACEFIX_DATA: root, TRACEFIX_CONSOLE_DB: databasePath, BUGBOARD_DATA: path.join(root, 'tasks.json')}});
  context.after(() => child.kill());
  const base = 'http://127.0.0.1:' + port + '/api/runs/current/artifacts/';
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {try {ready = (await fetch(base + '0001_report.json')).ok;} catch {} if (ready) break; await new Promise(resolve => setTimeout(resolve, 50));}
  assert.equal(ready, true);
  assert.equal((await (await fetch(base.replace('/artifacts/', ''))).json()).overall_status, 'FAILED');
  for (const [ref, mime] of [['0001_report.json', 'application/json'], ['0002_check.json', 'application/json'], ['0006_report.html', 'text/html'], ['0005_image.png', 'image/png']]) {const response = await fetch(base + ref); assert.equal(response.status, 200); assert.ok(response.headers.get('content-type').startsWith(mime)); if (ref.endsWith('.png')) assert.deepEqual(Buffer.from(await response.arrayBuffer()), png);}
  const html = await fetch(base + '0006_report.html'); assert.match(html.headers.get('content-disposition'), /^inline/); assert.match(html.headers.get('content-security-policy'), /sandbox/);
  assert.match(html.headers.get('content-security-policy'), /img-src 'self' data:/);
  assert.equal((await (await fetch(base + '0001_report.json')).json()).scope_id, 'alpha');
  assert.equal((await (await fetch(base + '0004_observation.json')).json()).screenshot_ref, '0005_image.png');
  const markup = await html.text();
  for (const relative of [...markup.matchAll(/(?:href|src)="([^"]+)"/g)].map(match => match[1]).filter(ref => !ref.startsWith('data:'))) assert.equal((await fetch(new URL(relative, html.url))).status, 200);
  assert.match(markup, /data:image\/png;base64,/);
  for (const ref of ['0011_item_evidence.json', '0012_plan.json', '0013_result.json', '0014_image.png', '0015_initial_result.json', '0016_initial_evidence.json']) assert.equal((await fetch(base + ref)).status, 200, ref);
  const finished = JSON.parse(fs.readFileSync(path.join(directory, '0008_events.json'), 'utf8'))[0];
  artifact('0008_events.json', [{...finished, payload: {...finished.payload, report_ref: '0002_check.json'}}], '收尾_完整事件数据');
  assert.ok((await fetch(base + '0006_report.html')).status >= 400);
  artifact('0008_events.json', [finished], '收尾_完整事件数据');
  for (const ref of ['0007_private.json', '0008_events.json', '0009_unrelated.diff', '0010_unrelated.html', '..%2F..%2Fprojects.yaml']) assert.ok((await fetch(base + ref)).status >= 400);
  for (const id of ['other', 'cross_scope']) assert.equal((await fetch(base.replace('/current/', '/' + id + '/') + '0001_report.json')).status, 200);
  for (const id of ['other', 'cross_scope']) assert.ok((await fetch(base.replace('/current/', '/' + id + '/') + '0005_image.png')).status >= 400);
  const corrupted = Buffer.from(png); corrupted[0] ^= 1; fs.writeFileSync(path.join(directory, '0005_image.png'), corrupted);
  assert.ok((await fetch(base + '0005_image.png')).status >= 400);
  fs.writeFileSync(path.join(directory, '0005_image.png'), png);
  for (const fields of [{run_id: 'run_other'}, {scope_id: 'beta'}, {evidence_refs: ['0007_private.json']},
    {check_results: [{...checkResult, evidence_refs: ['0007_private.json']}]},
    {images: [{ref: '0007_private.json', mime: 'image/png'}]}]) {
    artifact('0001_report.json', {...report, ...fields}, '收尾_修复报告数据');
    assert.ok((await fetch(base + '0001_report.json')).status >= 400);
  }
  artifact('0001_report.json', {...report, evidence_refs: Array(513).fill('0002_check.json')}, '收尾_修复报告数据');
  assert.ok((await fetch(base + '0001_report.json')).status >= 400);
});
