import fs from 'node:fs';
import path from 'node:path';

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
      if (separator > 0 && !content.startsWith('"') && !content.startsWith("'")) {
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
  const content = fs.readFileSync(filename, 'utf8');
  return content.trimStart().startsWith('{') ? JSON.parse(content) : parseConfig(content);
}

export function createProject(filename: string, id: string, root: string, repoId: string): Record<string, any> {
  if (!/^[A-Za-z0-9_-]{1,64}$/.test(id)) throw new Error('项目 ID 无效');
  const registry = fs.existsSync(filename) ? loadConfig(filename) : {projects: []};
  if (!Array.isArray(registry.projects) || registry.projects.some((project: Record<string, any>) => project.id === id))
    throw new Error('项目 ID 已存在或注册表无效');
  const item = {id, repo_id: repoId, root, allowed_files: ['src/**', 'server/**'],
    memory_revision: 1, access_epoch: 1, agent_instructions: 'AGENTS.md'};
  const target = path.resolve(path.dirname(filename), root);
  registry.projects.push(item);
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  fs.writeFileSync(filename, JSON.stringify(registry, null, 2) + '\n');
  fs.mkdirSync(target, {recursive: true});
  return {...item, root: target};
}

export function configureProject(filename: string, id: string, fields: Record<string, any>): Record<string, any> {
  if (!(loadConfig(filename).projects || []).some((project: Record<string, any>) => project.id === id))
    throw new Error('未注册的项目');
  const commands = fields.commands;
  if (['start', 'reset', 'static', 'unit', 'build'].some(name => !Array.isArray(commands?.[name]) || !commands[name].length))
    throw new Error('项目命令不能为空');
  const url = new URL(fields.url || 'http://app:3000');
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !['', '/'].includes(url.pathname))
    throw new Error('项目 URL 无效');
  const profile = {project: id, source_commit: fields.sourceCommit || 'HEAD', url: url.origin,
    allowed_origins: [url.origin], commands};
  const profilePath = path.join(path.dirname(filename), `${id}.yaml`);
  fs.writeFileSync(profilePath, JSON.stringify(profile, null, 2) + '\n');
  return {...profile, profilePath};
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
    try {
      const profile = loadConfig(fullPath);
      if (typeof profile.project !== 'string' || !profile.commands) throw new Error('Profile 无效');
      profile.url ||= 'http://app:3000';
      if (!profiles.has(profile.project)) profiles.set(profile.project, {filename: fullPath, profile});
    } catch (error) { errors.set(path.parse(filename).name, String(error)); }
  }
  let remote: Record<string, any> = {};
  const remotePath = path.join(dataRoot, 'remote-config.json');
  if (fs.existsSync(remotePath)) remote = JSON.parse(fs.readFileSync(remotePath, 'utf8')).projects || {};
  return projects.map((project: Record<string, any>) => {
    const root = path.resolve(path.dirname(registry), project.root);
    const entry = profiles.get(project.id);
    let issue: string | null = null;
    if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) issue = '目标目录不存在，请先初始化或 clone 项目';
    else if (!fs.existsSync(path.join(root, '.git'))) issue = '目标目录尚未初始化 Git';
    else if (!entry) issue = '缺少有效的项目 Profile' + (errors.has(project.id) ? ': ' + errors.get(project.id) : '');
    return {
      id: project.id, repoId: project.repo_id, root: path.relative(displayRoot, root) || '.',
      profile: entry ? path.relative(displayRoot, entry.filename) : null, registry, url: entry?.profile.url || null,
      allowedFiles: project.allowed_files || [], commands: entry?.profile.commands || {}, remoteConfig: remote[project.id] || null,
      ready: issue === null, issue,
    };
  });
}
