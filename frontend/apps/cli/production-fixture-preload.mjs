import {EventEmitter} from 'node:events';
import {PassThrough, Writable} from 'node:stream';
import {DatabaseSync} from 'node:sqlite';
import childProcess from 'node:child_process';
import {syncBuiltinESMExports} from 'node:module';
import fs from 'node:fs';

let fixtureRunCount = 0;

class FixtureChild extends EventEmitter {
  constructor(args) {
    super();
    this.args = args;
    this.closed = false;
    this.projectId = String(args[args.indexOf('--project') + 1] || 'bugboard');
    this.databasePath = String(args[args.indexOf('--console-db') + 1]);
    this.goal = args.includes('--goal') ? String(args[args.indexOf('--goal') + 1]) : '';
    this.signalFile = process.env.TRACEFIX_CLI_FIXTURE_SIGNAL || '';
    this.selectionDelivered = false;
    fixtureRunCount += 1;
    this.agentRunId = `fixture-agent-run-${fixtureRunCount}`;
    this.signalTimer = setInterval(() => {
      if (!this.started || this.closed || this.selectionDelivered || !this.signalFile || !fs.existsSync(this.signalFile)) return;
      if (fs.readFileSync(this.signalFile, 'utf8') !== 'selection') return;
      this.selectionDelivered = true;
      this.event('observation', {message: 'fixture event received while selecting', observation_ref: 'fixture-selection-observation'});
    }, 40);
    this.stdout = new PassThrough();
    this.stderr = new PassThrough();
    this.stdin = new Writable({
      write: (chunk, _encoding, callback) => {
        try { this.receive(String(chunk)); callback(); }
        catch (error) { callback(error); }
      },
    });
  }

  record() {
    const database = new DatabaseSync(this.databasePath);
    try {
      const row = database.prepare('SELECT id,data FROM console_runs ORDER BY rowid DESC LIMIT 1').get();
      if (!row) throw new Error('fixture run record was not created');
      return {database, id: row.id, data: JSON.parse(row.data)};
    } catch (error) {
      database.close();
      throw error;
    }
  }

  updateRecord(status, extra = {}) {
    const state = this.record();
    try {
      const data = {
        ...state.data,
        ...extra,
        status,
        phase: status === 'cancelled' ? 'FINALIZE' : 'VERIFY',
        updatedAt: new Date().toISOString(),
      };
      state.database.prepare('UPDATE console_runs SET data=? WHERE id=?').run(JSON.stringify(data), state.id);
      return state.id;
    } finally {
      state.database.close();
    }
  }

  event(type, payload, status = null) {
    const state = this.record();
    try {
      const row = state.database.prepare(
        'SELECT COALESCE(MAX(seq),0)+1 AS seq FROM console_events WHERE run_id=?',
      ).get(state.id);
      const seq = Number(row.seq);
      const event = {
        run_id: this.agentRunId,
        scope_id: this.projectId,
        seq,
        agentSeq: seq,
        phase: status === 'cancelled' ? 'FINALIZE' : 'VERIFY',
        type,
        revision: seq,
        at: Date.now() / 1000,
        payload,
      };
      state.database.prepare('INSERT INTO console_events VALUES (?,?,?,?)')
        .run(state.id, seq, `fixture:${seq}`, JSON.stringify(event));
    } finally {
      state.database.close();
    }
    if (status) this.updateRecord(status, {agentRunId: this.agentRunId});
  }

  stdoutText(text) {
    const bytes = Buffer.from(text, 'utf8');
    for (let index = 0; index < bytes.length; index++) this.stdout.write(bytes.subarray(index, index + 1));
  }

