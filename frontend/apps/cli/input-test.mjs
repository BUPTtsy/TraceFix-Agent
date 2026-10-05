import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import {build} from 'esbuild';

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-terminal-input-'));
const bundle = path.join(temporary, 'terminal-input.mjs');
await build({entryPoints: [path.join(import.meta.dirname, 'src', 'claude-ui', 'terminalInput.ts')],
  bundle: true, platform: 'node', format: 'esm', outfile: bundle});
const {normalizeTerminalInput} = await import(pathToFileURL(bundle).href);
const key = overrides => ({upArrow: false, downArrow: false, leftArrow: false, rightArrow: false,
  pageDown: false, pageUp: false, return: false, escape: false, ctrl: false,
  shift: false, tab: false, backspace: false, delete: false, meta: false, ...overrides});

test('DEL/Alt+DEL 退格与 BS 保持向后删除，真正 Delete 保持向前删除', () => {
  const [del] = normalizeTerminalInput('\x7f', '', key({delete: true}));
  assert.equal(del.key.backspace, true);
  assert.equal(del.key.delete, false);
  const [altDel] = normalizeTerminalInput('\x1b\x7f', '', key({delete: true, meta: true}));
  assert.equal(altDel.key.backspace, true);
  assert.equal(altDel.key.meta, true);
  const [backspace] = normalizeTerminalInput('\b', '', key({backspace: true}));
  assert.equal(backspace.key.backspace, true);
  const [forwardDelete] = normalizeTerminalInput('\x1b[3~', '', key({delete: true}));
  assert.equal(forwardDelete.key.delete, true);
  assert.equal(forwardDelete.key.backspace, false);
});

test('Home/End 原始序列补足公开 Ink 缺少的导航标记', () => {
  for (const sequence of ['\x1b[H', '\x1b[1~', '\x1b[7~', '\x1bOH', '\x1b[1;5H']) {
    assert.equal(normalizeTerminalInput(sequence, '', key())[0].key.home, true);
  }
  for (const sequence of ['\x1b[F', '\x1b[4~', '\x1b[8~', '\x1bOF', '\x1b[1;5F']) {
    assert.equal(normalizeTerminalInput(sequence, '', key())[0].key.end, true);
  }
});

test('SGR 与 legacy mouse 只传递 wheel，点击与释放不会插入输入', () => {
  const up = '\x1b[<64;1;1M';
  const down = '\x1b[<65;1;1M';
  const events = normalizeTerminalInput(up + up + down, '[<64;1;1M', key());
  assert.deepEqual(events.map(event => [event.input, event.key.wheelUp, event.key.wheelDown]),
    [['', true, false], ['', true, false], ['', false, true]]);
  for (const sequence of ['\x1b[<0;1;1M', '\x1b[<0;1;1m', '\x1b[<64;1;1m', '\x1b[M !!']) {
    const [event] = normalizeTerminalInput(sequence, sequence.slice(1), key());
    assert.equal(event.input, '');
    assert.equal(event.key.wheelUp, false);
    assert.equal(event.key.wheelDown, false);
  }
  assert.equal(normalizeTerminalInput('\x1b[M`!!', '[M`!!', key())[0].key.wheelUp, true);
  assert.equal(normalizeTerminalInput('\x1b[Ma!!', '[Ma!!', key())[0].key.wheelDown, true);
});

test('普通文本与完整 mouse 共块时只提交一次文本并清除解析残留标记', () => {
  const events = normalizeTerminalInput('before\x1b[<64;1;1Mafter', 'incorrect', key({delete: true, return: true}));
  assert.deepEqual(events.map(event => event.input), ['beforeafter', '']);
  assert.equal(events[0].key.delete, false);
  assert.equal(events[0].key.return, false);
  assert.equal(events[1].key.wheelUp, true);
  const [deleteEvent] = normalizeTerminalInput('\x1b[<64;1;1M\x1b[3~', 'incorrect', key());
  assert.equal(deleteEvent.input, '');
  assert.equal(deleteEvent.key.delete, true);
});
