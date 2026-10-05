import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {StringDecoder} from 'node:string_decoder';
import {DatabaseSync} from 'node:sqlite';
import stripAnsi from 'strip-ansi';

const root = path.resolve(import.meta.dirname, '../../..');
const cli = path.join(root, 'frontend/apps/cli/dist/cli.mjs');
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
assert.ok(fs.existsSync(cli), 'build @tracefix/cli before running chat smoke');
assert.ok(fs.existsSync(python), 'chat smoke requires the project Python environment');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-chat-smoke-'));
const projectRoot = path.join(data, 'project');
const filePath = path.join(projectRoot, 'src', 'sample.txt');
const projects = path.join(data, 'projects.json');
const database = path.join(data, 'console.sqlite3');
fs.mkdirSync(path.dirname(filePath), {recursive: true});
fs.writeFileSync(filePath, 'CHAT_CONTEXT_SENTINEL 中文\n', 'utf8');
fs.writeFileSync(projects, JSON.stringify({projects: [{id: 'chat-smoke', repo_id: 'chat-smoke',
  root: projectRoot, allowed_files: ['src/**'], memory_revision: 1, access_epoch: 1}]}));
const requests = [];
const holds = new Map();
const children = new Set();
const evidence = [];
const delay = duration => new Promise(resolve => setTimeout(resolve, duration));
const waitUntil = async (check, label, timeout = 12_000) => {
  const deadline = Date.now() + timeout;
  while (!check()) {
    if (Date.now() >= deadline) throw new Error(`CHAT_SMOKE_FAILED: ${label} timeout`);
    await delay(40);
  }
};
const sse = async (response, delta, finishReason = null) => {
  const value = {id: 'chat-smoke-completion', model: 'chat-smoke',
    choices: [{index: 0, delta, finish_reason: finishReason}]};
  const bytes = Buffer.from(`data: ${JSON.stringify(value)}\n\n`);
  const unicode = bytes.indexOf(Buffer.from('中'));
  const boundary = unicode < 0 ? Math.max(1, Math.floor(bytes.length / 2)) : unicode + 1;
  response.write(bytes.subarray(0, boundary));
  await delay(10);
  response.write(bytes.subarray(boundary));
};
const finish = async response => {
  await sse(response, {}, 'stop');
  response.end('data: [DONE]\n\n');
};
const tool = async (response, id, name, argumentsValue) => {
  await sse(response, {tool_calls: [{index: 0, id, type: 'function',
    function: {name, arguments: JSON.stringify(argumentsValue)}}]});
  await sse(response, {}, 'tool_calls');
  response.end('data: [DONE]\n\n');
};
const server = http.createServer(async (request, response) => {
  try {
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/chat/completions');
    let raw = '';
    for await (const fragment of request) raw += fragment;
    const payload = JSON.parse(raw);
    assert.equal(payload.stream, true);
    requests.push(payload);
    const names = payload.tools.map(entry => entry.function.name);
    for (const expected of ['Read', 'Glob', 'Grep']) assert.ok(names.includes(expected));
    assert.ok(!names.includes('Bash') && !names.includes('Write'));
    const lastUser = payload.messages.findLastIndex(entry => entry.role === 'user');
    const message = payload.messages[lastUser].content;
    if (message === 'force-error') {
      response.writeHead(503, {'Content-Type': 'application/json'});
      response.end(JSON.stringify({error: {message: 'fixture unavailable'}}));
      return;
    }
    response.writeHead(200, {'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-cache'});
    if (message === 'tool-flow') {
      const results = payload.messages.slice(lastUser + 1).filter(entry => entry.role === 'tool');
      if (!results.length) return await tool(response, 'smoke-read', 'Read', {file_path: filePath});
      assert.equal(results[0].tool_call_id, 'smoke-read');
      assert.ok(results[0].content.includes('CHAT_CONTEXT_SENTINEL'));
      const assistant = payload.messages.slice(lastUser + 1).find(entry => entry.tool_calls);
      assert.equal(assistant.tool_calls[0].id, results[0].tool_call_id);
      if (results.length === 1) return await tool(response, 'smoke-grep', 'Grep', {
        path: path.dirname(filePath), pattern: 'CHAT_CONTEXT_SENTINEL', output_mode: 'content'});
      assert.equal(results[1].tool_call_id, 'smoke-grep');
      assert.ok(results[1].content.includes('CHAT_CONTEXT_SENTINEL'));
      await sse(response, {content: '工具读取完成，'});
      await sse(response, {content: '中文结果🧪。'});
      await finish(response);
      return;
    }
    if (message === 'second-turn') {
      assert.ok(payload.messages.some(entry => entry.role === 'user' && entry.content === 'tool-flow'));
      assert.equal(payload.messages.filter(entry => entry.role === 'tool').length, 2);
      assert.ok(payload.messages.some(entry => entry.role === 'assistant' && entry.content === '工具读取完成，中文结果🧪。'));
    }
    if (message === 'fresh-turn') {
      assert.equal(payload.messages.filter(entry => entry.role === 'user').length, 1);
      assert.equal(payload.messages.filter(entry => entry.role === 'tool').length, 0);
    }
    if (message === 'tty-stream' || message === 'jsonl-stream' || message === 'cancel-turn') {
      let release;
      const held = {finished: false, release: () => release()};
      holds.set(message, held);
      const resumed = new Promise(resolve => {release = resolve;});
      response.on('close', held.release);
      await sse(response, {content: '首段中文🧪'});
      await resumed;
      if (response.destroyed) return;
      held.finished = true;
      const ending = message === 'tty-stream' ? '\n' + Array.from({length: 22}, (_, index) =>
        `chat-scroll-${String(index).padStart(2, '0')}`).join('\n') + '\nSTREAM_FINISHED' : ' STREAM_FINISHED';
      await sse(response, {content: ending});
      await finish(response);
      return;
    }
    await sse(response, {reasoning_content: 'HIDDEN_REASONING_SENTINEL'});
    await sse(response, {content: `回答：${message} 中文🧪`});
    await finish(response);
  } catch (error) {
    requests.push({fixture_error: error.message});
    response.destroy(error);
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
server.unref();
const env = {...process.env, PYTHONPATH: path.join(root, 'backend/packages/agent/src'),
  PYTHONIOENCODING: 'utf-8', TRACEFIX_API_KEY: 'chat-smoke-local-key',
  TRACEFIX_BASE_URL: `http://127.0.0.1:${server.address().port}`, TRACEFIX_TEXT_MODEL: 'chat-smoke',
  TRACEFIX_DATABASE_URL: '', TRACEFIX_THINKING: 'disabled', TRACEFIX_DATA: data,
  TRACEFIX_CONSOLE_DB: database, TRACEFIX_PROJECTS: projects, TRACEFIX_CLI_PLAIN: '',
  HTTP_PROXY: '', HTTPS_PROXY: '', ALL_PROXY: '', NO_PROXY: '127.0.0.1', NO_COLOR: '', TERM: 'xterm-256color'};
const argumentsValue = ['--project', 'chat-smoke', '--projects', projects, '--data', data, '--console-db', database];
const runCommand = async commands => {
  const child = spawn(process.execPath, [cli, ...argumentsValue, ...commands.flatMap(command => ['--command', command])],
    {cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  children.add(child);
  let stdout = '', stderr = '';
  child.stdout.setEncoding('utf8').on('data', chunk => {stdout += chunk;});
  child.stderr.setEncoding('utf8').on('data', chunk => {stderr += chunk;});
  const exit = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', code => {children.delete(child); resolve(code);});
  });
  const timer = setTimeout(() => child.kill(), 20_000);
  try {return {code: await exit, stdout, stderr};} finally {clearTimeout(timer);}
};
const fixtureConnection = () => new DatabaseSync(database);
const checkRunCount = () => {
  const connection = fixtureConnection();
  try {assert.equal(connection.prepare('SELECT COUNT(*) AS count FROM console_runs').get().count, 1);}
  finally {connection.close();}
};

try {
  const result = await runCommand(['tool-flow', 'second-turn']);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(result.stdout, '工具读取完成，中文结果🧪。\n回答：second-turn 中文🧪\n');
  assert.ok(!/目标已记录|创建.*Run|chat\.delta|tool\.(started|completed)|HIDDEN_REASONING/.test(result.stdout));
  assert.equal(requests.length, 4, JSON.stringify(requests));
  evidence.push('nonTTY --command: direct chat, two real tool rounds, tool bodies in next request, completed history in second turn');
  const connection = fixtureConnection();
  connection.prepare('INSERT INTO console_runs VALUES (?,?)').run('old-run', JSON.stringify({
    id: 'old-run', projectId: 'chat-smoke', agentRunId: 'old-agent-run', goal: 'OLD_RUN_SENTINEL', mode: 'test',
    status: 'completed', phase: 'FINALIZE', updatedAt: new Date().toISOString(), finishedAt: new Date().toISOString()}));
  connection.prepare('INSERT INTO console_events VALUES (?,?,?,?)').run('old-run', 1, 'old-event', JSON.stringify({
    run_id: 'old-agent-run', scope_id: 'chat-smoke', seq: 1, type: 'observation', payload: {message: 'OLD_RUN_SENTINEL'}}));
  connection.close();
  const quoted = await runCommand(["what's the result? \"open quote"]);
  assert.equal(quoted.code, 0, quoted.stderr);
  assert.equal(quoted.stdout, '回答：what\'s the result? "open quote 中文🧪\n');
  const fresh = await runCommand(['fresh-turn']);
  assert.equal(fresh.code, 0, fresh.stderr);
  assert.equal(fresh.stdout, '回答：fresh-turn 中文🧪\n');
  checkRunCount();
  evidence.push('restart: fresh model history, persisted old Run retained, no chat-created Run; ordinary unmatched quotes accepted');
  const failed = await runCommand(['force-error']);
  assert.equal(failed.code, 2);
  assert.match(failed.stderr, /HTTP 503/);
  assert.equal(failed.stdout, '');
  evidence.push('nonTTY model error: deterministic HTTP 503, no false final answer');

  const jsonl = spawn(python, ['tools/bootstrap/launch.py', '--plain', ...argumentsValue,
    '--mode', 'chat', '--chat-jsonl', '--chat-session', 'chat-smoke-jsonl'],
  {cwd: root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
  children.add(jsonl);
  const events = [];
  const decoder = new StringDecoder('utf8');
  let pending = '', errors = '';
  jsonl.stdout.on('data', chunk => {
    pending += decoder.write(chunk);
    let boundary;
    while ((boundary = pending.indexOf('\n')) >= 0) {
      events.push(JSON.parse(pending.slice(0, boundary)));
      pending = pending.slice(boundary + 1);
    }
  });
  jsonl.stderr.setEncoding('utf8').on('data', chunk => {errors += chunk;});
  const jsonlExit = new Promise(resolve => jsonl.once('close', code => {children.delete(jsonl); resolve(code);}));
  const send = value => jsonl.stdin.write(JSON.stringify(value) + '\n');
  await waitUntil(() => events.some(event => event.type === 'chat.ready'), `JSONL ready ${errors}`);
  send({type: 'message', id: 'stream-message', message: 'jsonl-stream', use_knowledge: false});
  await waitUntil(() => events.some(event => event.type === 'chat.delta' && event.delta === '首段中文🧪'), 'JSONL UTF-8 delta');
  assert.equal(holds.get('jsonl-stream').finished, false);
  assert.ok(!events.some(event => event.type === 'chat.finished'));
  holds.get('jsonl-stream').release();
  await waitUntil(() => events.some(event => event.type === 'chat.finished'), 'JSONL finish');
  send({type: 'message', id: 'cancel-message', message: 'cancel-turn', use_knowledge: false});
  await waitUntil(() => events.some(event => event.type === 'chat.delta' && event.message_id === 'cancel-message'), 'JSONL cancel first delta');
  send({type: 'cancel', id: 'cancel-message'});
  await waitUntil(() => events.some(event => event.type === 'chat.cancelled' && event.message_id === 'cancel-message'), 'JSONL cancellation');
  assert.ok(!events.some(event => event.type === 'chat.finished' && event.message_id === 'cancel-message'));
  send({type: 'message', id: 'error-message', message: 'force-error', use_knowledge: false});
  await waitUntil(() => events.some(event => event.type === 'chat.error' && event.message_id === 'error-message'), 'JSONL error');
  send({type: 'quit'});
  const jsonlExitCode = await Promise.race([jsonlExit, delay(5000).then(() => 'timeout')]);
  assert.equal(jsonlExitCode, 0, errors);
  evidence.push('real Python JSONL: split UTF-8 delta before finish, cancellation without final success, model error and clean quit');

  let pty;
  try {pty = await import('node-pty');} catch {}
  if (!pty) evidence.push('TTY SKIPPED: node-pty unavailable; no fake TTY evidence');
  else {
    const child = pty.spawn(process.execPath, [cli, ...argumentsValue],
      {name: 'xterm-256color', cols: 100, rows: 30, cwd: root, env});
    children.add(child);
    let output = '';
    child.onData(chunk => {output += chunk;});
    const exited = new Promise(resolve => child.onExit(value => {children.delete(child); resolve(value);}));
    const outputHas = (pattern, start = 0) => pattern.test(stripAnsi(output.slice(start)));
    const waitOutput = (pattern, label, start = 0) => waitUntil(() => outputHas(pattern, start),
      `${label}: ${JSON.stringify(stripAnsi(output.slice(-600)))}`);
    const key = async value => {child.write(value); await delay(90);};
    await waitOutput(/❯/, 'TTY startup');
    assert.match(stripAnsi(output), /chat/);
    assert.match(stripAnsi(output), /直接输入消息/);
    await delay(2100);
    assert.ok(!outputHas(/OLD_RUN_SENTINEL|目标已记录|输入目标，然后/));
    const streamStart = output.length;
    child.write('tty-stream\r');
    await waitOutput(/首段中文🧪/, 'TTY first streamed delta', streamStart);
    assert.equal(holds.get('tty-stream').finished, false);
    assert.ok(!outputHas(/STREAM_FINISHED/, streamStart));
    child.resize(88, 26);
    await delay(180);
    const selectionStart = output.length;
    await key('\x13');
    await waitOutput(/选择模式：拖拽选中/, 'TTY selection mode', selectionStart);
    assert.match(output.slice(selectionStart), /\x1b\[\?1006l\x1b\[\?1000l/);
    await delay(250);
    const frozen = output.length;
    holds.get('tty-stream').release();
    await delay(500);
    assert.equal(output.length, frozen, 'TTY selection must freeze while streamed completion arrives');
    await key('\x1b');
    await waitOutput(/STREAM_FINISHED/, 'TTY cached stream replay', frozen);
    assert.match(output.slice(frozen), /\x1b\[\?1000h\x1b\[\?1006h/);
    const scrollStart = output.length;
    for (let index = 0; index < 15; index++) await key('\x1b[<64;1;1M');
    await waitOutput(/chat-scroll-00/, 'TTY wheel reveals stream beginning', scrollStart);
    const recentStart = output.length;
    for (let index = 0; index < 15; index++) await key('\x1b[<65;1;1M');
    await waitOutput(/STREAM_FINISHED/, 'TTY wheel returns to stream end', recentStart);
    const cancelStart = output.length;
    child.write('cancel-turn\r');
    await waitOutput(/首段中文🧪/, 'TTY cancel streamed delta', cancelStart);
    await key('\x03');
    await waitOutput(/对话已取消/, 'TTY cancellation', cancelStart);
    const errorStart = output.length;
    child.write('force-error\r');
    await waitOutput(/对话失败.*HTTP 503/, 'TTY error', errorStart);
    assert.ok(!outputHas(/cli\.output:|chat\.delta:|state\.changed:|HIDDEN_REASONING/));
    child.write('/quit\r');
    const exit = await Promise.race([exited, delay(5000).then(() => ({exitCode: 'timeout'}))]);
    assert.equal(exit.exitCode, 0);
    checkRunCount();
    evidence.push('real node-pty TTY: fresh startup, direct chat, delta before finish, resize, frozen selection stream replay, global wheel, cancel/error/quit');
  }
  assert.ok(!requests.some(request => request.fixture_error), JSON.stringify(requests.filter(request => request.fixture_error)));
  console.log('CHAT_SMOKE_PASSED');
  for (const item of evidence) console.log(`  ${item}`);
  console.log(`  isolated evidence directory: ${data}`);
  console.log('  LIMIT: native OS mouse drag/clipboard and real provider/network not exercised; resume/approval remain in production-fixture-smoke');
} catch (error) {
  console.error(error.stack || String(error));
  console.error(`CHAT_SMOKE_EVIDENCE: ${data}`);
  process.exitCode = 1;
} finally {
  for (const held of holds.values()) held.release();
  for (const child of children) child.kill();
  server.closeAllConnections();
  server.close();
  setImmediate(() => process.exit(process.exitCode || 0));
}
