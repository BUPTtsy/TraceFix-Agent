import fs from 'node:fs';
import path from 'node:path';
import stripAnsi from 'strip-ansi';

let pty;
try { pty = await import('node-pty'); }
catch {
  console.log('TTY_SMOKE_SKIPPED: node-pty is not installed; no fake isTTY result reported');
  process.exit(0);
}

const root = path.resolve(import.meta.dirname, '../../..');
const cli = path.join(root, 'frontend', 'apps', 'cli', 'dist', 'cli.mjs');
if (!fs.existsSync(cli)) {
  console.error(`TTY_SMOKE_FAILED: missing ${cli}; run npm run build --workspace @tracefix/cli`);
  process.exit(1);
}

const data = fs.mkdtempSync(path.join(root, '.tmp-cli-tty-'));
const shell = pty.spawn(process.execPath, [cli, '--data', data, '--console-db', path.join(data, 'console.sqlite3')], {
  name: 'xterm-256color', cols: 80, rows: 24, cwd: root,
  env: {...process.env, TRACEFIX_CLI_PLAIN: '', NO_COLOR: '', TERM: 'xterm-256color'},
});
let output = '';
const waiters = [];
const exited = new Promise(resolve => shell.onExit(resolve));
let timeoutReject;
const timeout = new Promise((_, reject) => {timeoutReject = reject;});
let phase = 'startup';
const timer = setTimeout(() => {
  shell.kill();
  timeoutReject(new Error(`TTY_SMOKE_FAILED: ${phase} timeout, output=${JSON.stringify(output.slice(-1000))}`));
}, 35_000);
shell.onData(chunk => {
  output += chunk;
  for (const waiter of waiters.splice(0)) waiter();
});
const waitFor = async (pattern, start = 0) => {
  if (pattern.test(stripAnsi(output.slice(start)))) return;
  await Promise.race([new Promise(resolve => waiters.push(resolve)), timeout]);
  return waitFor(pattern, start);
};
const waitForRaw = async (pattern, start = 0) => {
  if (pattern.test(output.slice(start))) return;
  await Promise.race([new Promise(resolve => waiters.push(resolve)), timeout]);
  return waitForRaw(pattern, start);
};
const writeKey = async (value) => {
  shell.write(value);
  await new Promise(resolve => setTimeout(resolve, 80));
};
const wheel = async (direction, times = 1) => {
  const start = output.length;
  for (let index = 0; index < times; index++) await writeKey(`\x1b[<${direction === 'up' ? 64 : 65};1;1M`);
  return stripAnsi(output.slice(start));
};
const requireText = (value, pattern, label) => {
  if (!pattern.test(value)) throw new Error(`TTY_SMOKE_FAILED: ${label}, output=${JSON.stringify(value.slice(-1200))}`);
};
const latestFrame = value => {
  const plain = stripAnsi(value);
  const prompt = plain.lastIndexOf('❯');
  return prompt < 0 ? plain : plain.slice(Math.max(0, prompt - 3000), prompt + 120);
};
const submitEdit = async (value, keys, expected) => {
  phase = value;
  const start = output.length;
  if (/[^\x00-\x7f]/.test(value)) {
    for (const character of value) await writeKey(character);
  } else {
    shell.write(value);
    await waitFor(new RegExp(value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), start);
  }
  for (const key of keys) await writeKey(key);
  shell.write('\r');
  try { await waitFor(expected, start); }
  catch (error) {
    if (error instanceof Error) error.message += `; edit=${JSON.stringify(stripAnsi(output.slice(start)))}`;
    throw error;
  }
};
try {
await waitFor(/❯/);
await waitForRaw(/\x1b\[\?1000h\x1b\[\?1006h/);
phase = 'selection mode disables mouse and freezes frame';
const selectionStart = output.length;
await writeKey('\x13');
await waitFor(/选择模式：拖拽选中/, selectionStart);
await waitForRaw(/\x1b\[\?1006l\x1b\[\?1000l/, selectionStart);
requireText(output.slice(selectionStart), /\x1b\[\?1006l\x1b\[\?1000l/, 'selection mode must release native terminal mouse tracking');
await new Promise(resolve => setTimeout(resolve, 250));
const frozenStart = output.length;
await writeKey('should-not-edit');
await writeKey('\x03');
await new Promise(resolve => setTimeout(resolve, 300));
if (output.length !== frozenStart) throw new Error(`TTY_SMOKE_FAILED: selection mode redrew after input, output=${JSON.stringify(output.slice(frozenStart))}`);
const liveStart = output.length;
await writeKey('\x13');
await waitForRaw(/\x1b\[\?1000h\x1b\[\?1006h/, liveStart);
requireText(output.slice(liveStart), /\x1b\[\?1000h\x1b\[\?1006h/, 'leaving selection mode must restore global wheel tracking');
if (/should-not-edit/.test(stripAnsi(output.slice(liveStart)))) throw new Error('TTY_SMOKE_FAILED: selection-mode keyboard text leaked into input');
phase = 'help direct body and wheel';
const helpStart = output.length;
shell.write('/help\r');
await waitFor(/别名：/, helpStart);
if (/cli\.output:/.test(stripAnsi(output.slice(helpStart)))) throw new Error('TTY_SMOKE_FAILED: help is still wrapped in cli.output');
const helpTop = await wheel('up', 25);
requireText(helpTop, /会话|显示分组命令帮助/, 'help wheel must reveal first help rows');
const helpTopBoundary = await wheel('up', 4);
if (helpTopBoundary.trim()) throw new Error(`TTY_SMOKE_FAILED: help top boundary moved, output=${JSON.stringify(helpTopBoundary.slice(-500))}`);
const helpBottom = await wheel('down', 25);
requireText(helpBottom, /别名：/, 'help wheel must return to last help rows');
const helpBottomBoundary = await wheel('down', 4);
if (helpBottomBoundary.trim()) throw new Error(`TTY_SMOKE_FAILED: help bottom boundary moved, output=${JSON.stringify(helpBottomBoundary.slice(-500))}`);
await submitEdit('/mode chatx', ['\x7f'], /新 Run 模式：chat/);
await submitEdit('/mode repairx', ['\b'], /新 Run 模式：repair/);
await submitEdit('/mode teXst', ['\x1b[D', '\x1b[D', '\x1b[D', '\x1b[3~'], /新 Run 模式：test/);
await submitEdit('/mode chXat', ['\x1b[D', '\x1b[D', '\x7f'], /新 Run 模式：chat/);
await submitEdit('/mode test', [], /新 Run 模式：test/);
await submitEdit('/projects list 回归中文中', ['\x7f'], /bugboard/);
await submitEdit('/projects list 👩‍💻x', ['\x7f'], /bugboard/);
phase = 'resize and error';
shell.resize(100, 30);
shell.write('/mode invalid\r');
await waitFor(/用法：\/mode test\|repair\|chat/);
shell.write('/quit\r');
const exit = await Promise.race([exited, timeout]);
clearTimeout(timer);
if (exit.exitCode !== 0 || !/项目与知识|别名：/.test(output) || !/用法：\/mode/.test(output) || !/❯/.test(output) || /Invalid hook call|ReferenceError/.test(output)) {
  console.error(`TTY_SMOKE_FAILED: exit=${exit.exitCode} output=${JSON.stringify(output.slice(-500))}`);
  process.exit(1);
}
console.log('TTY_SMOKE_PASSED: real node-pty ConPTY selection mouse release/frame freeze/mouse restore/direct help/DEL Backspace/BS Backspace/Forward Delete/middle caret/CJK/emoji/help wheel/command editing/clamped viewport/error/resize/quit observed; native OS drag and clipboard not exercised');
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  shell.kill();
}
process.exit(process.exitCode || 0);
