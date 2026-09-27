import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import readline from 'node:readline/promises';
import {fileURLToPath} from 'node:url';
import {createConsoleService} from '@tracefix/console-service/dispatch';
import {DataError} from '@tracefix/console-service/database';
import {configureProject, createProject} from '@tracefix/console-service/config';
import {capabilities, palette} from './terminal.js';
import {LineEditor} from './editor.js';
import {banner, help as helpLines, PLAIN_HELP} from './render.js';
import {suggest} from './registry.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
process.chdir(root);

function loadEnvironment(): void {
  const filename = path.join(root, '.env');
  if (!fs.existsSync(filename)) return;
  for (const line of fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const separator = line.indexOf('=');
    if (separator < 1 || line.trimStart().startsWith('#')) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim().replace(/^("|')(.*)\1$/, '$2');
    process.env[key] ??= value;
  }
}

function tokens(text: string): string[] {
  const result: string[] = [];
  let quote = '', current = '', started = false;
  for (const character of text) {
    if (quote) { if (character === quote) quote = ''; else current += character; }
    else if (character === '"' || character === "'") { quote = character; started = true; }
    else if (/\s/.test(character)) { if (started) { result.push(current); current = ''; started = false; } }
    else { current += character; started = true; }
  }
  if (quote) throw new DataError('引号未闭合');
  if (started) result.push(current);
  return result;
}

function argumentsOf(args: string[]): {options: Record<string, string | boolean>; positionals: string[]} {
  const options: Record<string, string | boolean> = {}, positionals: string[] = [];
  for (let index = 0; index < args.length; index++) {
    if (!args[index].startsWith('--')) { positionals.push(args[index]); continue; }
    const name = args[index].slice(2);
    if (['global', 'disabled', 'all', 'no-knowledge', 'remote-write'].includes(name)) options[name] = true;
    else options[name] = args[++index] || '';
  }
  return {options, positionals};
}

/**
 * 交互期间的行编辑器。非 TTY 路径下始终为 null，
 * 因此 print / 错误输出与改造前完全一致（纯文本、直写 stdout）。
 */
let editor: LineEditor | null = null;

/** 输出前先擦掉输入行，输出后重绘，避免流式输出冲掉用户正在敲的内容。 */
function emit(write: () => void): void {
  if (editor) editor.external(write);
  else write();
}

function print(value: any): void {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  emit(() => process.stdout.write(text + '\n'));
}

function pythonCommand(): [string, string[]] {
  const executable = process.platform === 'win32' ? path.join(root, '.venv', 'Scripts', 'python.exe') : path.join(root, '.venv', 'bin', 'python');
  return fs.existsSync(executable) ? [executable, []] : process.platform === 'win32' ? ['py', ['-3.12']] : ['python3', []];
}

async function agent(args: string[], environment: Record<string, string> = {}): Promise<number> {
  const [python, prefix] = pythonCommand();
  const child = spawn(python, [...prefix, 'tools/bootstrap/launch.py', '--plain', '--console-db', databasePath, ...args], {
    cwd: root, stdio: 'inherit', windowsHide: true, env: {...process.env, ...environment},
  });
  return new Promise((resolve, reject) => { child.once('error', reject); child.once('close', code => resolve(code || 0)); });
}

function documentFile(filename: string): string {
  if (!/\.(md|markdown|txt)$/i.test(filename)) throw new DataError('请选择 .md、.markdown 或 .txt 文本文件');
  const raw = fs.readFileSync(filename);
  if (raw.length > 262144) throw new DataError('文档不能超过 256 KiB');
  const text = new TextDecoder('utf-8', {fatal: true}).decode(raw).replace(/^\uFEFF/, '');
  if (!text.trim() || text.includes('\0')) throw new DataError('文档必须是非空 UTF-8 文本');
  return text;
}

function exportFile(filename: string, content: string): void {
  fs.mkdirSync(path.dirname(path.resolve(filename)), {recursive: true});
  fs.writeFileSync(filename, content, {encoding: 'utf8', flag: 'wx'});
  print(`已导出 ${filename}`);
}

async function editContent(content: string): Promise<string> {
  const draftDirectory = path.join(dataRoot, 'document-drafts');
  fs.mkdirSync(draftDirectory, {recursive: true});
  const filename = path.join(draftDirectory, `draft-${Date.now()}.md`);
  fs.writeFileSync(filename, content, {flag: 'wx'});
  const editor = tokens(process.env.TRACEFIX_EDITOR || process.env.VISUAL || process.env.EDITOR ||
    (process.platform === 'win32' ? 'notepad.exe' : 'vi'));
  const child = spawn(editor[0], [...editor.slice(1), filename], {stdio: 'inherit', windowsHide: true});
  const code = await new Promise<number>((resolve, reject) => {
    child.once('error', reject); child.once('close', value => resolve(value || 0));
  });
  if (code) throw new DataError(`编辑未完成，草稿保留在 ${filename}`);
  const updated = documentFile(filename);
  if (updated !== content) fs.rmSync(filename);
  return updated;
}

loadEnvironment();
const cliArgs = process.argv.slice(2);
const option = (name: string, fallback = '') => {
  const index = cliArgs.indexOf('--' + name);
  return index < 0 ? fallback : cliArgs[index + 1] || fallback;
};
const projectsPath = option('projects', process.env.TRACEFIX_PROJECTS || 'profiles/projects.yaml');
const dataRoot = option('data', process.env.TRACEFIX_DATA || '.tracefix');
const databasePath = option('console-db', process.env.TRACEFIX_CONSOLE_DB || path.join(dataRoot, 'console.sqlite3'));
const dispatch = createConsoleService({projectsPath, dataRoot, databasePath});
const terminal = capabilities();
const colors = palette(terminal.color);
let projectId = option('project', 'bugboard');
let mode = option('mode', 'test');
let goal = option('goal');
let activeAgent: ReturnType<typeof spawn> | null = null;
const chatHistory = new Map<string, Array<{role: string; content: string}>>();
const sessionRemote = new Map<string, Record<string, any>>();
const agentEnvironment = () => ({
  TRACEFIX_CONSOLE_DB: path.resolve(databasePath),
  TRACEFIX_DATA: path.resolve(dataRoot),
  ...(sessionRemote.has(projectId) ? {TRACEFIX_SESSION_REMOTE: JSON.stringify(sessionRemote.get(projectId))} : {}),
});

function interactiveAgent(args: string[], environment: Record<string, string>, recordId: string, commands: string[]): void {
  if (activeAgent) throw new DataError('已有 Agent Run 正在执行');
  const [python, prefix] = pythonCommand();
  const child = spawn(python, [...prefix, 'tools/bootstrap/launch.py', '--plain', '--console-db', databasePath, ...args], {
    cwd: root, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, env: {...process.env, ...environment},
  });
  activeAgent = child;
  child.stdout?.on('data', chunk => emit(() => process.stdout.write(chunk)));
  child.stderr?.on('data', chunk => emit(() => process.stderr.write(chunk)));
  child.stdin?.on('error', error => emit(() => process.stderr.write(error.message + '\n')));
  child.once('error', error => emit(() => process.stderr.write(error.message + '\n')));
  child.once('close', code => {
    activeAgent = null;
    if (code && cliArgs.includes('--command')) process.exitCode = code;
    try { dispatch('run.ended', {id: recordId, exitCode: code}); }
    catch (error) { emit(() => process.stderr.write(String(error) + '\n')); }
  });
  for (const command of commands) child.stdin?.write(command + '\n');
  const monitor = setInterval(() => {
    if (activeAgent !== child) return clearInterval(monitor);
    try {
      const status = dispatch('run', {id: recordId}).status;
      if (['completed', 'failed', 'cancelled', 'abnormal'].includes(status)) {
        child.stdin?.write('/quit\n');
        clearInterval(monitor);
      }
    } catch { clearInterval(monitor); }
  }, 2000);
  monitor.unref();
}

function findRun(id: string): any {
  const record = dispatch('runs', {projectId}).find((item: any) => item.id === id || item.agentRunId === id);
  if (!record) throw new DataError('当前项目没有此运行记录');
  return dispatch('run', {id: record.id});
}

async function execute(text: string): Promise<boolean> {
  const args = tokens(text.trim());
  if (!args.length) return true;
  if (!args[0].startsWith('/')) {
    goal = text.trim();
    // 非交互路径保持原样的一行文本；交互下额外回显记录到的目标。
    if (editor) {
      print(colors.grey('目标已记录：') + colors.bold(goal));
      print(colors.grey('输入 ') + colors.bold('/run') + colors.grey(' 开始，或 ') +
        colors.bold('/mode') + colors.grey(' 切换模式。'));
    } else print('目标已记录。输入 /run 开始。');
    return true;
  }
  const command = args.shift()!.slice(1);
  const {options, positionals} = argumentsOf(args);
  if (command === 'quit') { if (activeAgent) activeAgent.stdin?.write('/quit\n'); return false; }
  if (command === 'help') {
    // 只有真正的交互会话（editor 已启动）才用分组帮助；
    // --command / 管道等非交互路径保持改造前的三行纯文本。
    const lines = editor ? helpLines(colors, process.stdout.columns || terminal.columns) : PLAIN_HELP;
    for (const line of lines) print(line);
    return true;
  }
  if (command === 'mode') { if (!['test', 'repair', 'chat'].includes(positionals[0])) throw new DataError('用法：/mode test|repair|chat');
    mode = positionals[0]; print(`新 Run 模式：${mode}`); return true; }
  if (command === 'projects' || command === 'scope') {
    const [action = 'list', id] = positionals;
    if (action === 'create') {
      if (!id || !positionals[2]) throw new DataError('用法：/projects create PROJECT_ID ROOT');
      print(createProject(projectsPath, id, positionals[2], String(options['repo-id'] || id)));
      return true;
    }
    if (action === 'configure') {
      const commands = Object.fromEntries(['start', 'reset', 'static', 'unit', 'build'].map(name =>
        [name, tokens(String(options[name] || ''))]));
      print(configureProject(projectsPath, id || projectId, {commands, url: options.url, sourceCommit: options['source-commit']}));
      return true;
    }
    const projects = dispatch('projects');
    if (action === 'use') {
      if (!projects.some((project: any) => project.id === id)) throw new DataError('未注册的项目');
      projectId = id; print(`当前项目：${id}`);
    } else if (action === 'show') print(projects.find((project: any) => project.id === (id || projectId)) || '未注册的项目');
    else if (action === 'list') print(projects);
    else throw new DataError('用法：/projects list|show|use');
    return true;
  }
  if (command === 'knowledge' || command === 'memory') {
    const [action = 'list', id, filename] = positionals;
    if (action === 'list') print(dispatch('documents', {projectId, query: options.query || ''})
      .filter((record: any) => !options.kind || record.kind === options.kind));
    else if (action === 'show') print(dispatch('document', {id}));
    else if (action === 'search') print(dispatch('search', {projectId, query: positionals.slice(1).join(' ')}));
    else if (action === 'new' || action === 'import') {
      const content = options.file || action === 'import' ? documentFile(String(options.file || id)) :
        await editContent('## 适用场景\n\n## 修复或测试方法\n\n## 验证与局限\n');
      print(dispatch('document.save', {title: String(options.title || (action === 'new' ? id : path.parse(id).name)), content,
        kind: options.kind || 'experience', tags: String(options.tags || '').split(',').filter(Boolean),
        enabled: !options.disabled, projectId: options.global ? null : projectId}));
    } else if (action === 'edit' || action === 'enable' || action === 'disable') {
      const record = dispatch('document', {id});
      if (record.projectId && record.projectId !== projectId) throw new DataError('请先切换到此文档所属项目', 403);
      if (action === 'edit' && options.file && options.version === undefined) throw new DataError('从文件更新须指定 --version');
      if (options.version !== undefined && Number(options.version) !== record.version) throw new DataError('文档版本已变化', 409);
      const changedMetadata = Boolean(options.title || options.kind || options.tags || options.scope);
      print(dispatch('document.save', {...record, title: options.title || record.title,
        content: options.file ? documentFile(String(options.file)) : action === 'edit' && !changedMetadata ?
          await editContent(record.content) : record.content,
        kind: options.kind || record.kind, tags: options.tags ? String(options.tags).split(',') : record.tags,
        enabled: action === 'enable' ? true : action === 'disable' ? false : record.enabled,
        projectId: options.scope === 'global' ? null : record.projectId}));
    } else if (action === 'export') exportFile(filename, dispatch('document', {id}).content);
    else throw new DataError('未知知识库命令');
    return true;
  }
  if (command === 'runs') {
    const [action = 'list', id, ref, filename] = positionals;
    if (action === 'list') {
      const runs = dispatch('runs', {projectId: options.all ? undefined : projectId});
      print(runs.filter((run: any) => (!options.status || run.status === options.status) &&
        (!options.query || JSON.stringify(run).includes(String(options.query)))).slice(0, Number(options.limit) || 50));
    } else if (action === 'show') print(findRun(id));
    else if (action === 'logs') print(findRun(id).logs || []);
    else if (action === 'trace') print(dispatch('run.trace', {id: findRun(id).id}));
    else if (action === 'sources') print(findRun(id).knowledge || []);
    else if (action === 'export') exportFile(filename, dispatch('artifact', {id: findRun(id).id, ref}).content);
    else if (action === 'remember') {
      const run = findRun(id);
      print(dispatch('document.save', {title: String(options.title || run.goal).slice(0, 120),
        content: options.file ? documentFile(String(options.file)) : `# ${run.goal}\n\n## 复现步骤\n\n## 原因\n\n## 修复或测试方法\n`,
        kind: 'experience', tags: [], enabled: false, projectId, sourceRunId: run.id}));
    } else if (action === 'continue') await continueRun(id, positionals.slice(2).join(' '), !cliArgs.includes('--command'));
    else throw new DataError('未知运行记录命令');
    return true;
  }
  if (command === 'status') { print(dispatch('runs', {projectId})[0] || '暂无 Run'); return true; }
  if (command === 'trace' || command === 'diff' || command === 'report' || command === 'evidence' || command === 'model-log' || command === 'context') {
    const latest = dispatch('runs', {projectId})[0];
    if (!latest) throw new DataError('当前没有 Run');
    const run = dispatch('run', {id: latest.id});
    if (command === 'trace') print(dispatch('run.trace', {id: run.id}));
    else if (command === 'context') print({goal: run.goal, phase: run.phase, knowledge: run.knowledge, outcome: run.outcome});
    else if (command === 'model-log') print((run.artifacts || []).filter((item: any) => /模型|请求|响应/.test(item.label)));
    else {
      const artifact = command === 'evidence' ? run.artifacts.find((item: any) => item.ref === positionals[0]) :
        command === 'diff' ? [...run.artifacts].reverse().find((item: any) => item.ref.endsWith('.diff')) :
          run.artifacts.find((item: any) => item.ref === run.reportRef || item.ref.endsWith('.html'));
      if (!artifact) throw new DataError('当前没有此产物');
      if (command === 'diff') print(dispatch('artifact', {id: run.id, ref: artifact.ref}).content);
      else print(path.join(run.dataRoot || dataRoot, 'artifacts', run.projectId, run.agentRunId, artifact.ref));
    }
    return true;
  }
  if (command === 'skills') { print(fs.readdirSync(path.join(root, 'skills'), {withFileTypes: true})
    .filter(item => item.isDirectory()).map(item => item.name)); return true; }
  if (command === 'run') { await startRun(!cliArgs.includes('--command')); return true; }
  if (command === 'continue') { await continueRun(positionals[0], positionals.slice(1).join(' '), !cliArgs.includes('--command')); return true; }
  if (['pause', 'cancel', 'interrupt', 'approve', 'reject', 'resume'].includes(command) && activeAgent) {
    activeAgent.stdin?.write('/' + command + (positionals.length ? ' ' + positionals.map(value => JSON.stringify(value)).join(' ') : '') + '\n');
    return true;
  }
  if (command === 'resume' || command === 'approve' || command === 'reject') {
    const commands = command === 'resume' ? [`/resume ${findRun(positionals[0]).agentRunId}`] :
      [`/resume ${findRun(positionals[1] || dispatch('runs', {projectId})[0]?.id).agentRunId}`, `/${command} ${positionals[0]}`];
    const code = await agent(['--project', projectId, '--projects', projectsPath, '--data', dataRoot,
      ...commands.flatMap(value => ['--command', value])], agentEnvironment());
    if (code) throw new DataError(`Agent 退出码：${code}`);
    return true;
  }
  if (command === 'remote') {
    const filename = path.join(dataRoot, 'remote-config.json');
    const state = fs.existsSync(filename) ? JSON.parse(fs.readFileSync(filename, 'utf8')) : {projects: {}};
    state.projects ||= {};
    const action = positionals[0] || 'show';
    if (action === 'show') print({projectId, project: state.projects[projectId] || null,
      session: sessionRemote.get(projectId) || null, effective: {...(state.projects[projectId] || {}), ...(sessionRemote.get(projectId) || {})}});
    else if (action === 'clear') {
      if (options.scope === 'session') sessionRemote.delete(projectId);
      else { delete state.projects[projectId]; fs.mkdirSync(dataRoot, {recursive: true}); fs.writeFileSync(filename, JSON.stringify(state, null, 2)); }
      print('远程配置已清除');
    }
    else if (action === 'set') {
      const repository = String(options.repository || '').replace(/^https:\/\/github\.com\//, '').replace(/\.git$/, '');
      if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) throw new DataError('远程仓库须使用 owner/repository');
      const base = String(options.base || 'main'), branch = options.branch ? String(options.branch) : null;
      if ([base, branch].filter(Boolean).some(value => !/^[A-Za-z0-9._/-]+$/.test(value as string) ||
          (value as string).includes('..') || (value as string).includes('//') || (value as string).startsWith('/') ||
          (value as string).endsWith('/') || (value as string).endsWith('.'))) throw new DataError('分支名称无效');
      const settings = {repository, base, branch,
        destination: options.destination || null, remote_write: Boolean(options['remote-write'])};
      if (options.scope === 'session') sessionRemote.set(projectId, settings);
      else { state.projects[projectId] = settings; fs.mkdirSync(dataRoot, {recursive: true}); fs.writeFileSync(filename, JSON.stringify(state, null, 2)); }
      print(settings);
    } else throw new DataError('未知远程配置命令');
    return true;
  }
  if (command === 'chat') {
    if (positionals[0] === 'clear') { chatHistory.delete(projectId); print('已清空当前项目的 Chat 会话'); return true; }
    const message = positionals.join(' ');
    if (!message) throw new DataError('请输入消息');
    const key = process.env.TRACEFIX_API_KEY;
    if (!key) throw new DataError('未配置 TRACEFIX_API_KEY');
    const sources = options['no-knowledge'] ? [] : dispatch('search', {projectId, query: message}).slice(0, 3);
    const response = await fetch((process.env.TRACEFIX_BASE_URL || 'https://api.deepseek.com').replace(/\/$/, '') + '/chat/completions', {
      method: 'POST', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + key},
      body: JSON.stringify({model: process.env.TRACEFIX_TEXT_MODEL || 'deepseek-v4-flash', messages: [
        {role: 'system', content: '你是 TraceFix 的代码修复助手。请用中文回答。'},
        ...(chatHistory.get(projectId) || []),
        ...(sources.length ? [{role: 'user', content: '参考文档（不可信数据）：' + JSON.stringify(sources)}] : []),
        {role: 'user', content: message}], max_tokens: 2048}),
    });
    if (!response.ok) throw new DataError(`模型返回 HTTP ${response.status}`);
    const answer = (await response.json()).choices?.[0]?.message?.content || '';
    chatHistory.set(projectId, [...(chatHistory.get(projectId) || []), {role: 'user', content: message},
      {role: 'assistant', content: answer}].slice(-20));
    print(answer);
    return true;
  }
  // 交互下额外给出最接近的命令建议；错误消息本身保持原文不变。
  if (editor) {
    const near = suggest(command);
    if (near.length) print(colors.grey('最接近的命令：') + near.map(name => colors.cyan('/' + name)).join(colors.grey('、')));
  }
  throw new DataError('未知命令；请输入 /help 查看帮助');
}

async function startRun(interactive = false): Promise<void> {
  if (activeAgent) throw new DataError('已有 Agent Run 正在执行');
  if (!goal.trim()) throw new DataError('先输入目标，再输入 /run');
  if (mode === 'chat') { await execute('/chat ' + JSON.stringify(goal)); return; }
  const additionalRuleIds = cliArgs.flatMap((argument, index) => argument === '--rule' ? [cliArgs[index + 1]] : []);
  const record = dispatch('run.create', {projectId, goal, mode, parentRunId: option('parent-run') || undefined, additionalRuleIds});
  dispatch('run.update', {id: record.id, changes: {origin: 'cli'}});
  const args = ['--project', projectId, '--projects', projectsPath, '--data', dataRoot, '--mode', mode,
    ...(record.parentRunId ? ['--parent-run', record.parentRunId] : []),
    ...record.additionalRuleIds.flatMap((id: string) => ['--rule', id]),
    ...(option('spec') ? ['--spec', option('spec')] : [])];
  if (interactive) {
    interactiveAgent(args, {...agentEnvironment(), TRACEFIX_CONSOLE_RUN_ID: record.id}, record.id, [goal, '/run']);
    return;
  }
  const code = await agent([...args, '--goal', goal, '--run'], {...agentEnvironment(), TRACEFIX_CONSOLE_RUN_ID: record.id});
  dispatch('run.ended', {id: record.id, exitCode: code});
  if (code) throw new DataError(`Agent 退出码：${code}`);
}

async function continueRun(id: string, instruction: string, interactive = false): Promise<void> {
  if (activeAgent) throw new DataError('已有 Agent Run 正在执行');
  if (!id || !instruction.trim()) throw new DataError('用法：/continue RUN_ID INSTRUCTION');
  const run = findRun(id);
  const record = dispatch('run.continue', {id: run.id, instruction});
  if (interactive) {
    interactiveAgent(['--project', projectId, '--projects', projectsPath, '--data', dataRoot],
      {...agentEnvironment(), TRACEFIX_CONSOLE_RUN_ID: record.id, TRACEFIX_CONTINUATION_ID: record.continuationId},
      record.id, ['/continue ' + JSON.stringify(record.id) + ' ' + JSON.stringify(instruction)]);
    return;
  }
  const code = await agent(['--project', projectId, '--projects', projectsPath, '--data', dataRoot,
    '--continue-run', record.id, '--instruction', instruction],
    {...agentEnvironment(), TRACEFIX_CONSOLE_RUN_ID: record.id, TRACEFIX_CONTINUATION_ID: record.continuationId});
  dispatch('run.ended', {id: record.id, exitCode: code});
  if (code) throw new DataError(`Agent 退出码：${code}`);
}

async function main(): Promise<void> {
  const commands = cliArgs.flatMap((argument, index) => argument === '--command' ? [cliArgs[index + 1]] : []).filter(Boolean);
  if (cliArgs.includes('--doctor')) {
    print({node: process.version, python: pythonCommand()[0], platform: process.platform,
      api_key_configured: Boolean(process.env.TRACEFIX_API_KEY), database_configured: Boolean(process.env.TRACEFIX_DATABASE_URL)});
    return;
  }
  if (cliArgs.includes('--run')) { await startRun(); return; }
  if (cliArgs.includes('--continue-run')) { await continueRun(option('continue-run'), option('instruction')); return; }
  if (commands.length) { for (const command of commands) await execute(command); return; }
  if (terminal.rich) return interactive();
  return basic();
}

/** 改造前的朴素交互路径：非 TTY、NO_COLOR、TERM=dumb 时使用，输出逐字保持原样。 */
async function basic(): Promise<void> {
  const input = readline.createInterface({input: process.stdin, output: process.stdout});
  print(`TraceFix · ${projectId} · ${mode} · 输入 /help 查看命令`);
  try {
    while (true) {
      let line: string;
      try { line = await input.question('TraceFix > '); } catch { break; }
      try { if (!await execute(line)) break; }
      catch (error) { process.stderr.write((error instanceof Error ? error.message : String(error)) + '\n'); }
    }
  } finally { input.close(); }
}

/** TTY 交互路径：横幅、状态行、斜杠菜单、行编辑与历史。 */
async function interactive(): Promise<void> {
  const session = new LineEditor(terminal, colors,
    {projectId: () => projectId, mode: () => mode, running: () => Boolean(activeAgent), goal: () => goal},
    {interrupt: () => {
      if (!activeAgent) return false;
      activeAgent.stdin?.write('/interrupt\n');
      return true;
    }},
    dataRoot);
  editor = session;
  for (const line of banner({projectId, mode, node: process.version, version: '0.1.1'}, colors, terminal.columns)) {
    process.stdout.write(line + '\n');
  }
  session.start();
  try {
    while (true) {
      const line = await session.read();
      if (line === null) break;
      try { if (!await execute(line)) break; }
      catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        session.external(() => process.stderr.write(colors.red(message) + '\n'));
      }
    }
  } finally {
    session.stop();
    editor = null;
  }
}

main().catch(error => { process.stderr.write((error instanceof Error ? error.message : String(error)) + '\n'); process.exitCode = 2; });
