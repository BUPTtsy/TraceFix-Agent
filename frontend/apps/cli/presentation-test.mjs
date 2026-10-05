import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {test} from 'node:test';

const root = path.resolve(import.meta.dirname);
const output = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'tracefix-presentation-')), 'presentation.mjs');
await build({entryPoints: [path.join(root, 'src', 'claude-ui', 'presentation.ts')], bundle: true, platform: 'node', format: 'esm', outfile: output});
const {appendUiMessage, messageLines, messageText, metadataLines, visibleMessageRows, selectUiMessages} = await import(pathToFileURL(output).href);

const message = (seq, type, payload, extra = {}) => ({
  id: `scope:run:${seq}`, kind: type.startsWith('tool.') ? 'tool' : type === 'approval.requested' ? 'approval' : 'event', text: `${type}: ${payload.status || ''}`,
  event: {scope_id: 'scope', run_id: 'run', seq, phase: 'VERIFY', type, payload},
  ref: payload.ref || payload.approval_ref || payload.report_ref || payload.evidence_ref,
  metadata: {...payload, ...extra},
});

const summaries = messages => messages.filter(item => messageText(item).includes('本次调用了'));
const render = (messages, expanded = false) => selectUiMessages(messages, expanded).flatMap(item => messageLines(item, 240, expanded)).join('\n');
const assertCounts = (item, expected) => {
  const value = messageText(item);
  for (const [label, count] of Object.entries(expected)) {
    if (count === 0 && !value.includes(label)) continue;
    assert.match(value, new RegExp(`${label}${count}(?:个工具)?(?:，|$|\\s)`), value);
  }
};

test('状态重复合并且终态、门禁、context 和公开引用仍可读', () => {
  const running = message(1, 'state.changed', {status: 'RUNNING'});
  const duplicate = message(2, 'state.changed', {status: 'RUNNING'});
  assert.equal(appendUiMessage([running], duplicate).length, 1);
  assert.match(messageText(message(3, 'run.finished', {status: 'COMPLETED', outcome: 'FIX_VERIFIED'})), /修复已验证（FIX_VERIFIED）/);
  assert.match(messageText(message(4, 'tool.completed', {tool_call_id: 'call-1', receipt: {error_code: 'UNKNOWN'}})), /结果未知，需核对/);
  assert.match(messageText(message(5, 'gate.decided', {passed: false, validation: '回归测试'})), /未通过/);
  assert.deepEqual(metadataLines(message(6, 'context.assembled', {selected: [{ref: 'ctx-1', version: 2}], dropped: [{ref: 'ctx-2'}], off: false, snapshot_ref: 'snapshot-1'})), ['选用 1 项：ctx-1@2', '未选用 1 项：ctx-2', '工作集已启用', '内容快照：snapshot-1']);
  assert.match(metadataLines(message(7, 'tool.completed', {receipt: {observation_ref: 'observation-1'}})).join('\n'), /页面观察：observation-1/);
});

test('行视口按滚动位置返回真实内容并限制顶部底部', () => {
  const rows = [message(1, 'cli.output', {message: '第一行'}), message(2, 'cli.output', {message: '第二行'}), message(3, 'cli.output', {message: '第三行'})];
  const ids = scroll => visibleMessageRows(rows, 80, false, scroll, 2).map(item => item.message.id).join(',');
  assert.equal(ids(0), 'scope:run:2,scope:run:3');
  assert.equal(ids(1), 'scope:run:1,scope:run:2');
  assert.equal(ids(1000), ids(1));
  assert.equal(ids(-1000), ids(0));
});

