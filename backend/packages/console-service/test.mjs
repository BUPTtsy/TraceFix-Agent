import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {test} from 'node:test';
import {DatabaseSync} from 'node:sqlite';
import {createConsoleService} from './dist/dispatch.mjs';

function fixture(context) {
  const root = fs.mkdtempSync(path.join(process.cwd(), '.tmp-console-test-'));
  const profiles = path.join(root, 'profiles');
  fs.mkdirSync(profiles);
  fs.mkdirSync(path.join(root, 'target', '.git'), {recursive: true});
  const projectsPath = path.join(profiles, 'projects.yaml');
  fs.writeFileSync(projectsPath, JSON.stringify({projects: [{id: 'alpha', repo_id: 'alpha', root: '../target'}]}));
  fs.writeFileSync(path.join(profiles, 'alpha.yaml'), JSON.stringify({project: 'alpha', source_commit: 'HEAD',
    commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', 'fixture.js']]))}));
  const databasePath = path.join(root, 'console.sqlite3');
  const dispatch = createConsoleService({projectsPath, dataRoot: root, databasePath});
  context.after(() => {dispatch.close(); fs.rmSync(root, {recursive: true, force: true});});
  return {root, profiles, projectsPath, databasePath, dispatch};
}

test('停止请求按进程控制标识幂等更新并支持失败恢复', context => {
  const {dispatch} = fixture(context);
  const run = dispatch('run.create', {projectId: 'alpha', goal: '停止测试', mode: 'test'});
  dispatch('run.update', {id: run.id, changes: {processControlId: 'control_test'}});
  assert.throws(() => dispatch('run.stop.request', {id: run.id, processControlId: 'old_control'}), error => error.status === 409);
  const stopping = dispatch('run.stop.request', {id: run.id, processControlId: 'control_test'});
  assert.equal(stopping.status, 'stopping');
  assert.equal(dispatch('run.stop.request', {id: run.id, processControlId: 'control_test'}).stopRequestedAt, stopping.stopRequestedAt);
  const restored = dispatch('run.stop.failed', {id: run.id, processControlId: 'control_test', stopRequestedAt: stopping.stopRequestedAt, error: '权限不足'});
  assert.equal(restored.status, 'running');
  assert.equal(restored.stopRequestedAt, null);
  assert.equal(restored.stopError, '权限不足');
  dispatch('run.update', {id: run.id, changes: {status: 'completed', outcome: 'NO_BUG_FOUND'}});
  assert.equal(dispatch('run.stop.request', {id: run.id, processControlId: 'control_test'}).status, 'completed');
});

test('无 taskId 的 SDK 记录（CLI 发起）不触发权威刷新，运行列表与详情照常返回', context => {
  const {dispatch} = fixture(context);
  const run = dispatch('run.create', {projectId: 'alpha', goal: 'CLI 发起的 SDK 修复', mode: 'test'});
  dispatch('run.update', {id: run.id, changes: {coding_backend: 'claude_sdk', control_status: 'COMPLETED', status: 'completed', outcome: 'FIX_VERIFIED'}});
  const listed = dispatch('runs', {projectId: 'alpha'});
  assert.equal(listed.length, 1);
  assert.equal(listed[0].coding_backend, 'claude_sdk');
  assert.equal(listed[0].control_status, 'COMPLETED');
  assert.equal(dispatch('run', {id: run.id}).coding_backend, 'claude_sdk');
  assert.throws(() => dispatch('run.harness.stop', {id: run.id}), error => error.code === 'HARNESS_TASK_REQUIRED');
});

test('待审批 Run 的控制投影：can_approve 仅待审批、不可 stop，刷新不改回 completed 并同步权威审批', context => {
  const {root, projectsPath, databasePath, dispatch: base} = fixture(context);
  let authorityApproval = null;
  const dispatch = createConsoleService({projectsPath, dataRoot: root, databasePath, harnessControl: () => ({
    coding_backend: 'claude_sdk', control_status: 'COMPLETED', can_resume: false, can_stop: true, resume_blockers: [],
    harness_job_id: 'job', harness_session_id: 'session', approval: authorityApproval})});
  const run = base('run.create', {projectId: 'alpha', goal: '待审批展示', mode: 'test'});
  const approval = {id: 'a1', status: 'pending', job_id: 'job', publish_branch: false};
  base('run.update', {id: run.id, changes: {taskId: 'task', coding_backend: 'claude_sdk', control_status: 'COMPLETED', can_stop: true,
    status: 'waiting_approval', pid: null, approval, publication: null, delivery_diff: 'diff text'}});
  const view = dispatch('runs', {projectId: 'alpha'})[0];
  assert.equal(view.status, 'waiting_approval');
  assert.equal(view.can_approve, true);
  assert.equal(view.can_stop, false);
  assert.deepEqual(view.approval, approval);
  assert.equal(view.delivery_diff, 'diff text');
  assert.equal(dispatch('run.stop.request', {id: run.id, processControlId: 'x'}).status, 'waiting_approval');
  authorityApproval = {id: 'a1', status: 'approved', decision: 'approve', decided_by: 'cli', decided_at: '2026-10-24T00:00:00Z', diff_ref: null};
  const synced = dispatch('run', {id: run.id});
  assert.equal(synced.status, 'waiting_approval');
  assert.equal(synced.approval.status, 'approved');
  assert.equal(synced.approval.job_id, 'job');
  assert.equal(synced.can_approve, false);
  dispatch.close();
});

test('新建与更新规则区分冲突、拒绝 Python 无法读取的配置', context => {
  const {dispatch, databasePath, projectsPath, root} = fixture(context);
  const fields = {id: 'new_rule', name: '新规则', detection: {type: 'guided', guided: {prompt: '检查空状态'}}};
  const created = dispatch('rule.create', fields);
  assert.throws(() => dispatch('rule.create', fields), error => error.status === 409);
  assert.throws(() => dispatch('rule.update', {...fields, id: 'missing'}), error => error.status === 404);
  for (const detection of [{type: 'guided', guided: {}}, {type: 'static', static: []},
    {type: 'guided', guided: '不是对象'}, {type: 'guided', guided: {prompt: '正常'}, oracle: []}]) {
    assert.throws(() => dispatch('rule.create', {...fields, id: undefined, detection}), error => error.status === 400);
  }
  assert.throws(() => dispatch('rule.create', {...fields, id: undefined, fix_guidance: 'https://user:password@example.com'}), error => error.status === 400);
  const changed = dispatch('rule.update', {...created, expectedVersion: created.version, fix_guidance: '完整要求'.repeat(220)});
  const preview = dispatch('rule.preview', {id: changed.id, maxTokens: 128});
  assert.ok(preview.over_budget);
  assert.equal(preview.items[0].fix_guidance, changed.fix_guidance);
  const builtin = dispatch('rule', {id: 'builtin_empty_state'});
  dispatch('rule.status', {id: builtin.id, status: 'disabled', expectedVersion: builtin.version});
  const reopened = createConsoleService({projectsPath, dataRoot: root, databasePath});
  try {assert.equal(reopened('rule', {id: builtin.id}).status, 'disabled');}
  finally {reopened.close();}
});

test('标准 YAML URL 列表与项目就绪状态保持 Python 契约', context => {
  const {dispatch, profiles} = fixture(context);
  const profile = path.join(profiles, 'alpha.yaml');
  fs.writeFileSync(profile, 'project: alpha\nsource_commit: HEAD\nallowed_origins:\n- http://app:3000\ncommands:\n' +
    ['start', 'reset', 'static', 'unit', 'build'].map(name => `  ${name}: ["node", "fixture.js"]\n`).join(''));
  assert.equal(dispatch('projects')[0].ready, true);
  fs.writeFileSync(profile, JSON.stringify({project: 'alpha', source_commit: 'HEAD', commands: {start: ['node', 'fixture.js']}}));
  const project = dispatch('projects')[0];
  assert.equal(project.ready, false);
  assert.match(project.issue, /命令/);
  assert.throws(() => dispatch('run.create', {projectId: 'alpha', goal: '检查', mode: 'test'}));
});

test('派生 Run 校验快照、项目与追加规则，并保持 Python 共享数据一致', context => {
  const {dispatch, databasePath} = fixture(context);
  const parent = dispatch('run.create', {projectId: 'alpha', goal: '父任务', mode: 'test'});
  dispatch('run.update', {id: parent.id, changes: {agentRunId: 'run_parent', status: 'completed'}});
  assert.throws(() => dispatch('run.create', {projectId: 'alpha', goal: '子任务', mode: 'test', parentRunId: parent.id}), error => error.status === 409);
  const python = process.env.TRACEFIX_PYTHON || (process.platform === 'win32' ? path.resolve('../../../.venv/Scripts/python.exe') : path.resolve('../../../.venv/bin/python'));
  if (!fs.existsSync(python)) return context.skip('需要本地 Python 引擎环境验证共享数据库');
  const script = `import json,sys
from tracefix.rules import RuleLibrary, RuleResolver, render_rule_context
from tracefix.rules.builtins import builtin_rules
library=RuleLibrary(sys.argv[1])
snapshot,_=RuleResolver(library).resolve_snapshot(run_id='run_parent', project_id='alpha', phase='*')
library.save_snapshot(snapshot)
print(json.dumps({'refs':[ref.model_dump() for ref in snapshot.refs],
 'builtins':[rule.model_dump(exclude={'created_at','updated_at'}) for rule in builtin_rules()],
 'preview':render_rule_context([library.rule('builtin_empty_state')], max_tokens=128)}, ensure_ascii=False))`;
  const result = spawnSync(python, ['-c', script, databasePath], {cwd: path.resolve('../../..'), encoding: 'utf8',
    env: {...process.env, PYTHONPATH: path.resolve('../agent/src'), PYTHONIOENCODING: 'utf-8'}});
  assert.equal(result.status, 0, result.stderr);
  const data = JSON.parse(result.stdout);
  for (const rule of data.builtins) {
    const {created_at, updated_at, ...actual} = dispatch('rule', {id: rule.id});
    assert.deepEqual(actual, rule);
  }
  const {rule, ...preview} = dispatch('rule.preview', {id: 'builtin_empty_state', maxTokens: 128});
  assert.deepEqual(preview, data.preview);
  const extra = dispatch('rule.create', {name: '子任务追加', scope: {level: 'run', project_ids: ['alpha']},
    detection: {type: 'guided', guided: {prompt: '检查追加要求'}}});
  const child = dispatch('run.create', {projectId: 'alpha', goal: '子任务', mode: 'test', parentRunId: parent.id, additionalRuleIds: [extra.id]});
  assert.equal(child.parentRunId, 'run_parent');
  assert.deepEqual(child.additionalRuleIds, [extra.id]);
  assert.deepEqual(dispatch('run', {id: parent.id}).ruleSnapshot.refs, data.refs);
  assert.throws(() => dispatch('rule.delete', {id: 'builtin_empty_state'}), error => error.status === 409);
  dispatch('run.ended', {id: child.id, error: '串行测试结束后验证无效追加规则'});
  assert.throws(() => dispatch('run.create', {projectId: 'alpha', goal: '子任务', mode: 'test', parentRunId: parent.id,
    additionalRuleIds: 'invalid'}), error => error.status === 400);
});

test('TypeScript 控制台共享文档、运行和规则数据', () => {
  const root = fs.mkdtempSync(path.join(process.cwd(), '.tmp-console-test-'));
  let dispatch;
  try {
    const profiles = path.join(root, 'profiles');
    fs.mkdirSync(profiles);
    fs.mkdirSync(path.join(root, 'target', '.git'), {recursive: true});
    fs.writeFileSync(path.join(profiles, 'projects.yaml'), 'projects:\n  - id: alpha\n    repo_id: alpha\n    root: ../target\n    allowed_files: ["src/**"]\n');
    fs.writeFileSync(path.join(profiles, 'alpha.yaml'), JSON.stringify({project: 'alpha', source_commit: 'HEAD',
      url: 'http://app:3000', commands: Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name => [name, ['node', 'fixture.js']]))}));
    dispatch = createConsoleService({projectsPath: path.join(profiles, 'projects.yaml'),
      dataRoot: path.join(root, 'data'), databasePath: path.join(root, 'data', 'console.sqlite3')});
    assert.equal(dispatch('projects')[0].ready, true);
    assert.deepEqual(dispatch('rules').map(rule => rule.id).sort(), [
      'builtin_api_no_5xx', 'builtin_console_no_error', 'builtin_empty_state',
      'builtin_input_label', 'builtin_submit_feedback',
    ]);

    const document = dispatch('document.save', {title: '保存状态', content: '刷新后完成状态丢失', tags: ['React'],
      kind: 'repair', enabled: true, projectId: 'alpha'});
    assert.equal(dispatch('search', {projectId: 'alpha', query: '刷新状态'})[0].id, document.id);
    assert.equal(dispatch('document.save', {...document, content: '修复后的内容', id: document.id}).version, 2);
    assert.throws(() => dispatch('document.save', {...document, id: document.id}), error => error.status === 409);

    const run = dispatch('run.create', {projectId: 'alpha', goal: '检查状态', mode: 'test'});
    dispatch('run.update', {id: run.id, changes: {status: 'failed', outcome: 'INFRA_FAILURE'}});
    const continued = dispatch('run.continue', {id: run.id, instruction: '重新检查'});
    assert.equal(continued.continuationCount, 1);
    assert.equal(dispatch('run.trace', {id: run.id})[0].type, 'run.continuation_requested');
    assert.equal(dispatch('runs', {projectId: 'alpha'})[0].canContinue, false);

    const rule = dispatch('rule.save', {id: 'state_persistence', name: '状态须持久化',
      scope: {project_ids: ['alpha']}, detection: {type: 'oracle', oracle: {kind: 'console_no_error'}}});
    assert.equal(dispatch('rules', {projectId: 'alpha'})[0].id, rule.id);
    const enabled = dispatch('rule.status', {id: rule.id, status: 'enabled', expectedVersion: 1});
    assert.equal(enabled.version, 2);
    assert.equal(dispatch('rule.versions', {id: rule.id}).length, 2);
    assert.throws(() => dispatch('rule.status', {id: rule.id, status: 'disabled', expectedVersion: 1}),
      error => error.status === 409);

    const python = process.platform === 'win32' ? path.resolve('../../../.venv/Scripts/python.exe') : path.resolve('../../../.venv/bin/python');
    if (fs.existsSync(python)) {
      const hasPythonRules = fs.existsSync(path.resolve('../agent/src/tracefix/rules/store.py'));
      const script = hasPythonRules ?
        'import json,sys; from tracefix.knowledge.documents import DocumentLibrary; from tracefix.rules.store import RuleLibrary; p=sys.argv[1]; print(json.dumps({"document":DocumentLibrary(p).document(sys.argv[2])["version"],"rule_hash":RuleLibrary(p).rule(sys.argv[3]).body_hash}))' :
        'import json,sys; from tracefix.knowledge.documents import DocumentLibrary; p=sys.argv[1]; print(json.dumps({"document":DocumentLibrary(p).document(sys.argv[2])["version"]}))';
      const result = spawnSync(python, ['-c', script, path.join(root, 'data', 'console.sqlite3'), document.id, rule.id],
        {cwd: path.resolve('../../..'), env: {...process.env, PYTHONPATH: path.resolve('../agent/src')}, encoding: 'utf8'});
      assert.equal(result.status, 0, result.stderr);
      const data = JSON.parse(result.stdout);
      assert.equal(data.document, 2);
      const db = new DatabaseSync(path.join(root, 'data', 'console.sqlite3'));
      try {
        const row = db.prepare('SELECT body_hash FROM rule_versions WHERE rule_id=? AND version=?').get(rule.id, 2);
      if (hasPythonRules) assert.equal(data.rule_hash, row.body_hash);
      } finally { db.close(); }
    }
  } finally { dispatch?.close(); fs.rmSync(root, {recursive: true, force: true}); }
});