  begin() {
    if (this.started || this.closed) return;
    this.started = true;
    this.updateRecord('running', {agentRunId: this.agentRunId, pid: process.pid});
    this.event('run.started', {status: 'RUNNING', message: 'fixture run started'});
    this.stdoutText('fixture stdout：运行开始\n');
    for (let index = 0; index < 36; index++) this.event('observation', {message: `fixture history-${String(index).padStart(2, '0')}`});
    setTimeout(() => {
      if (this.closed) return;
      this.event('state.changed', {status: 'RUNNING', phase: 'RUNNING'});
      this.event('state.changed', {status: 'RUNNING', phase: 'RUNNING'});
      const exchange = {logical_exchange_id: 'fixture-exchange-1', logical_call: 1, tool_round: 0, attempt: 1, model: 'fixture-model'};
      this.event('model.started', exchange);
      this.event('model.request.persisted', {...exchange, request_ref: 'fixture-request-1'});
      this.event('model.reasoning.persisted', {...exchange, reasoning_ref: 'fixture-reasoning-1'});
      this.event('model.response.persisted', {...exchange, response_ref: 'fixture-response-1'});
      this.event('model.usage', {usage: {total_tokens: 22}});
      this.event('tool.started', {tool_call_id: 'fixture-call', intent: {tool_name: 'fixture-event-only-tool', arguments: {path: 'fixture-file.ts'}}});
      this.event('tool.completed', {tool_call_id: 'fixture-call', receipt: {call_id: 'fixture-call', name: 'fixture-event-only-tool', isError: false, observation_ref: 'fixture-event-only-observation'}});
      this.event('tool.started', {tool_call_id: 'fixture-failed-call', intent: {tool_name: 'fixture-failing-tool'}});
      this.event('tool.error', {tool_call_id: 'fixture-failed-call', receipt: {call_id: 'fixture-failed-call', name: 'fixture-failing-tool', isError: true, error: {message: 'fixture execution failed', executed: false}}});
      this.event('tool.requested', {tool_call_id: 'fixture-unknown-call', intent: {tool_name: 'fixture-external-tool'}});
      this.event('tool.started', {operation_id: 'fixture-operation', intent: {tool_name: 'fixture-external-tool'}});
      this.event('tool.unknown', {operation_id: 'fixture-operation', reason: 'fixture acknowledgement unknown', resources: [{path: 'fixture-file.ts'}]});
      this.event('model.error.persisted', {error_ref: 'fixture-unknown-error', category: 'tool', details: {status: 'UNKNOWN_OPERATION', tool_call_id: 'fixture-unknown-call', operation_id: 'fixture-operation', message: 'fixture acknowledgement unknown'}});
      for (const tool_call_id of ['fixture-call', 'fixture-failed-call', 'fixture-unknown-call']) {
        this.event('model.tool.result.persisted', {logical_exchange_id: 'fixture-exchange-1', tool_round: 1, tool_call_id, result_ref: `fixture-result-${tool_call_id}`, reused: false});
      }
    }, 120);
    setTimeout(() => {
      if (this.closed) return;
      this.event('approval.requested', {approval_ref: 'fixture-approval', patch_hash: 'fixture-patch', status: 'WAITING_APPROVAL'});
      this.updateRecord('waiting_approval', {agentRunId: this.agentRunId});
    }, 260);
    setTimeout(() => {
      if (this.closed) return;
      this.event('run.continued', {continuation_ref: 'fixture-continuation', status: 'RUNNING'});
      this.updateRecord('running', {agentRunId: this.agentRunId});
    }, 420);
    if (this.goal === 'fixture finish goal') setTimeout(() => {
      if (this.closed) return;
      this.event('gate.decided', {passed: true, evidence_ref: 'fixture-event-only-gate'});
      this.event('run.finished', {status: 'COMPLETED', outcome: 'FIX_VERIFIED', error: null, error_details: null, cancelled: false, report_ref: 'fixture-event-only-report'}, 'completed');
      setTimeout(() => this.close(0), 80);
    }, 1500);
  }

  receive(text) {
    for (const line of text.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
      if (!line.startsWith('/')) {
        this.goal = line;
        this.begin();
      }
      if (line === '/run') this.begin();
      if (line === '/interrupt' || line === '/cancel') {
        if (this.closed) continue;
        this.event('run.cancelled', {status: 'CANCELLED', message: 'fixture cancel acknowledged'}, 'cancelled');
        this.stdoutText('fixture cancel acknowledged\n');
        this.started = false;
        this.emit('close', 0);
      }
      if (line === '/quit') this.close(0);
    }
  }

  close(code = 0) {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.signalTimer);
    this.stdin.destroy();
    this.stdout.end();
    this.stderr.end();
    this.emit('close', code);
  }

  kill() { this.close(0); }
}

const originalSpawn = childProcess.spawn;
childProcess.spawn = (command, args = [], options) => {
  if (process.env.TRACEFIX_CLI_FIXTURE === '1' && args.includes('tools/bootstrap/launch.py')) {
    return new FixtureChild(args);
  }
  return originalSpawn(command, args, options);
};
syncBuiltinESMExports();