test('model 内部生命周期默认隐藏，详情保留真实边界和引用', () => {
  const modelEvents = [
    message(10, 'model.started', {logical_exchange_id: 'exchange-1', tool_round: 0, attempt: 1, model: 'fixture-model'}),
    message(11, 'model.request.persisted', {logical_exchange_id: 'exchange-1', tool_round: 0, request_ref: 'request-1'}),
    message(12, 'model.reasoning.persisted', {logical_exchange_id: 'exchange-1', tool_round: 0, reasoning_ref: 'reasoning-1'}),
    message(13, 'model.response.persisted', {logical_exchange_id: 'exchange-1', tool_round: 0, response_ref: 'response-1'}),
    message(14, 'model.tool.result.persisted', {logical_exchange_id: 'exchange-1', tool_round: 1, tool_call_id: 'call-1', result_ref: 'result-1', reused: false}),
    message(15, 'model.usage', {usage: {total_tokens: 10}}),
    message(16, 'model.called', {model_revision: 'fixture-model', finish_reason: 'stop'}),
    message(17, 'model.decision', {summary: '继续验证', evidence_refs: ['decision-evidence']}),
  ];
  assert.doesNotMatch(render(modelEvents), /model\.|request-1|reasoning-1|response-1|result-1/);
  const expanded = render(modelEvents, true);
  for (const type of modelEvents.map(item => item.event.type)) assert.ok(expanded.includes(`公开事件：${type}`), type);
  assert.match(expanded, /logical_exchange_id/);
  assert.match(expanded, /response-1/);
});

test('同一工具批次成功、失败、未知、等待分别计数，UNKNOWN 不伪造成功或失败', () => {
  const rows = [
    message(20, 'model.started', {logical_exchange_id: 'exchange-tools', tool_round: 2}),
    message(21, 'tool.started', {tool_call_id: 'call-success', intent: {tool_name: 'code.read', arguments: {path: 'server/index.mjs'}}}),
    message(22, 'tool.completed', {tool_call_id: 'call-success', receipt: {call_id: 'call-success', name: 'code.read', isError: false, result: {ok: true}, observation_ref: 'observation-success'}}),
    message(23, 'tool.started', {tool_call_id: 'call-failed', intent: {tool_name: 'code.read'}}),
    message(24, 'tool.error', {tool_call_id: 'call-failed', receipt: {call_id: 'call-failed', name: 'code.read', isError: true, error: {type: 'PermissionError', message: 'fixture denied', executed: false}}}),
    message(25, 'tool.requested', {tool_call_id: 'call-unknown', intent: {tool_name: 'apply_patch'}}),
    message(26, 'tool.started', {operation_id: 'operation-unknown', intent: {tool_call_id: 'call-unknown', tool_name: 'apply_patch'}}),
    message(27, 'tool.unknown', {operation_id: 'operation-unknown', reason: 'network interrupted', resources: [{path: 'server/index.mjs'}]}),
    message(28, 'tool.requested', {tool_call_id: 'call-pending', intent: {tool_name: 'apply_patch'}}),
    message(29, 'approval.requested', {approval_ref: 'approval-1', status: 'WAITING_APPROVAL'}),
  ];
  const compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 4, '成功数': 1, '失败数': 1, '未知数': 1, '进行中': 1});
  const output = render(rows);
  assert.doesNotMatch(output, /调用完成|公开事件：tool|model\./);
  assert.match(output, /approval-1/);
  const expanded = render(rows, true);
  assert.match(expanded, /observation-success/);
  assert.match(expanded, /server\/index\.mjs/);
  assert.match(expanded, /operation-unknown/);
  assert.match(expanded, /fixture denied/);
});

