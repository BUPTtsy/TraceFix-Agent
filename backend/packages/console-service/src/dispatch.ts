import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {artifactIndex, evidenceClosure, readArtifact, readArtifactBytes} from './artifacts.js';
import {loadConfig, projectCatalog, repositoryControl, type RepositoryControlOptions} from './config.js';
import {canContinue, codingBackend, controlProjection, ConsoleDatabase, DataError, timestamp} from './database.js';
import {RuleDatabase} from './rules.js';
import {classifyGuidance, guidanceUnsafe} from './guidance.js';
import {TaskLifecycle, approvalInput, executionDefinition, normalizeTask, taskError} from './task-lifecycle.js';
import {resultView, executorResult} from './results.js';
import {harnessControl, type HarnessControlOptions, type HarnessControlOperation} from './harness-control.js';

type Data = Record<string, any>;

export interface ConsoleOptions extends RepositoryControlOptions, HarnessControlOptions {projectsPath?: string; databasePath?: string; displayRoot?: string}

export function createConsoleService(options: ConsoleOptions = {}) {
  const projectsPath = path.resolve(options.projectsPath || process.env.TRACEFIX_PROJECTS || 'profiles/projects.yaml');
  const dataRoot = path.resolve(options.dataRoot || process.env.TRACEFIX_DATA || '.tracefix');
  const database = new ConsoleDatabase(path.resolve(options.databasePath || process.env.TRACEFIX_CONSOLE_DB || path.join(dataRoot, 'console.sqlite3')));
  const rules = new RuleDatabase(database);
  const catalog = () => projectCatalog(projectsPath, dataRoot, options.displayRoot);
  for (const project of catalog()) rules.initializeRepositoryRules(project.id);
  const tasks = new TaskLifecycle(database, fields => normalizeTask(fields, catalog()),
    (fields, id) => dispatch('run.prepare', {...fields, _runId: id}));
  const repository = (operation: string, fields: Data = {}) => repositoryControl(projectsPath, operation, fields,
    {...options, engineRoot: options.engineRoot || options.displayRoot, dataRoot});
  const artifactRoot = (run: Data) => path.join(run.dataRoot || dataRoot, 'artifacts');
  const refreshControl = (run: Data, operation: HarnessControlOperation = 'view') => {
    if (codingBackend(run) !== 'claude_sdk') return run;
    // CLI 直接发起的 SDK Run 没有 Task 绑定：只读视图跳过权威刷新原样返回，控制操作仍要求 Task。
    if (!run.taskId) {
      if (operation === 'view') return run;
      throw taskError('HARNESS_TASK_REQUIRED', 'SDK 控制必须绑定 Task', 409);
    }
    let authority;
    try {
      authority = harnessControl(operation, {task_id: run.taskId, scope_id: run.projectId, run_id: run.id,
        ...(run.harness_job_id ? {job_id: run.harness_job_id} : {})}, {...options,
        engineRoot: options.engineRoot || options.displayRoot, dataRoot: run.dataRoot || dataRoot,
        databasePath: database.filename});
    } catch (error) {
      if (operation !== 'view' || (error as {status?: number}).status !== 404) throw error;
      authority = {coding_backend: 'claude_sdk', control_status: 'BLOCKED', can_resume: false,
        can_stop: false, resume_blockers: ['HARNESS_JOB_NOT_BOUND']};
    }
    const {coding_backend, control_status, can_resume, can_stop, resume_blockers,
      harness_job_id, harness_session_id, current_run_id, accepted_commit, quality, owner_epoch, revision, approval} = authority;
    // 权威交付记录只补充审批子集；不改写 Run status（waiting_approval 由执行结果维护，不能被刷新改回 completed）。
    const local = run.approval && typeof run.approval === 'object' ? run.approval : null;
    const merged = approval && typeof approval === 'object' && !Array.isArray(approval) ?
      (!local || local.id === approval.id ? {...(local || {}), ...Object.fromEntries(Object.entries(approval)
        .filter(([, value]) => value !== null && value !== undefined))} : approval) : undefined;
    return database.updateRun(run.id, {coding_backend, control_status, can_resume, can_stop, resume_blockers,
      ...(harness_job_id ? {harness_job_id} : {}), ...(harness_session_id ? {harness_session_id} : {}),
      ...(current_run_id ? {agentRunId: current_run_id, current_run_id} : {}),
      ...(accepted_commit !== undefined ? {accepted_commit} : {}), ...(quality !== undefined ? {quality} : {}),
      ...(owner_epoch !== undefined ? {owner_epoch} : {}), ...(revision !== undefined ? {harness_revision: revision} : {}),
      ...(merged ? {approval: merged} : {})});
  };
  const taskView = (task: Data) => {
    const run = task.currentRunId ? refreshControl(database.run(task.currentRunId)) : null;
    return {...task, ...(run ? controlProjection(run) : {
      coding_backend: task.definition?.mode === 'repair' ? 'claude_sdk' : null, control_status: String(task.status || '').toUpperCase(),
      can_stop: false, can_resume: false, can_approve: false, approval: null, resume_blockers: [],
      harness_job_id: null, harness_session_id: null,
    })};
  };
  // 与 Python 运行时共用 guidance.sqlite3，Web 写入先排队，执行层在安全边界读取并应用。
  const guidanceDb = (run: Data) => {
    const filename = path.join(run.dataRoot || dataRoot, 'guidance.sqlite3');
    fs.mkdirSync(path.dirname(filename), {recursive: true});
    const database = new DatabaseSync(filename, {timeout: 15000});
    database.exec('PRAGMA busy_timeout=15000');
    database.exec('CREATE TABLE IF NOT EXISTS run_guidance (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, scope_id TEXT NOT NULL, data TEXT NOT NULL)');
    return database;
  };
  const guidanceRead = (run: Data) => {
    const sqlite = guidanceDb(run);
    try { return (sqlite.prepare('SELECT data FROM run_guidance WHERE run_id=? AND scope_id=? ORDER BY rowid').all(run.agentRunId, run.projectId) as {data: string}[]).map(row => JSON.parse(row.data)); }
    finally {sqlite.close();}
  };
  const guidanceWrite = (run: Data, fields: Data) => {
    // L1 只补充提示，L2 收紧路径/变更量/动作约束，L3 需要后续确认；这里仅保存账本条目。
    const text = String(fields.text || '').trim();
    if (!text || text.length > 2000) throw new DataError('引导长度必须为 1-2000 字');
    let classified;
    try { classified = classifyGuidance(text, fields.level, fields.constraints); }
    catch (error) { throw new DataError(error instanceof Error ? error.message : '约束无效'); }
    const {level, constraints} = classified;
    const id = 'guidance_' + randomUUID().replaceAll('-', '');
    const entry: Data = {id, run_id: run.agentRunId, scope_id: run.projectId, author: 'web', level, text,
      created_at: Date.now() / 1000, created_phase: run.phase || null, applied_step: null, applied_call: null,
      expires: 'run', status: guidanceUnsafe(text) ? 'rejected' : 'queued', constraints,
      confirmed_at: null, rejection_reason: guidanceUnsafe(text) ? '引导不能扩大权限或跳过验证门禁' : null,
      how_applied: null, child_run_id: null};
    const sqlite = guidanceDb(run);
    try {sqlite.prepare('INSERT INTO run_guidance VALUES (?,?,?,?)').run(entry.id, entry.run_id, entry.scope_id, JSON.stringify(entry));}
    finally {sqlite.close();}
    return entry;
  };
  const guidanceConfirm = (run: Data, id: string) => {
    // 确认请求仅落确认时间；替换父目标并派生子 Run 由执行层在安全边界完成。
    const sqlite = guidanceDb(run);
    try {
      const row = sqlite.prepare('SELECT data FROM run_guidance WHERE id=? AND run_id=? AND scope_id=?').get(id, run.agentRunId, run.projectId) as {data: string} | undefined;
      if (!row) throw new DataError('当前 Run 中没有此引导', 404);
      const entry = JSON.parse(row.data);
      if (entry.level !== 'retarget' || entry.status !== 'queued' || entry.confirmed_at) throw new DataError('只有尚未确认的改目标引导可以确认');
      entry.confirmed_at = Date.now() / 1000;
      sqlite.prepare('UPDATE run_guidance SET data=? WHERE id=?').run(JSON.stringify(entry), id);
      return entry;
    } finally {sqlite.close();}
  };

  function runView(id: string): Data {
    const record = refreshControl(database.run(id));
    const index = record.agentRunId ? artifactIndex(artifactRoot(record), record.projectId, record.agentRunId) : {};
    const allowed = record.agentRunId ? evidenceClosure(artifactRoot(record), record.projectId,
      record.agentRunId, record.reportRef, index) : new Set<string>();
    const artifacts = Object.entries(index).filter(([ref]) => allowed.has(ref))
      .map(([ref, entry]) => ({ref, label: entry['用途'], bytes: entry['字节数']}));
    const issueReport = record.reportRef && allowed.has(record.reportRef) ?
      JSON.parse(readArtifact(artifactRoot(record), record.projectId, record.agentRunId, record.reportRef)) : null;
    const checkReport = Object.fromEntries(['check_plan', 'check_results', 'check_summary', 'overall_status', 'images',
      'initial_check_results', 'initial_check_summary']
      .filter(key => issueReport && key in issueReport).map(key => [key, issueReport[key]]));
    return {...record, ...controlProjection(record), canContinue: canContinue(record.status, record.outcome), artifacts,
      issueReport, ...checkReport,
      ruleSnapshot: record.agentRunId ? rules.snapshot(record.agentRunId) : null};
  }

  function importRuns(): Data {
    const known = new Set(database.runs().map(run => run.agentRunId));
    let imported = 0;
    for (const project of catalog()) {
      const projectRoot = path.join(dataRoot, 'artifacts', project.id);
      if (!fs.existsSync(projectRoot)) continue;
      for (const runName of fs.readdirSync(projectRoot).filter(name => /^run_/.test(name))) {
        const runDirectory = path.join(projectRoot, runName);
        if (!fs.statSync(runDirectory).isDirectory() || fs.lstatSync(runDirectory).isSymbolicLink() || known.has(runName)) continue;
        let index;
        try { index = artifactIndex(path.join(dataRoot, 'artifacts'), project.id, runName); } catch { continue; }
        for (const [ref, entry] of Object.entries(index).reverse()) {
          if (!ref.endsWith('.json') || !entry['用途']?.includes('修复报告数据')) continue;
          let report: Data;
          try { report = JSON.parse(readArtifact(path.join(dataRoot, 'artifacts'), project.id, runName, ref)); } catch { continue; }
          if (report.run_id !== runName || report.scope_id !== project.id || !report.run_status) continue;
          const finished = fs.statSync(path.join(runDirectory, ref)).mtime.toISOString();
          database.updateRun('history_' + runName, {
            agentRunId: runName, projectId: project.id, goal: report.goal || '', mode: report.mode || 'unknown',
            status: String(report.run_status).toLowerCase(), phase: 'FINALIZE', outcome: report.outcome || null, reportRef: ref,
            parentRunId: report.parent_run_id || null, continuationInstruction: report.continuation_instruction || null,
            continuationCount: report.continuation_count || 0, abnormalTermination: report.abnormal_termination || false,
            continuationMarkers: report.continuation_markers || [], branch: report.branch || null, error: report.error || null,
            finishedAt: finished, startedAt: finished, timeSource: 'report_file', origin: 'artifact', dataRoot,
            logs: ['此记录从已有报告导入。历史进程日志和实际启动时间未记录；列表按报告文件时间排序。'],
          }, true);
          imported++; known.add(runName); break;
        }
      }
    }
    return {imported};
  }

  function dispatch(operation: string, fields: Data = {}): any {
    if (operation === 'tasks') return tasks.tasks(fields.projectId).map(taskView);
    if (operation === 'task') return taskView(tasks.task(fields.id));
    if (operation === 'task.create') return tasks.create(fields.body, fields.key);
    if (operation === 'task.create.check') return tasks.checkCreate(fields.body, fields.key);
    if (operation === 'task.update') return tasks.update(fields.id, fields.body);
    if (operation === 'task.start') return tasks.start(fields.id, fields.body || {}, fields.key, fields.ownerId, fields.namespace, fields.expectedVersion);
    if (operation === 'task.start.check') return tasks.checkStart(fields.id, fields.body || {}, fields.key);
    if (operation === 'task.resume') {
      const replayed = tasks.checkResume(fields.id, fields.body || {}, fields.key);
      if (replayed) return replayed;
      if (!fields.body || typeof fields.body !== 'object' || Array.isArray(fields.body) || Object.keys(fields.body).length)
        throw taskError('INVALID_RESUME', 'resume 正文必须为空对象', 400);
      const task = tasks.task(fields.id);
      if (!task.currentRunId) throw taskError('RESUME_UNAVAILABLE', 'Task 没有可恢复的 Run', 409);
      refreshControl(database.run(task.currentRunId), 'resume.check');
      return tasks.resume(fields.id, fields.body, fields.key, fields.ownerId);
    }
    if (operation === 'task.resume.check') return tasks.checkResume(fields.id, fields.body || {}, fields.key);
    if (operation === 'task.approval') {
      const replayed = tasks.checkApproval(fields.id, fields.body ?? {}, fields.key);
      if (replayed) return replayed;
      approvalInput(fields.body);
      const task = tasks.task(fields.id);
      // 先同步权威交付记录：CLI 已审批或 Job 已交付时，下面的前置条件会拒绝重复交付。
      if (task.currentRunId) refreshControl(database.run(task.currentRunId));
      return tasks.approve(fields.id, fields.body, fields.key, fields.ownerId);
    }
    if (operation === 'task.approval.check') return tasks.checkApproval(fields.id, fields.body ?? {}, fields.key);
    if (operation === 'task.launching') return tasks.launchIntent(fields.id, fields.runId, fields.ownerId);
    if (operation === 'task.spawned') return tasks.spawned(fields.id, fields.runId, fields.ownerId, fields.pid);
    if (operation === 'task.finish') return tasks.finish(fields.id, fields.runId, fields.ownerId, fields.changes);
    if (operation === 'task.recover') return tasks.recover(fields.isAlive);
    if (operation === 'executor.lease') return tasks.lease();
    if (operation === 'repositories') return repository('repository.list');
    if (operation === 'repository') return repository('repository.get', {id: fields.id});
    if (operation === 'repository.create') {
      const created = repository('repository.register', fields);
      rules.initializeRepositoryRules(created.repo_id, false);
      return created;
    }
    if (operation === 'repository.update') return repository('repository.update', {id: fields.id, patch: fields.patch});
    if (operation === 'repository.delete') {
      if (database.runs(fields.id).length || database.documents().some(document => document.projectId === fields.id) ||
          tasks.tasks(fields.id).length) throw new DataError('仓库已有关联任务、运行或知识文档，请停用仓库以保留历史记录', 409);
      const removed = repository('repository.delete', {id: fields.id});
      database.db.prepare('DELETE FROM repository_rule_settings WHERE project_id=?').run(fields.id);
      return removed;
    }
    if (operation === 'repository.rules' || operation === 'repository.rules.save') {
      if (!catalog().some(project => project.id === fields.id)) throw new DataError('仓库不存在', 404);
      return operation === 'repository.rules' ? rules.repositoryRules(fields.id) : rules.saveRepositoryRules(fields.id, fields.ruleIds, fields.expectedVersion);
    }
    if (operation === 'repository.freeze') return repository('repository.freeze', {id: fields.id, run_id: fields.run_id});
    if (operation === 'runs.import') return importRuns();
    if (operation === 'chat.project') {
      if (typeof fields.projectId !== 'string' || !fields.projectId) throw new DataError('请选择已注册的项目');
      const project = catalog().find(item => item.id === fields.projectId);
      const entry = (loadConfig(projectsPath).projects || []).find((item: Data) => item.id === fields.projectId);
      if (!project || !entry || typeof entry.root !== 'string') throw new DataError('请选择已注册的项目');
      if (!project.ready) throw new DataError(project.issue || '项目当前不可读');
      const root = path.resolve(path.dirname(projectsPath), entry.root);
      const configuration = entry.repository_config ? repository('repository.get', {id: project.id}) : null;
      const profile = configuration?.profile || (project.profile ? loadConfig(path.resolve(options.displayRoot || process.cwd(), project.profile)) : null);
      const selectedRuleIds = new Set(rules.repositoryRules(project.id).effectiveRuleIds);
      return {projectId: project.id, root, context: {id: project.id, repoId: project.repoId,
        root: project.root, allowedFiles: project.allowedFiles, ready: project.ready, issue: project.issue,
        url: project.url, executionProfile: profile,
        repository: configuration ? {defaultRef: configuration.default_ref, commit: configuration.commit,
          configDigest: configuration.config_digest, checkoutMode: configuration.checkout_mode,
          publishBranch: configuration.publish_branch, requireApproval: configuration.require_approval === true,
          allowedUrls: configuration.allowed_urls} : null,
        detectionRules: rules.rules({status: 'enabled'}).filter(rule => selectedRuleIds.has(rule.id)).map(rule => ({
          id: rule.id, version: rule.version, name: rule.name, severity: rule.severity,
          detection: rule.detection, fix_guidance: rule.fix_guidance})),
        access: 'read-only',
        tools: ['project.list_files', 'project.read_file', 'project.search']}};
    }
    if (operation === 'projects') {
      const runs = database.runs(), documents = database.documents();
      return catalog().map(project => ({...project,
        runCount: runs.filter(run => run.projectId === project.id).length,
        documentCount: documents.filter(document => document.projectId === null || document.projectId === project.id).length}));
    }
    if (operation === 'runs') return database.runs(fields.projectId).map(run => refreshControl(run)).map(({logs, ...run}) =>
      ({...run, ...controlProjection(run), canContinue: canContinue(run.status, run.outcome)}));
    if (operation === 'run') return runView(fields.id);
    if (operation === 'run.record') return database.run(fields.id);
    if (operation === 'run.control') return refreshControl(database.run(fields.id));
    if (operation === 'run.harness.stop') return refreshControl(database.run(fields.id), 'stop');
    if (operation === 'run.executor-result') {
      // 待审批（非终态）结果没有权威终态报告，不做报告/补丁校验，只刷新权威控制状态。
      const view = fields.result?.terminal !== false && fields.result?.report_ref ? runView(fields.id) : undefined;
      const record = view || refreshControl(database.run(fields.id));
      const ref = view?.issueReport?.patch_diff_ref;
      const bytes = ref ? Buffer.from(dispatch('artifact', {id: fields.id, ref}).content, 'base64') : undefined;
      return executorResult(fields.result, record, view, bytes);
    }
    if (operation === 'run.findings') {
      const record = database.run(fields.id);
      return record.agentRunId ? rules.findings({runId: record.agentRunId, limit: 1000}) : [];
    }
    if (operation === 'run.result') return resultView(runView(fields.id), dispatch('run.findings', {id: fields.id}));
    if (operation === 'run.trace') {
      const record = database.run(fields.id);
      if (!database.runEvents(record.id).length && record.agentRunId) {
        const index = artifactIndex(artifactRoot(record), record.projectId, record.agentRunId);
        for (const [ref, entry] of Object.entries(index).reverse()) {
          if (!entry['用途']?.includes('完整事件数据')) continue;
          for (const event of JSON.parse(readArtifact(artifactRoot(record), record.projectId, record.agentRunId, ref)))
            database.addEvent(record.id, event);
          break;
        }
      }
      return database.runEvents(record.id, Number(fields.after) || 0);
    }
    if (operation === 'run.guidance' || operation === 'run.guidance.submit' || operation === 'run.guidance.confirm') {
      // 使用实际 Agent Run 和项目双重定位，避免将控制台记录 ID 当成运行时引导作用域。
      const record = database.run(fields.id);
      if (!record.agentRunId) throw new DataError('Agent 尚未建立 Run，暂时不能接收引导');
      if (codingBackend(record) === 'claude_sdk' && operation !== 'run.guidance')
        throw Object.assign(new DataError('SDK Job 不接受运行中引导或改目标请求', 409), {code: 'SDK_GUIDANCE_UNSUPPORTED'});
      if (operation === 'run.guidance') return guidanceRead(record);
      if (!['running', 'paused', 'waiting_input'].includes(record.status)) throw new DataError('只有运行中或已暂停的 Run 可以接收引导');
      return operation.endsWith('confirm') ? guidanceConfirm(record, fields.guidanceId) : guidanceWrite(record, fields);
    }
    if (operation === 'run.continue') {
      const previous = database.run(fields.id);
      if (codingBackend(previous) === 'claude_sdk')
        throw Object.assign(new DataError('SDK Job 不接受带指令 continue，请使用空请求 resume', 409), {code: 'SDK_RESUME_REQUIRES_EMPTY_BODY'});
      if (previous.taskId) throw taskError('TASK_RESTART_REQUIRED', 'Task 的重新执行必须使用 start 新建 Run', 409);
      const project = catalog().find(item => item.id === previous.projectId);
      if (project?.enabled === false) throw new DataError('仓库已禁用', 409);
      dispatch('run.trace', {id: fields.id});
      const record = database.continueRun(fields.id, fields.instruction, () => tasks.assertIdle());
      if (previous.repositorySnapshot) return record;
      return project ? database.updateRun(record.id, {profile: project.profile, registry: project.registry}) : record;
    }
    if (operation === 'artifact') {
      const record = runView(fields.id);
      if (!record.artifacts.some((entry: Data) => entry.ref === fields.ref)) throw new DataError('只允许读取此 Run 的报告或补丁');
      return {encoding: 'base64', content: readArtifactBytes(artifactRoot(record), record.projectId, record.agentRunId, fields.ref).toString('base64')};
    }
    if (operation === 'run.create') return database.transaction(() => {
      tasks.assertIdle();
      const record = dispatch('run.prepare', {...fields, _runId: randomUUID(), _legacy: true});
      database.db.prepare('INSERT INTO console_runs VALUES (?,?)').run(record.id, JSON.stringify(record));
      return record;
    });
    if (operation === 'run.prepare') {
      const project = catalog().find(item => item.id === fields.projectId);
      if (!project?.ready) throw new DataError(project?.issue || '请选择已注册的项目');
      if (!fields._legacy && project.repositoryConfigVersion !== 1)
        throw taskError('REPOSITORY_FREEZE_REQUIRED', 'Task 执行需要 B1 仓库注册与冻结', 409);
      if (typeof fields.goal !== 'string' || !fields.goal.trim() || fields.goal.trim().length > 4000 ||
          !['test', 'repair'].includes(fields.mode)) throw new DataError('运行目标或模式无效');
      if (fields.additionalRuleIds !== undefined && (!Array.isArray(fields.additionalRuleIds) ||
          fields.additionalRuleIds.length > 50 || fields.additionalRuleIds.some((id: any) => typeof id !== 'string' || !id)))
        throw new DataError('追加规则须为最多 50 个规则 ID');
      let parentRunId: string | undefined;
      let inheritedIds = new Set<string>();
      if (fields.parentRunId) {
        const parent = database.runs(project.id).find(run => run.id === fields.parentRunId || run.agentRunId === fields.parentRunId);
        if (!parent?.agentRunId) throw new DataError('当前项目没有此父 Run', 404);
        const snapshot = rules.snapshot(parent.agentRunId);
        if (!snapshot) throw new DataError('父 Run 尚未生成规则快照，无法派生', 409);
        parentRunId = parent.agentRunId;
        inheritedIds = new Set(snapshot.refs.map((ref: Data) => ref.id));
      }
      const additionalRuleIds = [...new Set<string>(fields.additionalRuleIds || [])];
      for (const id of additionalRuleIds) {
        if (inheritedIds.has(id)) continue;
        const rule = rules.rule(id);
        if (!['enabled', 'draft'].includes(rule.status) ||
            (rule.scope.project_ids.length && !rule.scope.project_ids.includes(project.id))) throw new DataError('追加规则已停用或不属于当前项目');
      }
      if (fields.source_run_id) {
        const source = database.runs(project.id).find(run => run.id === fields.source_run_id || run.agentRunId === fields.source_run_id);
        if (!source?.agentRunId) throw taskError('SOURCE_RUN_NOT_FOUND', '当前项目没有此来源 Run', 404);
        const findings = rules.findings({runId: source.agentRunId, limit: 1000});
        if ((fields.source_finding_ids || []).some((id: string) => !findings.some(finding => finding.id === id)))
          throw taskError('SOURCE_FINDING_NOT_FOUND', '来源 Finding 不属于指定 Run', 404);
        fields = {...fields, source_run_id: source.agentRunId};
      }
      const runId = fields._runId;
      const frozen = project.repositoryConfigVersion === 1 ? repository('repository.freeze', {id: project.id, run_id: runId}) : null;
      if (frozen && (frozen.repo_id !== project.id || frozen.projectId !== project.id ||
          !/^[a-f0-9]{40,64}$/.test(frozen.commit) || typeof frozen.config_digest !== 'string' ||
          typeof frozen.projects_path !== 'string' || typeof frozen.profile_path !== 'string' ||
          !path.isAbsolute(frozen.projects_path) || !path.isAbsolute(frozen.profile_path)))
        throw new DataError('仓库冻结响应无效', 503);
      if (fields.execution?.url && frozen &&
          !(frozen.repository_config?.allowed_urls || []).some((origin: string) => new URL(fields.execution.url).origin === origin))
        throw taskError('EXECUTION_URL_NOT_ALLOWED', '执行 URL 超出仓库冻结白名单', 403);
      // repair 一律由 SDK 编码；test 检测流程没有编码后端。历史定义中的 legacy/repairMode 不再写入 Run。
      const backend = fields.mode === 'repair' ? 'claude_sdk' : null;
      const record = {id: runId, projectId: project.id, mode: fields.mode,
        ...(fields.execution ? {execution: executionDefinition(fields), executionMode: fields.execution.executionMode} : {}),
        ...(backend ? {coding_backend: backend} : {}),
        ...(fields.execution?.sdk_execution_id ? {sdk_execution_id: fields.execution.sdk_execution_id} : {}),
        profile: frozen?.profile_path || project.profile, registry: frozen?.projects_path || project.registry,
        repositorySnapshot: frozen,
        goal: fields.goal.trim(), ...(parentRunId ? {parentRunId} : {}), additionalRuleIds,
        ...(fields.source_run_id ? {source_run_id: fields.source_run_id} : {}),
        status: 'running', phase: 'STARTING', outcome: null, finishedAt: null, startedAt: timestamp(),
        knowledge: [], logs: [], origin: 'web', dataRoot, consoleDatabasePath: database.filename};
      return record;
    }
    if (operation === 'run.update') {
      const previous = database.run(fields.id);
      if (previous.repositorySnapshot && ['agentRunId', 'reportRef', 'artifacts', 'repositorySnapshot', 'profile',
        'registry', 'projectId', 'dataRoot', 'id', 'consoleRunId', 'repoSnapshotRef', 'memorySnapshotRef',
        'sourceManifest', 'evidenceRefs', 'inheritedEvidenceRefs', 'scopeId', 'parentRunId', 'taskId', 'execution',
        'executionMode', 'coding_backend', 'harness_job_id', 'harness_session_id', 'harness_task_id', 'harness_stage',
        'sdk_execution_id', 'source_run_id', 'source_finding_ids', 'github_reads', 'consoleDatabasePath',
        'approval', 'publication', 'delivery_diff']
          .some(key => Object.hasOwn(fields.changes || {}, key))) throw new DataError('运行身份、证据归属和仓库快照不可修改', 409);
      return database.updateRun(fields.id, fields.changes);
    }
    if (operation === 'run.stop.request') return database.requestStop(fields.id, fields.processControlId);
    if (operation === 'run.stop.failed') return database.stopFailed(fields.id, fields);
    if (operation === 'run.ended') {
      const record = database.run(fields.id);
      const changes: Data = {exitCode: fields.exitCode ?? null, pid: null, processEndedAt: timestamp(),
        processControlId: null, stopRequestedAt: null, stopPreviousStatus: null};
      if (['running', 'stopping'].includes(record.status)) Object.assign(changes, {
        status: record.status === 'stopping' ? 'cancelled' : 'failed', finishedAt: timestamp(),
        error: fields.error || record.error || (record.status === 'stopping' ?
          '用户已停止；进程退出，未生成完整收尾结果' : 'Agent 进程已结束，未记录最终结果；请查看日志'),
      });
      return database.updateRun(fields.id, changes);
    }
    if (operation === 'documents') {
      const query = String(fields.query || '').trim().toLowerCase();
      return database.documents(fields.projectId).filter(record =>
        !query || (record.title + ' ' + record.content + ' ' + record.tags.join(' ')).toLowerCase().includes(query))
        .map(({content, ...record}) => ({...record, preview: content.slice(0, 180)}));
    }
    if (operation === 'document') return database.document(fields.id);
    if (operation === 'document.delete') return database.deleteDocument(fields.id);
    if (operation === 'document.save' || operation === 'search') {
      const projectId = fields.projectId || null;
      if (projectId && !catalog().some(project => project.id === projectId)) throw new DataError('知识范围必须是已注册项目或全局');
      if (operation === 'search') {
        if (!projectId) throw new DataError('检索必须指定项目');
        return database.search(fields.query || '', projectId);
      }
      const sourceRunId = fields.id ? database.document(fields.id).sourceRunId : fields.sourceRunId;
      if (sourceRunId && database.run(sourceRunId).projectId !== projectId) throw new DataError('运行经验须先归档到来源项目');
      return database.saveDocument(fields, fields.id);
    }
    if (operation === 'rules') return rules.rules(fields);
    if (operation === 'rule') return rules.rule(fields.id);
    if (operation === 'rule.save') return rules.save(fields, fields.id, fields.expectedVersion ?? fields.expected_version);
    if (operation === 'rule.create' || operation === 'rule.update')
      return rules.save({...fields, scope: {level: 'org'}}, fields.id, fields.expectedVersion ?? fields.expected_version, operation === 'rule.create' ? 'create' : 'update');
    if (operation === 'rule.status') return rules.status(fields.id, fields.status, fields.expectedVersion);
    if (operation === 'rule.delete') return rules.delete(fields.id);
    if (operation === 'rule.versions') return rules.versions(fields.id);
    if (operation === 'rule.preview') return rules.preview(fields.id, Number(fields.maxTokens) || 1200);
    if (operation === 'rule.rollback') return rules.rollback(fields.id, Number(fields.version));
    if (operation === 'rule.findings') return rules.findings(fields);
    if (operation === 'rule.insights') return rules.insights(fields);
    if (operation === 'rule-sets') return rules.ruleSets();
    if (operation === 'rule-set.save') return rules.saveRuleSet(fields);
    throw new DataError('未知控制台操作');
  }

  return Object.assign(dispatch, {close: () => database.db.close()});
}
