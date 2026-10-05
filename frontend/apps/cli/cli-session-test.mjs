import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {test} from 'node:test';

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-cli-session-'));
const bundle = path.join(directory, 'session.mjs');
await build({entryPoints: [path.join(import.meta.dirname, 'src', 'cli-session.ts')], bundle: true, platform: 'node', format: 'esm', outfile: bundle});
const {CliSession, chatMessage} = await import(pathToFileURL(bundle).href);

test('每次启动默认没有 Run，且会话标识独立', () => {
  const first = new CliSession();
  const second = new CliSession();
  assert.equal(first.currentRunId('bugboard'), null);
  assert.notEqual(first.id, second.id);
  first.bindRun('bugboard', 'run-a');
  assert.equal(second.currentRunId('bugboard'), null);
});

test('显式绑定仅对当前项目可见，切项目清除绑定', () => {
  const session = new CliSession();
  session.bindRun('bugboard', 'run-a');
  assert.equal(session.currentRunId('bugboard'), 'run-a');
  assert.equal(session.currentRunId('other'), null);
  session.clearRun();
  assert.equal(session.currentRunId('bugboard'), null);
});

test('同一次回复的增量保持消息标识，工具事件独立且保留实际轮次', () => {
  const identity = {scope_id: 'bugboard', session_id: 'session-a', message_id: 'message-a'};
  const delta = chatMessage({...identity, type: 'chat.delta', delta: '第一段'}, 'bugboard', 'session-a', 1);
  const finished = chatMessage({...identity, type: 'chat.finished', content: '第一段完整'}, 'bugboard', 'session-a', 2);
  assert.equal(delta.id, finished.id);
  assert.equal(delta.event.payload.delta, '第一段');
  const tool = chatMessage({...identity, type: 'tool.completed', payload: {logical_exchange_id: 'message-a', tool_round: 0, tool_call_id: 'call-a', receipt: {isError: false}}}, 'bugboard', 'session-a', 3);
  assert.notEqual(tool.id, delta.id);
  assert.equal(tool.event.payload.logical_exchange_id, 'message-a');
  assert.equal(tool.event.payload.tool_round, 0);
});

test('错误会话或项目被拒绝，非公开记录不进入 UI', () => {
  const event = {type: 'chat.delta', scope_id: 'bugboard', session_id: 'session-a', message_id: 'message-a', delta: '正文'};
  assert.throws(() => chatMessage(event, 'other', 'session-a', 1), /不匹配/);
  assert.throws(() => chatMessage(event, 'bugboard', 'other', 1), /不匹配/);
  assert.equal(chatMessage({...event, payload: {visibility: 'hidden', delta: '私有'}}, 'bugboard', 'session-a', 1), null);
});