test('isError、UNKNOWN 和人工核对按执行事实结算，业务断言未通过仍单独显示', () => {
  const rows = [
    message(40, 'model.started', {logical_exchange_id: 'exchange-reconcile', tool_round: 1}),
    message(41, 'tool.requested', {tool_call_id: 'call-operation', intent: {tool_name: 'apply_patch'}}),
    message(42, 'tool.started', {operation_id: 'op-1', intent: {tool_call_id: 'call-operation', tool_name: 'apply_patch'}}),
    message(43, 'tool.unknown', {operation_id: 'op-1', reason: 'not acknowledged'}),
    message(44, 'tool.reconciled', {operation_id: 'op-1', reviewer: 'user', receipt: {call_id: 'call-operation', name: 'apply_patch', isError: false, result: {success: true}}}),
    message(45, 'tool.completed', {tool_call_id: 'call-business-failure', receipt: {call_id: 'call-business-failure', name: 'validate', isError: false, passed: false}}),
    message(46, 'action.business.outcome', {tool_call_id: 'call-business-failure', passed: false, observation_ref: 'business-observation'}),
    message(47, 'tool.completed', {tool_call_id: 'call-error', receipt: {call_id: 'call-error', name: 'validate', isError: true, error: {message: 'failed'}}}),
  ];
  const compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 3, '成功数': 2, '失败数': 1, '未知数': 0, '进行中': 0});
  assert.match(render(rows), /业务验证 · 未通过/);
  assert.match(render(rows), /business-observation/);
});

test('批次边界按 exchange/round 区分，同 call ID 的重复事件不增量且不同 Run 隔离', () => {
  const rows = [
    message(60, 'model.started', {logical_exchange_id: 'exchange-a', tool_round: 0}),
    message(61, 'tool.completed', {tool_call_id: 'call-a', receipt: {call_id: 'call-a', isError: false}}),
    message(62, 'model.tool.result.persisted', {logical_exchange_id: 'exchange-a', tool_round: 0, tool_call_id: 'call-a', reused: false}),
    message(63, 'model.started', {logical_exchange_id: 'exchange-a', tool_round: 1}),
    message(64, 'tool.error', {tool_call_id: 'call-b', error: 'failed'}),
    message(65, 'model.started', {logical_exchange_id: 'exchange-b', tool_round: 0}),
    message(66, 'tool.completed', {tool_call_id: 'call-c', receipt: {call_id: 'call-c', isError: false}}),
  ];
  rows.push({...rows[1], id: 'duplicate-seq'});
  rows.push(message(67, 'tool.completed', {tool_call_id: 'call-c', receipt: {call_id: 'call-c', isError: false}}));
  const other = message(61, 'tool.error', {tool_call_id: 'call-a', error: 'other run failed'});
  other.event.run_id = 'other-run';
  other.id = 'scope:other-run:61';
  rows.push(other);
  const compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 4);
  assert.equal(summaries(compact).every(item => /本次调用了1个工具/.test(messageText(item))), true);
});

test('同名同参的不同 tool_call_id 保持两次调用，网络未发送保持待执行', () => {
  const intent = {tool_name: 'code.read', arguments: {path: 'same-file.ts'}};
  const rows = [
    message(70, 'model.started', {logical_exchange_id: 'exchange-parallel', tool_round: 1}),
    message(71, 'tool.started', {tool_call_id: 'parallel-1', intent}),
    message(72, 'tool.started', {tool_call_id: 'parallel-2', intent}),
    message(73, 'tool.completed', {tool_call_id: 'parallel-1', receipt: {call_id: 'parallel-1', isError: false}}),
    message(74, 'model.error.persisted', {category: 'network', details: {status: 'WAITING_NETWORK', request_status: 'not_sent', tool_call_id: 'parallel-2', message: '未发送，等待网络恢复'}}),
  ];
  const compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 2, '成功数': 1, '失败数': 0, '未知数': 0, '进行中': 1});
  assert.match(render(rows, true), /not_sent/);
});

