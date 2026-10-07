import http from 'node:http';
import fs from 'node:fs/promises';
import {existsSync, readFileSync} from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {spawn} from 'node:child_process';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {seed, validateTitle, applyFields} from './tasks.mjs';
import {errorWithContext} from './errors.mjs';
import {runChatCompletion} from './chat-completion.mjs';
import {childRunning, createAgentStopper, stopCapabilities} from './agent-stop.mjs';
import crypto from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const dataFile = process.env.BUGBOARD_DATA || path.join(os.tmpdir(), 'tracefix-bugboard-data.json');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../../frontend/apps/web/dist');
let tasks = seed();
try {tasks = JSON.parse(await fs.readFile(dataFile, 'utf8'));} catch {}
const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
const consoleBundlePath = path.join(os.tmpdir(), `tracefix-console-${process.pid}.mjs`);
await build({entryPoints: [path.join(projectRoot, 'backend/packages/console-service/src/dispatch.ts')],
  bundle: true, outfile: consoleBundlePath, platform: 'node', format: 'esm'});
const {createConsoleService} = await import(pathToFileURL(consoleBundlePath).href);
await fs.unlink(consoleBundlePath);
function loadProjectEnv() {
  const envFile = path.join(projectRoot, '.env');
  if (!existsSync(envFile)) return;
  for (const line of readFileSync(envFile, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const separator = trimmed.indexOf('=');
    if (separator < 1) continue;
    const key = trimmed.slice(0, separator).trim();
    let value = trimmed.slice(separator + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    if (!(key in process.env)) process.env[key] = value;
  }
}
loadProjectEnv();
process.env.TRACEFIX_DATA = path.resolve(projectRoot, process.env.TRACEFIX_DATA || '.tracefix');
process.env.TRACEFIX_CONSOLE_DB = path.resolve(projectRoot, process.env.TRACEFIX_CONSOLE_DB || path.join(process.env.TRACEFIX_DATA, 'console.sqlite3'));
const dataService = createConsoleService({projectsPath: path.resolve(projectRoot, process.env.TRACEFIX_PROJECTS || 'profiles/projects.yaml'),
  dataRoot: path.resolve(projectRoot, process.env.TRACEFIX_DATA), databasePath: path.resolve(projectRoot, process.env.TRACEFIX_CONSOLE_DB),
  displayRoot: projectRoot});
const bridge = (operation, fields) => Promise.resolve().then(() => dataService(operation, fields));
let agent = {status: 'idle', pid: null, goal: '', mode: 'test', startedAt: null, finishedAt: null, exitCode: null, signal: null, logs: []};
let agentProcess = null;
let launching = false;
const persist = () => fs.writeFile(dataFile, JSON.stringify(tasks));
let mutation = Promise.resolve();
async function body(req) {
  const chunks = []; let bytes = 0;
  for await (const chunk of req) {bytes += chunk.length; if (bytes > 1600000) throw Object.assign(new Error('请求内容过大'), {status: 413}); chunks.push(chunk);}
  try {return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');}
  catch (error) {if (error instanceof SyntaxError) throw Object.assign(new Error('请求 JSON 无效'), {status: 400}); throw error;}
}
function send(res, status, value) {res.writeHead(status, {'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(value));}
function loopback(req) {
  const address = req.socket.remoteAddress || '';
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1';
}
function controlAuthorized(req) {
  const configured = process.env.TRACEFIX_CONTROL_TOKEN || '';
  const authorization = req.headers.authorization || '';
  const supplied = authorization ? authorization.match(/^Bearer\s+(.+)$/i)?.[1] || '' : req.headers['x-tracefix-control-token'] || '';
  if (configured) {
    const expected = Buffer.from(configured);
    const actual = Buffer.from(String(supplied));
    if (expected.length !== actual.length || !crypto.timingSafeEqual(expected, actual)) return false;
  } else {
    if (!loopback(req)) return false;
    try {
      if (!['127.0.0.1', '[::1]', 'localhost'].includes(new URL(`http://${req.headers.host}`).hostname)) return false;
    } catch {return false;}
  }
  const origin = req.headers.origin;
  if (origin && origin !== `http://${req.headers.host}` && origin !== `https://${req.headers.host}`) return false;
  return true;
}
function requireControl(req, res) {
  if (controlAuthorized(req)) return true;
  send(res, 401, {error: '控制面需要有效凭据'});
  return false;
}
async function agentView() {
  const runId = agent.id;
  if (runId) {
    let record = await bridge('run', {id: runId});
    if (!agentProcess && record.pid && !isAlive(record.pid) && ['running', 'stopping'].includes(record.status)) record = await bridge('run.ended', {id: runId, error: 'Agent 进程已退出，未记录最终结果'});
    if (agent.id === runId) agent = {...agent, ...record};
  }
  return {...agent, ...stopCapabilities(agent, agentProcess, isAlive), logs: (agent.logs || []).slice(-80)};
}
function isAlive(pid) {try {process.kill(pid, 0); return true;} catch {return false;}}
const agentStopper = createAgentStopper({bridge, view: agentView,
  ownedProcess: () => ({id: agent.id, child: agentProcess}), isAlive});
function pythonCommand() {
  const candidates = process.platform === 'win32' ? ['.venv\\Scripts\\python.exe', 'py'] : ['.venv/bin/python', 'python3'];
  return candidates.find(candidate => candidate === 'py' || existsSync(path.join(projectRoot, candidate))) || candidates[candidates.length - 1];
}
async function startAgent(goal, mode, projectId, continuation = null, derived = null) {
  // 通过受控子进程启动 Agent；继续运行和派生运行都保留父 Run 标识，便于追溯完整轨迹。
  if (agentProcess || launching || (agent.pid && isAlive(agent.pid))) throw new Error('已有 Agent Run 正在执行');
  launching = true;
  try {
  agent = continuation ? await bridge('run.continue', {id: continuation.runId, instruction: continuation.instruction}) : await bridge('run.create', {goal, mode, projectId, ...(derived || {})});
  const python = pythonCommand();
  const args = [...(python === 'py' ? ['-3.12'] : []), 'tools/bootstrap/launch.py', '--plain',
    ...(continuation ? ['--continue-run', agent.id, '--instruction', continuation.instruction] : ['--run', '--goal', goal, '--mode', mode]),
    ...(derived?.parentRunId ? ['--parent-run', derived.parentRunId] : []),
    ...((derived?.additionalRuleIds || []).flatMap(ruleId => ['--rule', ruleId])),
    '--project', projectId, '--projects', agent.registry,
    ...(agent.profile ? ['--profile', agent.profile] : []), '--data', agent.dataRoot || process.env.TRACEFIX_DATA];
  const runId = agent.id;
  const processControlId = crypto.randomUUID();
  agent = await bridge('run.update', {id: runId, changes: {processControlId,
    stopRequestedAt: null, stopPreviousStatus: null, stopError: null}});
  const child = spawn(python, args, {cwd: projectRoot, env: {...process.env, TRACEFIX_CONSOLE_RUN_ID: runId,
    TRACEFIX_PROCESS_CONTROL_ID: processControlId, TRACEFIX_CONTINUATION_ID: continuation ? agent.continuationId : ''},
    windowsHide: true, detached: process.platform !== 'win32'});
  agentProcess = child;
  agent.pid = child.pid ?? null;
  let logs = [...(agent.logs || [])], flushTimer = null, processError = '';
  const flush = () => bridge('run.update', {id: runId, changes: {logs: [...logs]}});
  const capture = chunk => {
    let text = String(chunk).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '').replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]');
    for (const key of ['TRACEFIX_API_KEY', 'TRACEFIX_DATABASE_URL', 'TRACEFIX_GITHUB_TOKEN']) if (process.env[key]) text = text.replaceAll(process.env[key], '[REDACTED]');
    logs.push(text.slice(-8000)); logs = logs.slice(-120);
    if (!flushTimer) flushTimer = setTimeout(() => {flushTimer = null; flush().catch(error => console.error('后台写入 Agent 日志失败：', error));}, 500);
  };
  child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
  child.stdout.on('data', capture); child.stderr.on('data', capture);
  child.on('error', error => {processError = `Agent 进程启动或控制失败：${error.message}`; capture(processError);});
  child.on('close', async code => {
    clearTimeout(flushTimer);
    try {
      await flush(); const record = await bridge('run.ended', {id: runId, exitCode: code, error: processError});
      if (agent.id === runId && agentProcess === child) agent = {...agent, ...record};
    }
    catch (error) {console.error('后台记录 Agent 结果失败：', error);}
    finally {if (agentProcess === child) agentProcess = null;}
  });
  if (childRunning(child)) await bridge('run.update', {id: runId, changes: {pid: child.pid}});
  } finally {launching = false;}
}
async function chatCompletion(message, history, projectId, useKnowledge, res) {
  return runChatCompletion({message, history, projectId, useKnowledge, res, bridge, persistAudit: audit => {
    const database = new DatabaseSync(process.env.TRACEFIX_CONSOLE_DB, {timeout: 15000});
    try {
      database.exec('CREATE TABLE IF NOT EXISTS chat_model_exchanges (id TEXT PRIMARY KEY, project_id TEXT, data TEXT NOT NULL)');
      let data = JSON.stringify(audit).replace(/\bsk-[A-Za-z0-9_-]{20,}\b/g, '[REDACTED]');
      for (const name of ['TRACEFIX_API_KEY', 'TRACEFIX_DATABASE_URL', 'TRACEFIX_GITHUB_TOKEN']) if (process.env[name]) data = data.replaceAll(process.env[name], '[REDACTED]');
      database.prepare('INSERT INTO chat_model_exchanges VALUES (?,?,?)').run(audit.id, projectId || null, data);
    } finally {database.close();}
  }});
}
const server = http.createServer(async (req,res) => {
  const requestId = crypto.randomUUID();
  try {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname === '/health') return send(res, 200, {ok:true});
    if (url.pathname === '/version') return send(res, 200, {source_manifest:process.env.TRACEFIX_SOURCE || 'manual-development'});
    if ((url.pathname.startsWith('/api/') || url.pathname === '/__reset') && !requireControl(req, res)) return;
    if (url.pathname === '/api/services' && req.method === 'GET') return send(res, 200, [{id: 'test', label: 'Test 测试'}, {id: 'repair', label: 'Repair 修复'}, {id: 'chat', label: 'Chat 对话'}]);
    if (url.pathname === '/api/projects' && req.method === 'GET') return send(res, 200, await bridge('projects'));
    if (url.pathname === '/api/runs' && req.method === 'GET') return send(res, 200, await bridge('runs', {projectId: url.searchParams.get('projectId') || undefined}));
    const traceMatch = url.pathname.match(/^\/api\/runs\/([a-zA-Z0-9_-]+)\/trace$/);
    if (traceMatch && req.method === 'GET') return send(res, 200, await bridge('run.trace', {id: traceMatch[1], after: url.searchParams.get('after') || 0}));
    // 引导 API：GET 查询审计记录，POST 提交引导；L3 改目标必须再单独确认。
    const guidanceMatch = url.pathname.match(/^\/api\/(?:v1\/)?runs\/([a-zA-Z0-9_-]+)\/guidance(?:\/([a-zA-Z0-9_-]+)\/confirm)?$/);
    if (guidanceMatch && req.method === 'GET' && !guidanceMatch[2]) return send(res, 200, await bridge('run.guidance', {id: guidanceMatch[1]}));
    if (guidanceMatch && req.method === 'POST') {
      const fields = await body(req);
      if (guidanceMatch[2]) return send(res, 202, await bridge('run.guidance.confirm', {id: guidanceMatch[1], guidanceId: guidanceMatch[2]}));
      if (typeof fields.text !== 'string' || !fields.text.trim() || fields.text.trim().length > 2000) return send(res, 400, {error: '请输入 1-2000 字的引导'});
      if (fields.level !== undefined && !['hint', 'constraint', 'retarget'].includes(fields.level)) return send(res, 400, {error: '引导级别无效'});
      return send(res, 202, await bridge('run.guidance.submit', {...fields, id: guidanceMatch[1]}));
    }
    const runMatch = url.pathname.match(/^\/api\/runs\/([a-zA-Z0-9_-]+)(?:\/artifacts\/(.+))?$/);
    if (runMatch && req.method === 'GET') {
      if (!runMatch[2]) return send(res, 200, await bridge('run', {id: runMatch[1]}));
      const ref = decodeURIComponent(runMatch[2]);
      const artifact = await bridge('artifact', {id: runMatch[1], ref});
      const extension = path.extname(ref), inline = ['.html', '.png', '.json'].includes(extension);
      const mime = {'.html': 'text/html; charset=utf-8', '.png': 'image/png', '.json': 'application/json; charset=utf-8'};
      if (artifact.encoding !== 'base64') throw new Error('证据传输编码无效');
      res.writeHead(200, {'Content-Type': mime[extension] || 'text/plain; charset=utf-8',
        'Content-Disposition': `${inline ? 'inline' : 'attachment'}; filename*=UTF-8''${encodeURIComponent(ref)}`,
        'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        ...(extension === '.html' ? {'Content-Security-Policy': "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; sandbox"} : {})});
      return res.end(Buffer.from(artifact.content, 'base64'));
    }
    if (url.pathname === '/api/knowledge' && req.method === 'GET') return send(res, 200, await bridge('documents', {projectId: url.searchParams.get('projectId') || undefined, query: url.searchParams.get('q') || ''}));
    if (url.pathname === '/api/knowledge' && req.method === 'POST') {
      return send(res, 201, await bridge('document.save', {...await body(req), id: undefined}));
    }
    if (url.pathname === '/api/knowledge/search' && req.method === 'POST') return send(res, 200, await bridge('search', await body(req)));
    if (url.pathname === '/api/rules' && req.method === 'GET') return send(res, 200, await bridge('rules', {
      projectId: url.searchParams.get('projectId') || undefined,
      status: url.searchParams.get('status') || undefined, q: url.searchParams.get('q') || '',
      includeArchived: url.searchParams.get('includeArchived') === 'true'}));
    if (url.pathname === '/api/rules' && req.method === 'POST') return send(res, 201, await bridge('rule.create', await body(req)));
    if (url.pathname === '/api/rules/insights' && req.method === 'GET') return send(res, 200, await bridge('rule.insights', {
      from: url.searchParams.get('from') || undefined, to: url.searchParams.get('to') || undefined}));
    if (url.pathname === '/api/rule-sets' && req.method === 'GET') return send(res, 200, await bridge('rule-sets'));
    if (url.pathname === '/api/rule-sets' && req.method === 'POST') return send(res, 201, await bridge('rule-set.save', await body(req)));
    const ruleFindingsMatch = url.pathname.match(/^\/api\/rules\/([^/]+)\/findings$/);
    if (ruleFindingsMatch && req.method === 'GET') return send(res, 200, await bridge('rule.findings', {
      ruleId: decodeURIComponent(ruleFindingsMatch[1]), status: url.searchParams.get('status') || undefined,
      limit: url.searchParams.get('limit') || 100}));
    const ruleVersionsMatch = url.pathname.match(/^\/api\/rules\/([^/]+)\/versions$/);
    if (ruleVersionsMatch && req.method === 'GET') return send(res, 200, await bridge('rule.versions', {id: decodeURIComponent(ruleVersionsMatch[1])}));
    const rulePreviewMatch = url.pathname.match(/^\/api\/rules\/([^/]+)\/preview$/);
    if (rulePreviewMatch && req.method === 'GET') return send(res, 200, await bridge('rule.preview', {id: decodeURIComponent(rulePreviewMatch[1])}));
    const ruleActionMatch = url.pathname.match(/^\/api\/rules\/([^/:]+):([a-z]+)$/);
    if (ruleActionMatch && req.method === 'POST') {
      const id = decodeURIComponent(ruleActionMatch[1]); const action = ruleActionMatch[2]; const fields = await body(req);
      if (action === 'publish') return send(res, 200, await bridge('rule.status', {id, status: 'enabled', expectedVersion: fields.expectedVersion}));
      if (action === 'disable') return send(res, 200, await bridge('rule.status', {id, status: 'disabled', expectedVersion: fields.expectedVersion}));
      if (action === 'archive') return send(res, 200, await bridge('rule.status', {id, status: 'archived', expectedVersion: fields.expectedVersion}));
      if (action === 'rollback') return send(res, 200, await bridge('rule.rollback', {id, version: fields.version}));
    }
    const ruleMatch = url.pathname.match(/^\/api\/rules\/([^/]+)$/);
    if (ruleMatch && req.method === 'GET') return send(res, 200, await bridge('rule', {id: decodeURIComponent(ruleMatch[1])}));
    if (ruleMatch && req.method === 'PUT') return send(res, 200, await bridge('rule.update', {...await body(req), id: decodeURIComponent(ruleMatch[1])}));
    if (ruleMatch && req.method === 'DELETE') return send(res, 200, await bridge('rule.delete', {id: decodeURIComponent(ruleMatch[1])}));
    const documentMatch = url.pathname.match(/^\/api\/knowledge\/([a-zA-Z0-9_-]+)$/);
    if (documentMatch && req.method === 'GET') return send(res, 200, await bridge('document', {id: documentMatch[1]}));
    if (documentMatch && req.method === 'PUT') {
      return send(res, 200, await bridge('document.save', {...await body(req), id: documentMatch[1]}));
    }
    if (url.pathname === '/api/agent/status' && req.method === 'GET') return send(res, 200, await agentView());
    if (url.pathname === '/api/agent/start' && req.method === 'POST') {
      const fields = await body(req); const goal = typeof fields.goal === 'string' ? fields.goal.trim() : '';
      if (!goal || goal.length > 4000) return send(res, 400, {error: '请输入 1-4000 字的修复目标'});
      if (!['test', 'repair'].includes(fields.mode)) return send(res, 400, {error: '未知 Agent 服务'});
      await startAgent(goal, fields.mode, fields.projectId);
      return send(res, 202, await agentView());
    }
    const continuationMatch = url.pathname.match(/^\/api\/runs\/([a-zA-Z0-9_-]+)\/continue$/);
    if (continuationMatch && req.method === 'POST') {
      const fields = await body(req); const instruction = typeof fields.instruction === 'string' ? fields.instruction.trim() : '';
      if (!instruction || instruction.length > 4000) return send(res, 400, {error: '请输入 1-4000 字的继续指令'});
      const record = await bridge('run', {id: continuationMatch[1]});
      if (!record.canContinue) return send(res, 409, {error: '只有非成功结束的任务可以继续'});
      await startAgent(record.goal, record.mode, record.projectId, {runId: record.id, instruction});
      return send(res, 202, await agentView());
    }
    const deriveMatch = url.pathname.match(/^\/api\/runs\/([a-zA-Z0-9_-]+)\/derive$/);
    if (deriveMatch && req.method === 'POST') {
      const fields = await body(req); const parent = await bridge('run', {id: deriveMatch[1]});
      const additionalRuleIds = fields.additionalRuleIds ?? [];
      if (fields.mode !== undefined && !['test', 'repair'].includes(fields.mode)) return send(res, 400, {error: '未知 Agent 服务'});
      await startAgent(typeof fields.goal === 'string' && fields.goal.trim() ? fields.goal.trim() : parent.goal,
        fields.mode || (parent.mode === 'repair' ? 'repair' : 'test'), parent.projectId, null,
        {parentRunId: parent.agentRunId || parent.id, additionalRuleIds});
      return send(res, 202, await agentView());
    }
    if (url.pathname === '/api/agent/stop' && req.method === 'POST') {
      const result = await agentStopper.stop(); return send(res, result.status, result.agent);
    }
    if (url.pathname === '/api/chat' && req.method === 'POST') {
      const fields = await body(req); const message = typeof fields.message === 'string' ? fields.message.trim() : '';
      if (!message || message.length > 8000) return send(res, 400, {error: '请输入 1-8000 字的消息'});
      return await chatCompletion(message, fields.history, fields.projectId, fields.useKnowledge === true, res);
    }
    if (url.pathname === '/__reset' && req.method === 'POST') {tasks=seed();await persist();return send(res,200,{ok:true});}
    if (url.pathname === '/api/tasks' && req.method === 'GET') return send(res,200,tasks);
    if (url.pathname === '/api/tasks' && req.method === 'POST') {
      const fields = await body(req);const title = validateTitle(fields.title);
      mutation = mutation.then(async () => {const task={id:Math.max(0,...tasks.map(t=>t.id))+1,title,status:'Todo'};tasks=[task,...tasks];await persist();send(res,201,task);});
      await mutation;return;
    }
    if (process.env.NODE_ENV === 'development' && !url.pathname.startsWith('/api/')) {
      res.writeHead(302, {Location: `http://127.0.0.1:5173${url.pathname}${url.search}`});
      return res.end();
    }
    const match = url.pathname.match(/^\/api\/tasks\/(\d+)$/);
    if (match) {
      const task = tasks.find(t=>t.id===Number(match[1]));if(!task) return send(res,404,{error:'找不到任务'});
      if(req.method==='GET') return send(res,200,task);
      if(req.method==='PATCH') {const next=applyFields(task,await body(req));tasks=tasks.map(t=>t.id===task.id?next:t);await persist();return send(res,200,next);}
      if(req.method==='DELETE') {tasks=tasks.filter(t=>t.id!==task.id);await persist();return send(res,200,{ok:true});}
    }
    if (url.pathname.startsWith('/api/')) return send(res,404,{error:'找不到接口'});
    const file = path.resolve(root, '.' + decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname));
    if(!file.startsWith(root+path.sep)) return send(res,403,{error:'禁止访问'});
    const content=await fs.readFile(file);
    res.writeHead(200,{'Content-Type':({'.html':'text/html','.js':'text/javascript','.css':'text/css'})[path.extname(file)]||'application/octet-stream','Cache-Control':'no-store'});res.end(content);
  } catch(e) {
    if (res.headersSent) return;
    const status = Number.isInteger(e.status) ? e.status : e.code === 'ENOENT' ? 404 : 500;
    if (status >= 500) console.error(`请求 ${requestId} 处理失败：`, e);
    send(res, status, {error: status >= 500 ? '服务器内部错误' : status === 404 ? '资源不存在' : e.message, requestId});
  }
});
await bridge('runs.import');
setInterval(() => bridge('runs.import').catch(error => console.error('后台导入运行记录失败：', error)), 60000).unref();
for (const record of await bridge('runs')) {
  if (['running', 'stopping', 'paused', 'waiting_input', 'waiting_approval'].includes(record.status)) {
    if (record.pid && isAlive(record.pid)) agent = {...record, logs: []};
    else await bridge('run.ended', {id: record.id, error: 'API 重启后检测到 Agent 进程已退出'});
  }
}
server.listen(Number(process.env.PORT || 3000), process.env.TRACEFIX_CONTROL_HOST || '127.0.0.1');
