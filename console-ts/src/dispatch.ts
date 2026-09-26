import fs from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {artifactIndex, readArtifact} from './artifacts.js';
import {projectCatalog} from './config.js';
import {canContinue, ConsoleDatabase, DataError, timestamp} from './database.js';
import {RuleDatabase} from './rules.js';

type Data = Record<string, any>;

export interface ConsoleOptions {projectsPath?: string; dataRoot?: string; databasePath?: string; displayRoot?: string}

export function createConsoleService(options: ConsoleOptions = {}) {
  const projectsPath = path.resolve(options.projectsPath || process.env.TRACEFIX_PROJECTS || 'profiles/projects.yaml');
  const dataRoot = path.resolve(options.dataRoot || process.env.TRACEFIX_DATA || '.tracefix');
  const database = new ConsoleDatabase(path.resolve(options.databasePath || process.env.TRACEFIX_CONSOLE_DB || path.join(dataRoot, 'console.sqlite3')));
  const rules = new RuleDatabase(database);
  const catalog = () => projectCatalog(projectsPath, dataRoot, options.displayRoot);
  const artifactRoot = (run: Data) => path.join(run.dataRoot || dataRoot, 'artifacts');

  function runView(id: string): Data {
    const record = database.run(id);
    const index = record.agentRunId ? artifactIndex(artifactRoot(record), record.projectId, record.agentRunId) : {};
    const knowledgeRefs = new Set((record.knowledge || []).map((entry: Data) => entry.artifact_ref));
    const artifacts = Object.entries(index).filter(([ref, entry]) =>
      ref.endsWith('.diff') || ref.endsWith('.html') || ref === record.reportRef || knowledgeRefs.has(ref) ||
      ['修复报告数据', '完整事件数据', '继续执行前状态'].some(name => entry['用途']?.includes(name)))
      .map(([ref, entry]) => ({ref, label: entry['用途'], bytes: entry['字节数']}));
    return {...record, canContinue: canContinue(record.status, record.outcome), artifacts};
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
      return database.updateRun(randomUUID(), {...fields, profile: project.profile, registry: project.registry,
        status: 'running', phase: 'STARTING', outcome: null, finishedAt: null, origin: 'web', dataRoot}, true);
    }
    if (operation === 'run.update') return database.updateRun(fields.id, fields.changes);
    if (operation === 'run.ended') {
      const record = database.run(fields.id);
      const changes: Data = {exitCode: fields.exitCode ?? null, pid: null, processEndedAt: timestamp()};
      if (['running', 'stopping'].includes(record.status)) Object.assign(changes, {
        status: record.status === 'stopping' ? 'cancelled' : 'failed', finishedAt: timestamp(),
        error: fields.error || record.error || 'Agent 进程已结束，未记录最终结果；请查看日志',
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