test('真实写工具 ledger started 不重复计数，receipt.call_id 明确关联操作而不猜同名同参身份', () => {
  const intent = {tool_name: 'apply_patch', arguments: {path: 'server/index.mjs', patch_ref: 'same-patch'}};
  const rows = [
    message(90, 'model.started', {logical_exchange_id: 'exchange-write', tool_round: 1}),
    message(91, 'tool.requested', {tool_call_id: 'write-call-1', intent}),
    message(92, 'tool.started', {operation_id: 'write-operation-1', intent}),
  ];
  let compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 1, '成功数': 0, '失败数': 0, '进行中': 1});
  rows.push(message(93, 'tool.completed', {operation_id: 'write-operation-1', receipt: {call_id: 'write-call-1', name: 'apply_patch', isError: false}}));
  compact = selectUiMessages(rows, false);
  assertCounts(summaries(compact)[0], {'本次调用了': 1, '成功数': 1, '失败数': 0, '进行中': 0});

  const parallel = [
    message(94, 'model.started', {logical_exchange_id: 'exchange-write-parallel', tool_round: 1}),
    message(95, 'tool.requested', {tool_call_id: 'write-call-2', intent}),
    message(96, 'tool.requested', {tool_call_id: 'write-call-3', intent}),
    message(97, 'tool.started', {operation_id: 'write-operation-2', intent}),
    message(98, 'tool.completed', {operation_id: 'write-operation-2', receipt: {call_id: 'write-call-3', name: 'apply_patch', isError: true, error: {message: 'second operation refused'}}}),
  ];
  compact = selectUiMessages(parallel, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 2, '成功数': 0, '失败数': 1, '进行中': 1});
  parallel.push(message(99, 'tool.started', {operation_id: 'write-operation-3', intent}));
  parallel.push(message(100, 'tool.completed', {operation_id: 'write-operation-3', receipt: {call_id: 'write-call-2', name: 'apply_patch', isError: false}}));
  compact = selectUiMessages(parallel, false);
  assertCounts(summaries(compact)[0], {'本次调用了': 2, '成功数': 1, '失败数': 1, '进行中': 0});
});

test('人工核对需明确执行结果，completed 包含 nested isError 仍计失败，缺结果仍未知', () => {
  const rows = [
    message(110, 'model.started', {logical_exchange_id: 'exchange-reconcile-real', tool_round: 1}),
    message(111, 'tool.requested', {tool_call_id: 'reconcile-failed', intent: {tool_name: 'apply_patch'}}),
    message(112, 'tool.unknown', {tool_call_id: 'reconcile-failed', operation_id: 'reconcile-op-1', reason: 'no acknowledgement'}),
    message(113, 'tool.reconciled', {operation_id: 'reconcile-op-1', reviewer: 'user', receipt: {outcome: 'completed', result: {call_id: 'reconcile-failed', name: 'apply_patch', isError: true, error: {message: 'operation executed but failed'}}}}),
    message(114, 'tool.requested', {tool_call_id: 'reconcile-still-unknown', intent: {tool_name: 'apply_patch'}}),
    message(115, 'tool.unknown', {tool_call_id: 'reconcile-still-unknown', operation_id: 'reconcile-op-2', reason: 'no acknowledgement'}),
    message(116, 'tool.reconciled', {operation_id: 'reconcile-op-2', reviewer: 'user', receipt: {comment: 'awaiting evidence'}}),
  ];
  const compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 2, '成功数': 0, '失败数': 1, '未知数': 1, '进行中': 0});
  assert.match(render(rows, true), /operation executed but failed/);
  assert.match(render(rows, true), /awaiting evidence/);
});

test('model.error 的 tool_call_id 与 operation_id 联合别名只关联一个 UNKNOWN 调用，核对后转成功', () => {
  const rows = [
    message(120, 'model.started', {logical_exchange_id: 'exchange-alias', tool_round: 1}),
    message(121, 'tool.requested', {tool_call_id: 'alias-call', intent: {tool_name: 'apply_patch'}}),
    message(122, 'tool.started', {operation_id: 'alias-operation', intent: {tool_name: 'apply_patch'}}),
    message(123, 'tool.unknown', {operation_id: 'alias-operation', reason: 'no acknowledgement'}),
    message(124, 'model.error.persisted', {details: {status: 'UNKNOWN_OPERATION', tool_call_id: 'alias-call', operation_id: 'alias-operation'}}),
  ];
  let compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 1, '成功数': 0, '失败数': 0, '未知数': 1});
  rows.push(message(125, 'tool.reconciled', {operation_id: 'alias-operation', reviewer: 'user', receipt: {outcome: 'completed', result: {call_id: 'alias-call', name: 'apply_patch', isError: false}}}));
  compact = selectUiMessages(rows, false);
  assert.equal(summaries(compact).length, 1);
  assertCounts(summaries(compact)[0], {'本次调用了': 1, '成功数': 1, '失败数': 0, '未知数': 0});
});

