import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

type Entry = {indent: number; content: string};

function scalar(raw: string): unknown {
  const value = raw.trim();
  if (!value || value === 'null' || value === '~') return null;
  if (value === 'true') return true;
  if (value === 'false') return false;
  if (/^-?\d+$/.test(value)) return Number(value);
  if (value.startsWith('[') || value.startsWith('{')) {
    try { return JSON.parse(value); } catch { throw new Error('配置中的内联 YAML 必须使用 JSON 兼容格式'); }
  }
  if (value.startsWith('"') || value.startsWith("'")) {
    if (value[0] !== value.at(-1)) throw new Error('配置中的引号未闭合');
    return value[0] === '"' ? JSON.parse(value) : value.slice(1, -1).replaceAll("''", "'");
  }
  return value.replace(/\s+#.*$/, '');
}

export function parseConfig(text: string): Record<string, any> {
  const entries: Entry[] = text.split(/\r?\n/).filter(line => line.trim() && !line.trimStart().startsWith('#'))
    .map(line => ({indent: line.length - line.trimStart().length, content: line.trim()}));
  let position = 0;
  function mapping(indent: number, initial: Record<string, any> = {}): Record<string, any> {
    const result = initial;
    while (position < entries.length && entries[position].indent === indent && !entries[position].content.startsWith('- ')) {
      const entry = entries[position++];
      const separator = entry.content.indexOf(':');
      if (separator < 1) throw new Error('项目配置格式无效');
      const key = entry.content.slice(0, separator).trim();
      const value = entry.content.slice(separator + 1).trim();
      result[key] = value ? scalar(value) : position < entries.length &&
        (entries[position].indent > indent || entries[position].indent === indent && entries[position].content.startsWith('- '))
        ? block(entries[position].indent) : null;
    }
    return result;
  }
  function sequence(indent: number): unknown[] {
    const result: unknown[] = [];
    while (position < entries.length && entries[position].indent === indent && entries[position].content.startsWith('- ')) {
      const content = entries[position++].content.slice(2).trim();
      const separator = content.indexOf(':');
      if (separator > 0 && /^\s|^$/.test(content.slice(separator + 1)) && !content.startsWith('"') && !content.startsWith("'")) {
        const key = content.slice(0, separator).trim();
        const value = content.slice(separator + 1).trim();
        const item: Record<string, any> = {[key]: value ? scalar(value) : null};
        if (position < entries.length && entries[position].indent > indent) mapping(entries[position].indent, item);
        result.push(item);
      } else {
        result.push(content ? scalar(content) : block(entries[position].indent));
      }
    }
    return result;
  }
  function block(indent: number): any {
    return entries[position]?.content.startsWith('- ') ? sequence(indent) : mapping(indent);
  }
  const data = entries.length ? block(entries[0].indent) : {};
  if (position !== entries.length || !data || Array.isArray(data)) throw new Error('项目配置格式无效');
  return data;
}

export function loadConfig(filename: string): Record<string, any> {
  const content = fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, '');
  return content.trimStart().startsWith('{') ? JSON.parse(content) : parseConfig(content);
}

export interface RepositoryControlOptions {
  engineRoot?: string; pythonCommand?: string; repositoryRoots?: string[]; dataRoot?: string;
  repositoryTimeoutMs?: number;
}

