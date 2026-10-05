import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';

const root = path.resolve(import.meta.dirname);
const output = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-presentation-')), 'presentation.mjs');
await build({entryPoints: [path.join(root, 'src', 'claude-ui', 'presentation.ts')], bundle: true, platform: 'node', format: 'esm', outfile: output});
const {appendUiMessage, messageText, metadataLines, visibleMessageRows} = await import(pathToFileURL(output).href);

const message = (seq, type, payload, extra = {}) => ({
  id: `${seq}`, kind: type.startsWith('tool.') ? 'tool' : 'event', text: `${type}: ${payload.status || ''}`,
  event: {scope_id: 'scope', run_id: 'run', seq, phase: 'VERIFY', type, payload}, metadata: {...payload, ...extra},
});

const running = message(1, 'state.changed', {status: 'RUNNING'});
const duplicate = message(2, 'state.changed', {status: 'RUNNING'});
assert.equal(appendUiMessage([running], duplicate).length, 1);
assert.match(messageText(message(3, 'run.finished', {status: 'COMPLETED', outcome: 'FIX_VERIFIED'})), /修复已验证（FIX_VERIFIED）/);
assert.match(messageText(message(4, 'tool.completed', {tool_call_id: 'call-1', receipt: {error_code: 'UNKNOWN'}})), /结果未知，需核对/);
assert.match(messageText(message(4, 'tool.completed', {tool_call_id: 'call-2', receipt: {passed: false}})), /结果未通过/);
assert.match(messageText(message(5, 'gate.decided', {passed: false, validation: '回归测试'})), /未通过/);
assert.deepEqual(metadataLines(message(6, 'context.assembled', {selected: [{ref: 'ctx-1', version: 2}], dropped: [{ref: 'ctx-2'}], off: false, snapshot_ref: 'snapshot-1'})), ['选用 1 项：ctx-1@2', '未选用 1 项：ctx-2', '工作集已启用', '内容快照：snapshot-1']);
assert.match(metadataLines(message(7, 'tool.completed', {receipt: {observation_ref: 'observation-1'}})).join('\n'), /页面观察：observation-1/);
const rows = [message(1, 'cli.output', {message: '第一行'}), message(2, 'cli.output', {message: '第二行'}), message(3, 'cli.output', {message: '第三行'})];
assert.equal(visibleMessageRows(rows, 80, false, 0, 2).map(item => item.message.id).join(','), '2,3');
assert.equal(visibleMessageRows(rows, 80, false, 1, 2).map(item => item.message.id).join(','), '1,2');
console.log('PRESENTATION_TEST_PASSED: state/tool/gate/final text, metadata, dedupe, and anchored viewport verified');
