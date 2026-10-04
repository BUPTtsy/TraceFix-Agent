import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

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
const timer = setTimeout(() => {
  child.kill();
  timeoutReject(new Error(`PRODUCTION_FIXTURE_FAILED: interactive timeout, output=${JSON.stringify(output.slice(-1200))}`));
}, 20_000);
child.onData(chunk => {
  output += chunk;
  for (const waiter of waiters.splice(0)) waiter();
});
const waitFor = async (pattern) => {
  if (pattern.test(output)) return;
  await Promise.race([new Promise(resolve => waiters.push(resolve)), timeout]);
  return waitFor(pattern);
};

try {
  await waitFor(/❯/);
  child.write('fixture controlled goal\r');
  child.write('/run\r');
  await waitFor(/fixture stdout/);
  await waitFor(/fixture tool\.error UNKNOWN/);
  await waitFor(/fixture WAITING_APPROVAL fixture-approval/);
  await waitFor(/fixture resume continuation/);
  child.write('\x03');
  await waitFor(/fixture cancel acknowledged/);
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
  console.log('PRODUCTION_FIXTURE_PASSED: real node-pty production UI/console DB event fixture observed');
} catch (error) {
  console.error(`PRODUCTION_FIXTURE_FAILED: ${error instanceof Error ? error.message : String(error)}`);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  child.kill();
}

process.exit(process.exitCode || 0);