test('同 exchange/round 的 attempt 重试与迟到或 reused result 不重复计调用或移走已完成批次', () => {
  const rows = [
    message(130, 'model.started', {logical_exchange_id: 'exchange-retry', tool_round: 1, attempt: 1}),
    message(131, 'tool.started', {tool_call_id: 'retry-call', intent: {tool_name: 'code.read'}}),
    message(132, 'tool.completed', {tool_call_id: 'retry-call', receipt: {call_id: 'retry-call', isError: false}}),
    message(133, 'model.started', {logical_exchange_id: 'exchange-retry', tool_round: 1, attempt: 2}),
    message(134, 'model.request.persisted', {logical_exchange_id: 'exchange-retry', tool_round: 1, attempt: 2}),
    message(135, 'model.started', {logical_exchange_id: 'exchange-retry', tool_round: 2, attempt: 1}),
    message(136, 'tool.error', {tool_call_id: 'next-call', error: 'new round failed'}),
    message(137, 'model.tool.result.persisted', {logical_exchange_id: 'exchange-retry', tool_round: 1, tool_call_id: 'retry-call', reused: false, result_ref: 'late-result'}),
    message(138, 'model.tool.result.persisted', {logical_exchange_id: 'exchange-retry', tool_round: 2, tool_call_id: 'retry-call', reused: true, result_ref: 'reused-result'}),
  ];
  const compact = selectUiMessages(rows, false);
  const batches = summaries(compact);
  assert.equal(batches.length, 2);
  const first = batches.find(item => item.id.includes('round:1'));
  const second = batches.find(item => item.id.includes('round:2'));
  assert.ok(first);
  assert.ok(second);
  assertCounts(first, {'本次调用了': 1, '成功数': 1, '失败数': 0});
  assertCounts(second, {'本次调用了': 1, '成功数': 0, '失败数': 1});
  assert.match(render(rows, true), /"attempt": 2/);
  assert.match(render(rows, true), /late-result/);
  assert.match(render(rows, true), /reused-result/);
});

test('重要错误、审批、门禁和终态默认保留，工具参数和完整 hash 在详情恢复', () => {
  const rows = [
    message(80, 'model.started', {logical_exchange_id: 'exchange-final', tool_round: 1}),
    message(81, 'tool.completed', {tool_call_id: 'call-1', receipt: {call_id: 'call-1', isError: false, staged_ref: 'staged-patch', before_hash: '0123456789abcdef0123456789abcdef'}}),
    message(82, 'approval.requested', {approval_ref: 'approval-final', status: 'WAITING_APPROVAL'}),
    message(83, 'model.error.persisted', {logical_exchange_id: 'exchange-final', error_ref: 'model-error-ref', category: 'WAITING_NETWORK', details: {message: 'network temporarily unavailable'}}),
    message(84, 'run.error', {error: 'fixture visible error', error_ref: 'run-error-ref'}),
    message(85, 'gate.decided', {passed: true, evidence_ref: 'gate-evidence'}),
    message(86, 'run.finished', {status: 'COMPLETED', outcome: 'FIX_VERIFIED', report_ref: 'final-report'}),
  ];
  const output = render(rows);
  for (const reference of ['approval-final', 'gate-evidence', 'final-report']) assert.ok(output.includes(reference), reference);
  assert.match(output, /fixture visible error/);
  assert.match(output, /修复已验证/);
  const details = render(rows, true);
  assert.match(details, /model-error-ref/);
  assert.match(details, /staged-patch/);
  assert.match(details, /0123456789abcdef0123456789abcdef/);
});
