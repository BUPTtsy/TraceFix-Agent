import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import {test} from 'node:test';
import {DatabaseSync} from 'node:sqlite';
import {createConsoleService} from './dist/dispatch.mjs';

test('TypeScript 控制台共享文档、运行和规则数据', () => {
  const root = fs.mkdtempSync(path.join(process.cwd(), '.tmp-console-test-'));
  let dispatch;
  try {
    const profiles = path.join(root, 'profiles');
    fs.mkdirSync(profiles);
    fs.mkdirSync(path.join(root, 'target', '.git'), {recursive: true});
    fs.writeFileSync(path.join(profiles, 'projects.yaml'), 'projects:\n  - id: alpha\n    repo_id: alpha\n    root: ../target\n    allowed_files: ["src/**"]\n');
    fs.writeFileSync(path.join(profiles, 'alpha.yaml'), 'project: alpha\nurl: http://app:3000\ncommands:\n  start: ["node", "start.js"]\n');
    dispatch = createConsoleService({projectsPath: path.join(profiles, 'projects.yaml'),
      dataRoot: path.join(root, 'data'), databasePath: path.join(root, 'data', 'console.sqlite3')});
    assert.equal(dispatch('projects')[0].ready, true);

    const document = dispatch('document.save', {title: '保存状态', content: '刷新后完成状态丢失', tags: ['React'],
      kind: 'repair', enabled: true, projectId: 'alpha'});
    assert.equal(dispatch('search', {projectId: 'alpha', query: '刷新状态'})[0].id, document.id);
    assert.equal(dispatch('document.save', {...document, content: '修复后的内容', id: document.id}).version, 2);
    assert.throws(() => dispatch('document.save', {...document, id: document.id}), error => error.status === 409);
    const cli = spawnSync(process.execPath, [path.resolve('dist/cli.mjs'), '--project', 'alpha',
      '--projects', path.join(profiles, 'projects.yaml'), '--data', path.join(root, 'data'),
      '--console-db', path.join(root, 'data', 'console.sqlite3'), '--command', `/knowledge show ${document.id}`],
      {cwd: path.resolve('..'), encoding: 'utf8'});
    assert.equal(cli.status, 0, cli.stderr);
    assert.match(cli.stdout, /修复后的内容/);
    const sessionCli = spawnSync(process.execPath, [path.resolve('dist/cli.mjs'), '--project', 'alpha',
      '--projects', path.join(profiles, 'projects.yaml'), '--data', path.join(root, 'data'),
      '--console-db', path.join(root, 'data', 'console.sqlite3'),
      '--command', '/remote set --repository team/repo --scope session', '--command', '/remote show'],
      {cwd: path.resolve('..'), encoding: 'utf8'});
    assert.equal(sessionCli.status, 0, sessionCli.stderr);
    assert.match(sessionCli.stdout, /team\/repo/);
    assert.equal(fs.existsSync(path.join(root, 'data', 'remote-config.json')), false);

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

    const python = process.platform === 'win32' ? path.resolve('../.venv/Scripts/python.exe') : path.resolve('../.venv/bin/python');
    if (fs.existsSync(python)) {
      const hasPythonRules = fs.existsSync(path.resolve('../src/tracefix/rules/store.py'));
      const script = hasPythonRules ?
        'import json,sys; from tracefix.knowledge.documents import DocumentLibrary; from tracefix.rules.store import RuleLibrary; p=sys.argv[1]; print(json.dumps({"document":DocumentLibrary(p).document(sys.argv[2])["version"],"rule_hash":RuleLibrary(p).rule(sys.argv[3]).body_hash}))' :
        'import json,sys; from tracefix.knowledge.documents import DocumentLibrary; p=sys.argv[1]; print(json.dumps({"document":DocumentLibrary(p).document(sys.argv[2])["version"]}))';
      const result = spawnSync(python, ['-c', script, path.join(root, 'data', 'console.sqlite3'), document.id, rule.id],
        {cwd: path.resolve('..'), env: {...process.env, PYTHONPATH: path.resolve('../src')}, encoding: 'utf8'});
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
