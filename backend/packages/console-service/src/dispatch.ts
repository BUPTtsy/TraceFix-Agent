import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {artifactIndex, readArtifact} from './artifacts.js';
import {loadConfig, projectCatalog} from './config.js';
import {canContinue, ConsoleDatabase, DataError, timestamp} from './database.js';
import {RuleDatabase} from './rules.js';
import {classifyGuidance, guidanceUnsafe} from './guidance.js';

type Data = Record<string, any>;

export interface ConsoleOptions {projectsPath?: string; dataRoot?: string; databasePath?: string; displayRoot?: string}

export function createConsoleService(options: ConsoleOptions = {}) {
  const projectsPath = path.resolve(options.projectsPath || process.env.TRACEFIX_PROJECTS || 'profiles/projects.yaml');
  const dataRoot = path.resolve(options.dataRoot || process.env.TRACEFIX_DATA || '.tracefix');
  const database = new ConsoleDatabase(path.resolve(options.databasePath || process.env.TRACEFIX_CONSOLE_DB || path.join(dataRoot, 'console.sqlite3')));
  const rules = new RuleDatabase(database);
  const catalog = () => projectCatalog(projectsPath, dataRoot, options.displayRoot);
  const artifactRoot = (run: Data) => path.join(run.dataRoot || dataRoot, 'artifacts');
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
    const record = database.run(id);
    const index = record.agentRunId ? artifactIndex(artifactRoot(record), record.projectId, record.agentRunId) : {};
    const knowledgeRefs = new Set((record.knowledge || []).map((entry: Data) => entry.artifact_ref));
    const artifacts = Object.entries(index).filter(([ref, entry]) =>
      ref.endsWith('.diff') || ref.endsWith('.html') || ref === record.reportRef || knowledgeRefs.has(ref) ||
      ['修复报告数据', '完整事件数据', '继续执行前状态'].some(name => entry['用途']?.includes(name)))
      .map(([ref, entry]) => ({ref, label: entry['用途'], bytes: entry['字节数']}));
    return {...record, canContinue: canContinue(record.status, record.outcome), artifacts,
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
    if (operation === 'runs.import') return importRuns();
    if (operation === 'chat.project') {
      if (typeof fields.projectId !== 'string' || !fields.projectId) throw new DataError('请选择已注册的项目');
      const project = catalog().find(item => item.id === fields.projectId);
      const entry = (loadConfig(projectsPath).projects || []).find((item: Data) => item.id === fields.projectId);
      if (!project || !entry || typeof entry.root !== 'string') throw new DataError('请选择已注册的项目');
      if (!project.ready) throw new DataError(project.issue || '项目当前不可读');
      const root = path.resolve(path.dirname(projectsPath), entry.root);
      return {projectId: project.id, root, context: {id: project.id, repoId: project.repoId,
        root: project.root, allowedFiles: project.allowedFiles, ready: project.ready, issue: project.issue,
        access: 'read-only',
        tools: ['project.list_files', 'project.read_file', 'project.search']}};
    }
    if (operation === 'projects') {
      const runs = database.runs(), documents = database.documents();
      return catalog().map(project => ({...project,
        runCount: runs.filter(run => run.projectId === project.id).length,
        documentCount: documents.filter(document => document.projectId === null || document.projectId === project.id).length}));
    }
    if (operation === 'runs') return database.runs(fields.projectId).map(({logs, ...run}) =>
      ({...run, canContinue: canContinue(run.status, run.outcome)}));
    if (operation === 'run') return runView(fields.id);
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
      if (operation === 'run.guidance') return guidanceRead(record);
      if (!['running', 'paused', 'waiting_input'].includes(record.status)) throw new DataError('只有运行中或已暂停的 Run 可以接收引导');
      return operation.endsWith('confirm') ? guidanceConfirm(record, fields.guidanceId) : guidanceWrite(record, fields);
    }
    if (operation === 'run.continue') {
      const previous = database.run(fields.id);
      dispatch('run.trace', {id: fields.id});
      const record = database.continueRun(fields.id, fields.instruction);
      const project = catalog().find(item => item.id === previous.projectId);
      return project ? database.updateRun(record.id, {profile: project.profile, registry: project.registry}) : record;
    }
    if (operation === 'artifact') {
      const record = runView(fields.id);
      if (!record.artifacts.some((entry: Data) => entry.ref === fields.ref)) throw new DataError('只允许读取此 Run 的报告或补丁');
      return {content: readArtifact(artifactRoot(record), record.projectId, record.agentRunId, fields.ref)};
    }
    if (operation === 'run.create') {
      const project = catalog().find(item => item.id === fields.projectId);
      if (!project?.ready) throw new DataError(project?.issue || '请选择已注册的项目');
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
      return database.updateRun(randomUUID(), {...fields, profile: project.profile, registry: project.registry,
        goal: fields.goal.trim(), ...(parentRunId ? {parentRunId} : {}), additionalRuleIds,
        status: 'running', phase: 'STARTING', outcome: null, finishedAt: null, origin: 'web', dataRoot}, true);
    }
    if (operation === 'run.update') return database.updateRun(fields.id, fields.changes);
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
      return rules.save(fields, fields.id, fields.expectedVersion ?? fields.expected_version, operation === 'rule.create' ? 'create' : 'update');
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
