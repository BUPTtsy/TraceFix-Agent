import fs from 'node:fs';
import path from 'node:path';

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
const timer = setTimeout(() => {
  shell.kill();
  timeoutReject(new Error(`TTY_SMOKE_FAILED: interactive timeout, output=${JSON.stringify(output.slice(-500))}`));
}, 15_000);
shell.onData(chunk => {
  output += chunk;
  for (const waiter of waiters.splice(0)) waiter();
});
const waitFor = async (pattern) => {
  if (pattern.test(output)) return;
  await Promise.race([new Promise(resolve => waiters.push(resolve)), timeout]);
  return waitFor(pattern);
};
await waitFor(/❯/);
shell.write('/help\r');
await waitFor(/项目与知识|别名：/);
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
console.log('TTY_SMOKE_PASSED: real node-pty ConPTY interactive help/error/resize/quit flow observed');
process.exit(0);
