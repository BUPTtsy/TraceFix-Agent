import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawn, spawnSync} from 'node:child_process';
import {aggregate, compileCases, oracleResult, version} from './contract.mjs';

export async function runAcceptance(input, output, {timeoutMs = 180000} = {}) {
  await fs.mkdir(output, {recursive: true});
  if ((await fs.readdir(output)).length) throw new Error('验收产物目录必须为空，防止旧证据重用');
  const requestPath = path.join(output, 'request.json');
  await fs.writeFile(requestPath, JSON.stringify(input, null, 2));
  let expected;
  try { expected = compileCases(input); }
  catch (error) {
    const result = {schema_version: version, status: 'inconclusive', passed: null,
      error: error.message, binding: input.binding || {}, cases: [], evidence_refs: [requestPath]};
    await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    return result;
  }
  const root = path.dirname(fileURLToPath(import.meta.url));
  if (process.env.TRACEFIX_ACCEPTANCE_CONTAINER === '1') {
    const builds = (await fs.readdir('/ms-playwright')).filter(name => /^chromium-\d+$/.test(name)).sort();
    const executable = builds.length && path.join('/ms-playwright', builds.at(-1), 'chrome-linux64/chrome');
    if (!executable) throw new Error('隔离浏览器镜像缺少 Chromium');
    await fs.access(executable);
    process.env.TRACEFIX_ACCEPTANCE_CHROMIUM_PATH = executable;
  }
  const child = spawn(process.execPath, [path.join(root, 'node_modules/@playwright/test/cli.js'),
    'test', '--config', path.join(root, 'playwright.config.mjs')], {cwd: root, windowsHide: true,
    env: {...process.env, TRACEFIX_ACCEPTANCE_REQUEST: requestPath, TRACEFIX_ACCEPTANCE_OUTPUT: output},
    detached: process.platform !== 'win32',
    stdio: ['ignore', 'pipe', 'pipe']});
  let stdout = '', stderr = '', timedOut = false;
  child.stdout.on('data', data => {stdout += data;});
  child.stderr.on('data', data => {stderr += data;});
  const timer = setTimeout(() => {
    timedOut = true;
    if (process.platform === 'win32') spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {windowsHide: true});
    else {try {process.kill(-child.pid, 'SIGKILL');} catch {child.kill();}}
  }, timeoutMs);
  let exitCode;
  try { exitCode = await new Promise((resolve, reject) => {child.once('close', resolve); child.once('error', reject);}); }
  finally { clearTimeout(timer); }
  await fs.writeFile(path.join(output, 'stdout.log'), stdout);
  await fs.writeFile(path.join(output, 'stderr.log'), stderr);
  const cases = [];
  for (const [index, item] of expected.entries()) {
    try { cases.push(JSON.parse(await fs.readFile(path.join(output, `case-${index}.json`), 'utf8'))); }
    catch { cases.push({id: item.id, kind: item.kind, scenario_hash: item.scenario_hash,
      status: 'inconclusive', passed: null, checks: [], evidence_refs: [], failure_kind: 'infrastructure',
      error: timedOut ? '验收总超时' : '场景未执行或缺少证据'}); }
  }
  const reportPath = path.join(output, 'playwright-report.json');
  let report;
  try { report = JSON.parse(await fs.readFile(reportPath, 'utf8')); } catch {}
  const collect = suites => suites.flatMap(suite => [...(suite.specs || []).map(spec => ({title: spec.title,
    results: (spec.tests || []).flatMap(test => test.results || [])})), ...collect(suite.suites || [])]);
  const reports = report ? collect(report.suites || []) : [];
  for (const item of cases) {
    const attachments = reports.filter(report => report.title === item.id).flatMap(test => test.results || []).flatMap(test => test.attachments || []);
    item.evidence_refs.push(...attachments.map(attachment => attachment.path).filter(Boolean));
    item.evidence_refs = [...new Set(item.evidence_refs)];
  }
  let status = aggregate(cases);
  if (!report || timedOut || exitCode !== 0 && status === 'pass') status = 'inconclusive';
  const result = {schema_version: version, status, passed: status === 'pass' ? true : status === 'fail' ? false : null,
    binding: input.binding, cases, exit_code: exitCode, timed_out: timedOut,
    evidence_refs: [requestPath, reportPath, path.join(output, 'stdout.log'), path.join(output, 'stderr.log'),
      ...cases.flatMap(item => item.evidence_refs)]};
  if (input.oracle_case_id && ['pass', 'fail'].includes(status)) result.oracle = oracleResult(result, input.oracle_case_id);
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let result;
  try {
    const input = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
    result = await runAcceptance(input, path.resolve(process.argv[3]));
  } catch (error) { result = {schema_version: version, status: 'inconclusive', passed: null, error: error.message}; }
  console.log(JSON.stringify(result.oracle || result));
  process.exitCode = result.status === 'pass' ? 0 : result.status === 'fail' ? 1 : 2;
}