export function repositoryControl(filename: string, operation: string, fields: Record<string, any> = {},
                                  options: RepositoryControlOptions = {}): any {
  const failure = (code: string, message: string, status = 503) => Object.assign(new Error(message), {code, status});
  const engineRoot = path.resolve(options.engineRoot || process.env.TRACEFIX_ENGINE_ROOT || process.cwd());
  const localPython = path.join(engineRoot, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const python = options.pythonCommand || process.env.TRACEFIX_PYTHON ||
    (fs.existsSync(localPython) ? localPython : process.platform === 'win32' ? 'py' : 'python3');
  const env = {...process.env, PYTHONIOENCODING: 'utf-8',
    PYTHONPATH: [path.join(engineRoot, 'backend/packages/agent/src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    ...(options.dataRoot ? {TRACEFIX_DATA: path.resolve(options.dataRoot)} : {}),
    ...(options.repositoryRoots ? {TRACEFIX_REPOSITORY_ROOTS: JSON.stringify(options.repositoryRoots.map(root => path.resolve(root)))} : {})};
  const defaultTimeout = ['repository.register', 'repository.update', 'project.create'].includes(operation) ? 1200000 : 15000;
  const result = spawnSync(python, [...(python === 'py' ? ['-3.12'] : []), '-m', 'tracefix.repository_control'],
    {cwd: engineRoot, env, input: JSON.stringify({operation, projects_path: path.resolve(filename), fields}),
      encoding: 'utf8', windowsHide: true, timeout: options.repositoryTimeoutMs || defaultTimeout, maxBuffer: 1048576});
  if (result.error) throw failure(result.error.code === 'ETIMEDOUT' ? 'repository_control_timeout' : 'repository_control_unavailable',
    result.error.code === 'ETIMEDOUT' ? '仓库控制服务超时' : '仓库控制服务不可用');
  let response;
  try {response = JSON.parse(result.stdout);}
  catch {throw failure('repository_control_protocol', '仓库控制服务响应无效');}
  if (!response || typeof response !== 'object' || typeof response.ok !== 'boolean')
    throw failure('repository_control_protocol', '仓库控制服务响应无效');
  if (!response.ok) {
    const error = response.error;
    if (!error || typeof error.code !== 'string' || typeof error.message !== 'string' ||
        ![400, 401, 403, 404, 409, 413, 503].includes(error.status))
      throw failure('repository_control_protocol', '仓库控制服务响应无效');
    throw failure(error.code, error.message, error.status);
  }
  if (result.status !== 0 || !Object.hasOwn(response, 'data'))
    throw failure('repository_control_protocol', '仓库控制服务响应无效');
  return response.data;
}

export function createProject(filename: string, id: string, root: string, repoId: string): Record<string, any> {
  return repositoryControl(filename, 'project.create', {id, root, repoId});
}

export function configureProject(filename: string, id: string, fields: Record<string, any>): Record<string, any> {
  return repositoryControl(filename, 'project.configure', {id, fields});
}

export function projectCatalog(projectsPath: string, dataRoot: string, displayRoot = process.cwd()): Record<string, any>[] {
  const registry = path.resolve(projectsPath);
  const projects = loadConfig(registry).projects;
  if (!Array.isArray(projects)) throw new Error('项目注册表缺少 projects');
  const profiles = new Map<string, {filename: string; profile: Record<string, any>}>();
  const errors = new Map<string, string>();
  for (const filename of fs.readdirSync(path.dirname(registry)).filter(name => name.endsWith('.yaml') && !name.includes('.example.')).sort()) {
    const fullPath = path.join(path.dirname(registry), filename);
    if (fullPath === registry) continue;
    let declaredProject = path.parse(filename).name;
    try {
      const profile = loadConfig(fullPath);
      if (typeof profile.project === 'string') declaredProject = profile.project;
      if (typeof profile.project !== 'string' || typeof profile.source_commit !== 'string' || !profile.source_commit ||
          !profile.commands || typeof profile.commands !== 'object' || Array.isArray(profile.commands)) throw new Error('Profile 无效');
      if (['start', 'reset', 'static', 'unit', 'build'].some(name => !profile.commands[name]) ||
          Object.values(profile.commands).some(command => !Array.isArray(command) || !command.length ||
            command.some(argument => typeof argument !== 'string' || !argument))) throw new Error('缺少有效的已批准命令');
      const origins = profile.allowed_origins ?? ['http://app:3000'];
      if (!Array.isArray(origins) || origins.some(origin => {
        if (typeof origin !== 'string') return true;
        const parsed = new URL(origin);
        return !['http:', 'https:'].includes(parsed.protocol) || Boolean(parsed.username) || !['', '/'].includes(parsed.pathname);
      })) throw new Error('origin 必须是精确的 http(s) 来源');
      profile.url ||= 'http://app:3000';
      if (!profiles.has(profile.project)) profiles.set(profile.project, {filename: fullPath, profile});
    } catch (error) { errors.set(declaredProject, String(error)); }
  }
  let remote: Record<string, any> = {};
  const remotePath = path.join(dataRoot, 'remote-config.json');
  if (fs.existsSync(remotePath)) remote = JSON.parse(fs.readFileSync(remotePath, 'utf8')).projects || {};
  return projects.map((project: Record<string, any>) => {
    const root = path.resolve(path.dirname(registry), project.root);
    const repository = project.repository_config;
    const entry = repository?.version === 1 && repository.profile ?
      {filename: repository.profile_ref || null, profile: repository.profile} : profiles.get(project.id);
    const enabled = repository?.enabled !== false && project.enabled !== false;
    let issue: string | null = null;
    if (!enabled) issue = '仓库已禁用';
    else if (repository?.checkout_mode !== 'managed' && (!fs.existsSync(root) || !fs.statSync(root).isDirectory())) issue = '目标目录不存在，请先初始化或 clone 项目';
    else if (repository?.checkout_mode !== 'managed' && !fs.existsSync(path.join(root, '.git'))) issue = '目标目录尚未初始化 Git';
    else if (!entry) issue = '缺少有效的项目 Profile' + (errors.has(project.id) ? ': ' + errors.get(project.id) : '');
    return {
      id: project.id, repoId: project.repo_id, root: path.relative(displayRoot, root) || '.',
      profile: entry?.filename ? path.relative(displayRoot, path.resolve(path.dirname(registry), entry.filename)) : null,
      registry, url: entry?.profile.url || null, enabled, repositoryConfigVersion: repository?.version || null,
      checkoutMode: repository?.checkout_mode || 'existing',
      cloneUrl: repository ? `https://github.com/${repository.owner}/${repository.repo}.git` : null,
      defaultRef: repository?.default_ref || null, publishBranch: repository?.publish_branch === true,
      requireApproval: repository?.require_approval === true,
      allowedFiles: project.allowed_files || [], commands: entry?.profile.commands || {}, remoteConfig: remote[project.id] || null,
      ready: issue === null, issue,
    };
  });
}
