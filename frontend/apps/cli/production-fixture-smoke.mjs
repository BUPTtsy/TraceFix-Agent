import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import stripAnsi from 'strip-ansi';
import {DatabaseSync} from 'node:sqlite';

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
const signalFile = path.join(data, 'fixture-signal');
const child = pty.spawn(process.execPath, [
  '--import', `file://${preload.replace(/\\/g, '/')}`,
  cli, '--data', data, '--console-db', database, '--mode', 'test',
], {
  name: 'xterm-256color',
  cols: 100,
  rows: 30,
  cwd: root,
  env: {...process.env, TRACEFIX_CLI_FIXTURE: '1', TRACEFIX_CLI_FIXTURE_SIGNAL: signalFile, NO_COLOR: '', TERM: 'xterm-256color'},
});

let output = '';
const waiters = [];
const exited = new Promise(resolve => child.onExit(resolve));
let timeoutReject;
const timeout = new Promise((_, reject) => { timeoutReject = reject; });
let phase = 'startup';
const timer = setTimeout(() => {
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
const databaseHas = pattern => {
  const connection = new DatabaseSync(database);
  try {
    return connection.prepare('SELECT data FROM console_events').all().some(row => pattern.test(String(row.data)));
  } finally {connection.close();}
};
const waitForDatabase = async pattern => {
  for (let attempt = 0; attempt < 40; attempt++) {
    if (databaseHas(pattern)) return;
    await new Promise(resolve => setTimeout(resolve, 40));
  }
  throw new Error(`fixture did not persist expected event ${pattern}`);
};

try {
  await waitFor(/❯/);
  phase = 'streaming public event presentation';
  const runStart = output.length;
  child.write('fixture controlled goal\r');
  await waitFor(/fixture stdout/);
  await waitFor(/本次调用了3个工具，成功数1，失败数1，未知数1/, runStart);
  await waitFor(/fixture-approval/, runStart);
  await waitFor(/fixture-continuation/, runStart);
  await waitFor(/等待审批/, runStart);
  await waitFor(/已继续执行/, runStart);
  const publicDefault = stripAnsi(output.slice(runStart));
  if (/cli\.output:|state\.changed|model\.|调用完成/.test(publicDefault)) throw new Error('default UI still exposes raw internal event rows');
  await waitForDatabase(/model\.tool\.result\.persisted/);
  phase = 'selection mode buffers true persisted events without redraw or cancellation';
  const selectionStart = output.length;
  await writeKey('\x13');
  await waitFor(/选择模式：拖拽选中/, selectionStart);
  requireText(output.slice(selectionStart), /\x1b\[\?1006l\x1b\[\?1000l/, 'selection must disable mouse tracking');
  await new Promise(resolve => setTimeout(resolve, 250));
  const frozenStart = output.length;
  fs.writeFileSync(signalFile, 'selection');
  await waitForDatabase(/fixture event received while selecting/);
  await writeKey('\x03');
  await new Promise(resolve => setTimeout(resolve, 1000));
  if (output.length !== frozenStart) throw new Error(`selection viewport redrew, output=${JSON.stringify(output.slice(frozenStart))}`);
  if (databaseHas(/fixture cancel acknowledged/)) throw new Error('Ctrl+C cancelled while in selection mode');
  const selectionExit = output.length;
  await writeKey('\x1b');
  await waitFor(/fixture event received while selecting/, selectionExit);
  requireText(output.slice(selectionExit), /\x1b\[\?1000h\x1b\[\?1006h/, 'selection exit must restore wheel tracking');
  phase = 'expanded true tool payload and model refs';
  const toolDetailsStart = output.length;
  await writeKey('\x0f');
  const requiredDetails = [/公开事件：tool\.completed/, /fixture-event-only-observation/, /fixture-result-fixture-unknown-call/];
  for (let page = 0; page < 25 && requiredDetails.some(pattern => !pattern.test(stripAnsi(output.slice(toolDetailsStart)))); page++) {
    await writeKey('\x1b[5~');
  }
  for (const pattern of requiredDetails) requireText(stripAnsi(output.slice(toolDetailsStart)), pattern, 'expanded public tool payload must remain browsable');
  await writeKey('\x0f');
  phase = 'running ordinary event history wheel';
  const historyTop = await wheel('up', 30);
  requireText(historyTop, /fixture history-00/, 'running wheel must reveal ordinary event history start');
  const historyTopBoundary = await wheel('up', 4);
  void historyTopBoundary;
  const historyBottom = await wheel('down', 30);
  requireText(historyBottom, /本次调用了3个工具|工具结果未知|fixture event received while selecting/, 'running wheel must return to recent public events');
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
  await waitFor(/fixture stdout/, finishStart);
  await waitFor(/fixture-event-only-gate/, finishStart);
  await waitFor(/验证门禁 · 通过/, finishStart);
  await waitFor(/修复已验证/, finishStart);
  await waitFor(/fixture-event-only-report/, finishStart);
  phase = 'idle command after final event';
  const idleCommandStart = output.length;
  child.write('/mode repair\r');
  await waitFor(/新 Run 模式：repair/, idleCommandStart);
  phase = 'expanded public event details';
  const expandedStart = output.length;
  await new Promise(resolve => setTimeout(resolve, 300));
  await writeKey('\x0f');
  const finalDetails = [/公开事件：run\.finished/, /FIX_VERIFIED/];
  for (let page = 0; page < 8 && finalDetails.some(pattern => !pattern.test(stripAnsi(output.slice(expandedStart)))); page++) {
    await writeKey('\x1b[5~');
  }
  if (finalDetails.some(pattern => !pattern.test(stripAnsi(output.slice(expandedStart))))) {
    await writeKey('\x0f');
    await new Promise(resolve => setTimeout(resolve, 180));
    await writeKey('\x0f');
    for (let page = 0; page < 8 && finalDetails.some(pattern => !pattern.test(stripAnsi(output.slice(expandedStart)))); page++) {
      await writeKey('\x1b[5~');
    }
  }
  for (const pattern of finalDetails) requireText(stripAnsi(output.slice(expandedStart)), pattern, 'expanded final event and outcome must remain browsable');
  await writeKey('\x0f');
  child.write('/quit\r');
  const exit = await Promise.race([exited, timeout]);
  const required = [
    /fixture stdout/,
    /本次调用了3个工具，成功数1，失败数1，未知数1/,
    /fixture-approval/,
    /fixture-continuation/,
    /fixture cancel acknowledged/,
  ];
  if (exit.exitCode !== 0 || required.some(pattern => !pattern.test(output)) || /Invalid hook call|ReferenceError/.test(output)) {
    throw new Error(`exit=${exit.exitCode} output=${JSON.stringify(output.slice(-1600))}`);
  }
  console.log('PRODUCTION_FIXTURE_PASSED: real node-pty compact tool counts/default model filter/public details/selection frame freeze and persisted-event replay/gate/FIX_VERIFIED/cancel/approval/resume/global wheel fixture observed; native OS drag and clipboard not exercised');
} catch (error) {
  console.error(`PRODUCTION_FIXTURE_FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  child.kill();
}

process.exit(process.exitCode || 0);
