import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';
import {test} from 'node:test';

const root = path.resolve(import.meta.dirname);
const bundle = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-events-')), 'events.mjs');
await build({entryPoints: [path.join(root, 'src', 'tracefix-events.ts')], bundle: true, platform: 'node', format: 'esm', outfile: bundle});
const {ChunkedTextDecoder, EventIntake, publicPayload} = await import(pathToFileURL(bundle).href);

const cursor = (scope_id, run_id, seq) => Buffer.from(JSON.stringify({scope_id, run_id, seq})).toString('base64url');
const row = (seq, type, payload, extra = {}) => ({scope_id: 'scope-a', run_id: 'run-a', seq, type, phase: 'VERIFY', revision: seq, at: seq, payload_json: JSON.stringify(payload), ...extra});

test('公开事件保留 Run/tool/error/cancel/resume/approval/unknown 与 T04-T09 refs', () => {
  const intake = new EventIntake('scope-a', 'run-a');
  const messages = intake.accept([
    row(1, 'run.started', {status: 'RUNNING', staged_ref: 'stage-1', before_hash: 'before', patch_hash: 'patch'}),
    row(2, 'tool.error', {error: 'tool failed', error_code: 'UNKNOWN'}),
    row(3, 'run.cancelled', {status: 'CANCELLED'}),
    row(4, 'run.continued', {continuation_ref: 'cont-1'}),
    row(5, 'approval.requested', {approval_ref: 'approval-1', patch_hash: 'patch'}),
    row(6, 'custom.future_event', {skill_ref: 'skill-1', context_ref: 'ctx-1', feedback_class: 'public'}),
  ]);
  assert.deepEqual(messages.map(message => message.kind), ['run', 'error', 'cancel', 'resume', 'approval', 'event']);
  assert.equal(messages[0].metadata.staged_ref, 'stage-1');
  assert.equal(messages[5].ref, 'skill-1');
  assert.equal(intake.after, 6);
});
test('nested receipt/result and T08 workset metadata remain visible without artifact reads', () => {
  const intake = new EventIntake('scope-a', 'run-a');
  const [message] = intake.accept([row(1, 'tool.completed', {
    receipt: {staged_ref: 'stage-1', overlay_revision: 4, before_hash: 'before', patch_hash: 'patch', internal_note: 'omit'},
    result: {error_code: 'UNKNOWN', operation_status: 'UNKNOWN', disk_before_hash: 'disk-before'},
    context: {selected: [{ref: 'ctx-1', version: 2}], dropped: [{ref: 'ctx-2'}], off: false, coverage: 'semantic_view', span: {start: 4, end: 9}},
    skill: {skill_hash: 'skill-hash', skill_version: 3, snapshot_ref: 'skill-snapshot', content_hash: 'skill-content-hash',
      references: [{path: 'references/checklist.md', content_hash: 'reference-content-hash', source_hash: 'reference-source-hash'}]},
    public_feedback: {feedback_class: 'counterevidence'},
  })]);
  assert.equal(message.metadata['receipt.staged_ref'], 'stage-1');
  assert.equal(message.metadata['receipt.overlay_revision'], 4);
  assert.equal(message.metadata['result.error_code'], 'UNKNOWN');
  assert.equal(message.metadata['context.coverage'], 'semantic_view');
  assert.deepEqual(message.metadata['context.span'], {start: 4, end: 9});
  assert.equal(message.metadata['skill.skill_hash'], 'skill-hash');
  assert.equal(message.metadata['skill.snapshot_ref'], 'skill-snapshot');
  assert.equal(message.metadata['skill.content_hash'], 'skill-content-hash');
  assert.equal(message.metadata['skill.references'][0].content_hash, 'reference-content-hash');
  assert.equal(message.metadata['skill.references'][0].source_hash, 'reference-source-hash');
  assert.equal(message.metadata['public_feedback.feedback_class'], 'counterevidence');
  assert.equal(message.metadata['receipt.internal_note'], undefined);
});

test('递归过滤 private/oracle/final scoring 整个记录并解析嵌套 JSON', () => {
  assert.equal(publicPayload({message: 'secret', nested: {visibility: 'hidden', value: 'oracle'}}), undefined);
  assert.equal(publicPayload({message: 'secret', nested: {source: 'final_scoring', value: 'oracle'}}), undefined);
  assert.equal(publicPayload({message: 'secret', source: 'final_oracle', before_hash: 'private'}), undefined);
  assert.equal(publicPayload({message: 'secret', provenance: 'heldout', before_hash: 'private'}), undefined);
  assert.equal(publicPayload({diagnostics: JSON.stringify({source: 'oracle', message: 'hidden'})}), undefined);
  assert.equal(publicPayload({diagnostics: JSON.stringify({provenance: 'final_scoring_only', message: 'hidden'})}), undefined);
  assert.equal(publicPayload({message: 'secret', source: 'final_oracle', before_hash: 'private'}), undefined);
  assert.equal(publicPayload({message: 'secret', provenance: 'heldout', before_hash: 'private'}), undefined);
  assert.equal(publicPayload({diagnostics: JSON.stringify({source: 'oracle', message: 'hidden'})}), undefined);
  assert.equal(publicPayload({diagnostics: JSON.stringify({provenance: 'final_scoring_only', message: 'hidden'})}), undefined);
  assert.deepEqual(publicPayload('{"message":"ok","meta":{"value":1}}'), {message: 'ok', meta: {value: 1}});
  const intake = new EventIntake('scope-a', 'run-a');
  assert.deepEqual(intake.accept([row(1, 'validation.feedback', {message: 'hidden', nested: {private: true, raw: 'secret'}})]), []);
});

