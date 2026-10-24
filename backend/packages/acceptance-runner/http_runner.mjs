import {request, expect} from '@playwright/test';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {requireIsolation, version} from './contract.mjs';

export async function runHttpAcceptance(input, output, {token = process.env.TRACEFIX_CONSOLE_API_TOKEN} = {}) {
  requireIsolation(input);
  if (!input.task?.execution || !input.repo_id || !input.commit) throw new Error('HTTP 验收缺少 Task/repo/revision');
  await fs.mkdir(output, {recursive: true});
  if ((await fs.readdir(output)).length) throw new Error('HTTP 证据目录必须为空');
  await fs.writeFile(path.join(output, 'request.json'), JSON.stringify(input, null, 2));
  const context = await request.newContext({baseURL: input.url, timeout: 10000,
    extraHTTPHeaders: token ? {authorization: `Bearer ${token}`} : {}});
  const requests = [], checks = [];
  const deadline = Date.now() + Math.min(input.timeout_ms || 120000, 300000);
  const maxRequests = Math.min(input.max_requests || 200, 300);
  const call = async (method, route, body, key) => {
    if (requests.length >= maxRequests || Date.now() >= deadline) throw new Error('HTTP 验收请求或时间额度耗尽');
    const response = await context.fetch(route, {method, data: body, maxRedirects: 0,
      headers: key ? {'idempotency-key': key} : {}});
    const text = await response.text();
    let json;
    try { json = JSON.parse(text); } catch {}
    requests.push({method, path: route, request: body || null,
      idempotency_key: key || null, status: response.status(), response: json || text});
    if (response.status() >= 500) throw new Error(`意外 HTTP ${response.status()}: ${route}`);
    return {status: response.status(), body: json};
  };
  const check = async (name, work) => {
    try {await work(); checks.push({name, passed: true});}
    catch (error) {checks.push({name, passed: false, error: error.message}); throw error;}
  };
  let started, result, status = 'inconclusive', error;
  try {
    let first, second;
    await check('create 幂等绑定同一 Task', async () => {
      first = await call('POST', '/api/v1/tasks', input.task, `accept-create-${input.run_key}`);
      second = await call('POST', '/api/v1/tasks', input.task, `accept-create-${input.run_key}`);
      expect(first.status).toBe(201); expect(second.status).toBe(201);
      expect(first.body.run).toBeNull(); expect(second.body.task.id).toBe(first.body.task.id);
    });
    await check('幂等正文冲突保持 409', async () => {
      const conflict = await call('POST', '/api/v1/tasks', {...input.task, goal: input.task.goal + ' conflicting'}, `accept-create-${input.run_key}`);
      expect(conflict.status).toBe(409); expect(conflict.body.code).toBe('IDEMPOTENCY_CONFLICT');
    });
    const other = await call('POST', '/api/v1/tasks', input.task, `accept-other-${input.run_key}`);
    await check('start 幂等且 Run 与仓库身份匹配', async () => {
      started = await call('POST', `/api/v1/tasks/${first.body.task.id}/start`, {}, `accept-start-${input.run_key}`);
      const replay = await call('POST', `/api/v1/tasks/${first.body.task.id}/start`, {}, `accept-start-${input.run_key}`);
      expect(started.status).toBe(202); expect(replay.status).toBe(202);
      expect(replay.body.run.id).toBe(started.body.run.id);
      expect(started.body.run.taskId).toBe(first.body.task.id); expect(started.body.run.projectId).toBe(input.repo_id);
    });
    await check('错 Task stop 不影响当前 Run', async () => {
      const stopped = await call('POST', `/api/v1/tasks/${other.body.task.id}/stop`, {});
      expect(stopped.status).toBe(200);
      const task = await call('GET', `/api/v1/tasks/${first.body.task.id}`);
      expect(task.body.currentRunId).toBe(started.body.run.id);
      if (input.require_running_stop) expect(task.body.status).toBe('running');
    });
    await check('权威 result 归属与 revision 一致', async () => {
      do {
        result = await call('GET', `/api/v1/runs/${started.body.run.id}/result`);
        expect([200, 202]).toContain(result.status);
        expect(result.body.runId).toBe(started.body.run.id); expect(result.body.taskId).toBe(first.body.task.id);
        expect(result.body.provenance.projectId).toBe(input.repo_id); expect(result.body.provenance.commit).toBe(input.commit);
        if (result.body.ready) break;
        if (input.cancel) await call('POST', `/api/v1/tasks/${first.body.task.id}/stop`, {});
        await new Promise(resolve => setTimeout(resolve, 500));
      } while (Date.now() < deadline);
      expect(result.body.ready).toBe(true);
      if (input.expected_outcome) expect(result.body.outcome).toBe(input.expected_outcome);
      if (result.body.outcome === 'UNKNOWN') {
        expect(result.body.settled).toBe(false); expect(result.body.error.retryable).toBe(false);
      } else expect(result.body.settled).toBe(true);
      if (result.body.outcome === 'FIX_VERIFIED') {
        expect(result.body.patch.available).toBe(true); expect(result.body.patch.verification).toBe('verified');
        expect(result.body.patch.diff).toBeTruthy();
      }
    });
    await check('刷新查询保持权威结果和证据', async () => {
      const refreshed = await call('GET', `/api/v1/runs/${started.body.run.id}/result`);
      expect(refreshed.body).toEqual(result.body);
      const findings = await call('GET', `/api/v1/runs/${started.body.run.id}/findings`);
      expect(findings.status).toBe(200); expect(Array.isArray(findings.body)).toBe(true);
      const trace = await call('GET', `/api/v1/runs/${started.body.run.id}/events`);
      expect(trace.status).toBe(200); expect(Array.isArray(trace.body)).toBe(true);
      await fs.writeFile(path.join(output, 'trace.json'), JSON.stringify(trace.body, null, 2));
      await fs.writeFile(path.join(output, 'diff.patch'), result.body.patch?.diff || '');
    });
    status = 'pass';
  } catch (failure) {status = checks.some(item => !item.passed) ? 'fail' : 'inconclusive'; error = failure.message;}
  finally {await context.dispose();}
  await fs.writeFile(path.join(output, 'requests.json'), JSON.stringify(requests, null, 2));
  const report = {schema_version: version, status, passed: status === 'pass' ? true : status === 'fail' ? false : null,
    binding: {repo_id: input.repo_id, commit: input.commit, run_id: started?.body?.run?.id || null}, checks, error,
    result: result?.body || null, evidence_refs: ['request.json', 'requests.json', 'trace.json', 'diff.patch'].map(name => path.join(output, name))};
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let result;
  try {result = await runHttpAcceptance(JSON.parse(await fs.readFile(process.argv[2], 'utf8')), path.resolve(process.argv[3]));}
  catch (error) {result = {status: 'inconclusive', passed: null, error: error.message};}
  console.log(JSON.stringify(result));
  process.exitCode = result.status === 'pass' ? 0 : result.status === 'fail' ? 1 : 2;
}
