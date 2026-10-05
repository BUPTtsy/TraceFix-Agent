import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import stripAnsi from 'strip-ansi';

let pty;
try {
  pty = await import('node-pty');
} catch {
  console.log('PRODUCTION_FIXTURE_SKIPPED: node-pty is not installed; no fake TTY result reported');
  process.exit(0);
}

const root = path.resolve(import.meta.dirname, '../../..');
const cli = path.join(root, 'frontend', 'apps', 'cli', 'dist', 'cli.mjs');
const preload = path.join(root, 'frontend', 'apps', 'cli', 'production-fixture-preload.mjs');
if (!fs.existsSync(cli)) {
  console.error(`PRODUCTION_FIXTURE_FAILED: missing ${cli}; run npm run build --workspace @tracefix/cli`);
  process.exit(1);
}

const data = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-cli-production-fixture-'));
const database = path.join(data, 'console.sqlite3');
const child = pty.spawn(process.execPath, [
  '--import', `file://${preload.replace(/\\/g, '/')}`,
  cli, '--data', data, '--console-db', database,
], {
  name: 'xterm-256color',
  cols: 100,
  rows: 30,
  cwd: root,
  env: {...process.env, TRACEFIX_CLI_FIXTURE: '1', NO_COLOR: '', TERM: 'xterm-256color'},
});

let output = '';
const waiters = [];
const exited = new Promise(resolve => child.onExit(resolve));
let timeoutReject;
const timeout = new Promise((_, reject) => { timeoutReject = reject; });
let phase = 'startup';
const timer = setTimeout(() => {
  child.kill();
  timeoutReject(new Error(`PRODUCTION_FIXTURE_FAILED: ${phase} timeout, output=${JSON.stringify(output.slice(-1200))}`));
}, 40_000);
child.onData(chunk => {
  output += chunk;
  for (const waiter of waiters.splice(0)) waiter();
});
const waitFor = async (pattern, start = 0) => {
  if (pattern.test(stripAnsi(output.slice(start)))) return;
  await Promise.race([new Promise(resolve => waiters.push(resolve)), timeout]);
  return waitFor(pattern, start);
};
const writeKey = async value => {
  child.write(value);
  await new Promise(resolve => setTimeout(resolve, 80));
};
const wheel = async (direction, times = 1) => {
  const start = output.length;
  for (let index = 0; index < times; index++) await writeKey(`\x1b[<${direction === 'up' ? 64 : 65};1;1M`);
  return stripAnsi(output.slice(start));
};
const requireText = (value, pattern, label) => {
  if (!pattern.test(value)) throw new Error(`${label}, output=${JSON.stringify(value.slice(-1400))}`);
};
const latestFrame = value => {
  const plain = stripAnsi(value);
  const prompt = plain.lastIndexOf('❯');
  return prompt < 0 ? plain : plain.slice(Math.max(0, prompt - 3000), prompt + 120);
};

try {
  await waitFor(/❯/);
  phase = 'streaming public event presentation';
  const runStart = output.length;
  child.write('fixture controlled goal\r');
  child.write('/run\r');
  await waitFor(/fixture stdout/);
  await waitFor(/fixture tool\.error UNKNOWN/);
  await waitFor(/fixture WAITING_APPROVAL fixture-approval/);
  await waitFor(/fixture resume continuation/);
  await waitFor(/fixture-event-only-tool/, runStart);
  await waitFor(/fixture-event-only-observation/, runStart);
  await waitFor(/等待审批/, runStart);
  await waitFor(/已继续执行/, runStart);
  const publicDefault = stripAnsi(output.slice(runStart));
  if (/cli\.output:|state\.changed/.test(publicDefault)) throw new Error('default UI still exposes raw cli.output or repeated state.changed');
  phase = 'running ordinary event history wheel';
  const historyTop = await wheel('up', 30);
  requireText(historyTop, /fixture history-00/, 'running wheel must reveal ordinary event history start');
  const historyTopBoundary = await wheel('up', 4);
  void historyTopBoundary;
  const historyBottom = await wheel('down', 30);
  requireText(historyBottom, /fixture-event-only-tool|UNKNOWN|fixture resume continuation/, 'running wheel must return to recent public events');
  const historyBottomBoundary = await wheel('down', 4);
  void historyBottomBoundary;
  const inputStart = output.length;
  child.write('/mode test\r');
  await waitFor(/新 Run 模式：test/, inputStart);
  if (/未知命令/.test(stripAnsi(output.slice(inputStart)))) throw new Error('wheel sequence leaked into prompt input');
  phase = 'cancel';
  child.write('\x03');
  await waitFor(/fixture cancel acknowledged/);
  const finishStart = output.length;
  phase = 'verified gate and final event';
  await new Promise(resolve => setTimeout(resolve, 200));
  child.write('fixture finish goal\r');
  await waitFor(/目标已记录：fixture finish goal/, finishStart);
  child.write('/run\r');
  await waitFor(/fixture-event-only-gate/, finishStart);
  await waitFor(/验证门禁 · 通过/, finishStart);
  await waitFor(/修复已验证/, finishStart);
  await waitFor(/fixture-event-only-report/, finishStart);
  phase = 'expanded public event details';
  const expandedStart = output.length;
  await writeKey('\x0f');
  await waitFor(/公开事件：run\.finished/, expandedStart);
  requireText(stripAnsi(output.slice(expandedStart)), /FIX_VERIFIED/, 'expanded details must preserve actual public outcome');
  await writeKey('\x0f');
  child.write('/quit\r');
  const exit = await Promise.race([exited, timeout]);
  const required = [
    /fixture stdout/,
    /fixture tool\.error UNKNOWN/,
    /fixture WAITING_APPROVAL fixture-approval/,
    /fixture resume continuation/,
    /fixture cancel acknowledged/,
  ];
  if (exit.exitCode !== 0 || required.some(pattern => !pattern.test(output)) || /Invalid hook call|ReferenceError/.test(output)) {
    throw new Error(`exit=${exit.exitCode} output=${JSON.stringify(output.slice(-1600))}`);
  }
  console.log('PRODUCTION_FIXTURE_PASSED: real node-pty streaming/public tool/ref/gate/FIX_VERIFIED/cancel/approval/resume/running wheel fixture observed');
} catch (error) {
  console.error(`PRODUCTION_FIXTURE_FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  child.kill();
}

process.exit(process.exitCode || 0);