test('批次 cursor 只校验水位，事件本身仍可消费；重复事件去重', () => {
  const intake = new EventIntake('scope-a', 'run-a');
  const batch = {events: [row(1, 'run.started', {status: 'RUNNING'}), row(2, 'tool.completed', {ref: 'tool-2'})],
    cursor: cursor('scope-a', 'run-a', 2), high_watermark: 2};
  assert.equal(intake.accept(batch).length, 2);
  assert.equal(intake.after, 2);
  assert.equal(intake.accept(batch).length, 0);
  assert.throws(() => intake.accept({...batch, high_watermark: 3}), /水位/);
  assert.throws(() => intake.accept({...batch, cursor: cursor('other', 'run-a', 2)}), /作用域/);
});

test('sequence gap 与 EventAdapter snapshot 需要新观察且不伪造成功', () => {
  const intake = new EventIntake('scope-a', 'run-a');
  assert.equal(intake.accept([row(1, 'run.started', {status: 'RUNNING'}), row(3, 'run.finished', {status: 'COMPLETED'})]).length, 2);
  assert.deepEqual(intake.gap, {expected: 2, received: 3, reason: 'sequence_gap'});
  assert.equal(intake.requiresNewObservation, true);
  const snapshot = {scope_id: 'scope-a', run_id: 'run-a', reason: 'cursor_expired', state: {status: 'UNKNOWN', hidden: true}};
  intake.accept({events: [], cursor: cursor('scope-a', 'run-a', 3), high_watermark: 3, snapshot, requires_new_observation: true});
  assert.equal(intake.snapshot.reason, 'cursor_expired');
  assert.equal(intake.snapshot.state, undefined);
});

test('scope/run mismatch is rejected and unknown event type remains visible', () => {
  const intake = new EventIntake('scope-a', 'run-a');
  assert.throws(() => intake.accept([row(1, 'run.started', {}, {scope_id: 'other'})]), /作用域/);
  assert.throws(() => intake.accept([row(1, 'run.started', {}, {run_id: 'other'})]), /Run/);
  assert.equal(intake.accept([row(1, 'unknown.future', {message: 'kept'})])[0].kind, 'event');
});

test('StringDecoder keeps UTF-8 and partial stdout lines across chunks', () => {
  const decoder = new ChunkedTextDecoder();
  const bytes = Buffer.from('工具开始：中文\n第二行\n尾部', 'utf8');
  const firstNewline = bytes.indexOf(10);
  assert.deepEqual(decoder.push(bytes.subarray(0, 7)), []);
  assert.deepEqual(decoder.push(bytes.subarray(7, firstNewline + 1)), ['工具开始：中文']);
  const secondNewline = bytes.indexOf(10, firstNewline + 1);
  assert.deepEqual(decoder.push(bytes.subarray(firstNewline + 1, secondNewline + 1)), ['第二行']);
  decoder.push(bytes.subarray(secondNewline + 1));
  assert.deepEqual(decoder.flush(), ['尾部']);
});

test('已构建 CLI 的非 TTY --command 保留纯文本帮助与错误码', () => {
  const projectRoot = path.resolve(root, '../../..');
  const cli = path.join(root, 'dist', 'cli.mjs');
  assert.equal(fs.existsSync(cli), true, '请先构建 CLI 后再运行 --command smoke');
  const data = fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-command-smoke-'));
  const options = ['--data', data, '--console-db', path.join(data, 'console.sqlite3')];
  const help = spawnSync(process.execPath, [cli, ...options, '--command', '/help'], {cwd: projectRoot, encoding: 'utf8'});
  assert.equal(help.status, 0, help.stderr);
  assert.match(help.stdout, /\/projects list\|show\|use/);
  assert.equal(help.stdout.includes('\u001b['), false);
  const error = spawnSync(process.execPath, [cli, ...options, '--command', '/nonexistent-command'], {cwd: projectRoot, encoding: 'utf8'});
  assert.equal(error.status, 2);
  assert.match(error.stderr, /未知命令/);
});
