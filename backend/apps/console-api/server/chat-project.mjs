import fs from 'node:fs';
import path from 'node:path';

const MAX_LIST_ENTRIES = 200;
const MAX_READ_BYTES = 256 * 1024;
const MAX_READ_LINES = 4000;
const MAX_SEARCH_RESULTS = 100;
const MAX_SEARCH_BYTES = 8 * 1024 * 1024;
const MAX_SEARCH_FILES = 5000;
const MAX_SEARCH_ENTRIES = 10000;

export class ProjectToolError extends Error {
  constructor(message, status = 400, code = 'invalid_project_path') {
    super(message);
    this.name = 'ProjectToolError';
    this.status = status;
    this.code = code;
  }
}

function sensitiveSegment(segment) {
  const value = segment.toLowerCase();
  return ['.git', '.tracefix', '.aws', '.ssh', '.azure', '.gcloud', '.codex', '.agents',
    'node_modules', '.venv', 'venv', '__pycache__', '.npmrc', '.pypirc', '.netrc'].includes(value) ||
    value.startsWith('.env') || /(?:secret|credential|token|password|private[_-]?key)/i.test(value) ||
    /\.(?:pem|key|p12|pfx|sqlite|sqlite3|db)$/.test(value) ||
    ['id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'clock$'].includes(value) ||
    /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/.test(value);
}

function relativeParts(value, allowEmpty = true) {
  if (value === undefined || value === null || value === '') {
    if (allowEmpty) return [];
    throw new ProjectToolError('路径不能为空');
  }
  if (typeof value !== 'string' || value.length > 1024 || /[\x00-\x1f\x7f:]/.test(value))
    throw new ProjectToolError('路径无效');
  const normalized = value.replaceAll('\\', '/');
  if (path.posix.isAbsolute(normalized) || path.win32.isAbsolute(value) || normalized.startsWith('//'))
    throw new ProjectToolError('只允许使用项目根目录下的相对路径');
  const result = [];
  for (const segment of normalized.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..' || /[. ]$/.test(segment)) throw new ProjectToolError('路径不能包含上级目录或不规范名称');
    if (sensitiveSegment(segment)) throw new ProjectToolError('禁止读取敏感路径');
    result.push(segment);
  }
  return result;
}

function linkError(target) {
  return new ProjectToolError(`拒绝读取符号链接或 junction：${target}`, 403, 'link_not_allowed');
}

function assertDirectoryEntry(filename, displayPath) {
  let stat;
  try { stat = fs.lstatSync(filename); } catch (error) {
    if (error?.code === 'ENOENT') throw new ProjectToolError('文件不存在', 404, 'not_found');
    throw error;
  }
  if (stat.isSymbolicLink()) throw linkError(displayPath);
  return stat;
}

function ensureNoLinks(root, parts) {
  let current = root;
  assertDirectoryEntry(current, '.');
  for (const [index, part] of parts.entries()) {
    current = path.join(current, part);
    assertDirectoryEntry(current, parts.slice(0, index + 1).join('/'));
  }
  const relative = path.relative(root, fs.realpathSync(current));
  if (relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative))
    throw new ProjectToolError('路径不能离开项目根目录', 403);
  return current;
}

function textLimit(value, limit, label) {
  if (value === undefined) return undefined;
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0 || number > limit)
    throw new ProjectToolError(`${label}超出允许范围`);
  return number;
}

function safeRead(filename, displayPath, signal) {
  const stat = assertDirectoryEntry(filename, displayPath);
  if (!stat.isFile()) throw new ProjectToolError('目标不是普通文件', 400, 'not_file');
  if (stat.size > MAX_READ_BYTES) throw new ProjectToolError(`文件过大（最多 ${MAX_READ_BYTES} 字节）`, 413, 'output_limit');
  if (signal?.aborted) throw new ProjectToolError('客户端已断开请求', 499, 'aborted');
  const descriptor = fs.openSync(filename, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  try {
    const opened = fs.fstatSync(descriptor);
    if (!opened.isFile() || opened.size > MAX_READ_BYTES || opened.ino !== stat.ino || opened.dev !== stat.dev)
      throw new ProjectToolError('文件在读取前已改变', 409, 'file_changed');
    const content = Buffer.alloc(Math.min(opened.size, MAX_READ_BYTES));
    const read = fs.readSync(descriptor, content, 0, content.length, 0);
    const bytes = content.subarray(0, read);
    if (bytes.includes(0)) throw new ProjectToolError('只支持读取文本文件', 400, 'binary_file');
    try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
    catch { throw new ProjectToolError('只支持读取 UTF-8 文本文件', 400, 'binary_file'); }
  } finally {fs.closeSync(descriptor);}
}

function safeEntryName(name) {
  return name !== '.' && name !== '..' && !/[. ]$/.test(name) && !sensitiveSegment(name);
}

function walk(root, start, output, maxDepth, maxEntries, signal, depth = 0) {
  if (signal?.aborted) throw new ProjectToolError('客户端已断开请求', 499, 'aborted');
  if (output.length >= maxEntries) return;
  let entries;
  try { entries = fs.readdirSync(start, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name)); }
  catch { return; }
  for (const entry of entries) {
    if (output.length >= maxEntries || !safeEntryName(entry.name)) continue;
    const filename = path.join(start, entry.name);
    const display = path.relative(root, filename).replaceAll(path.sep, '/') || entry.name;
    let stat;
    try { stat = assertDirectoryEntry(filename, display); } catch (error) {
      if (error instanceof ProjectToolError && error.code === 'link_not_allowed') continue;
      continue;
    }
    const directory = stat.isDirectory();
    output.push({path: display, type: directory ? 'directory' : 'file', ...(directory ? {} : {bytes: stat.size})});
    if (directory && depth < maxDepth) walk(root, filename, output, maxDepth, maxEntries, signal, depth + 1);
  }
}

function searchFiles(root, start, query, results, state, maxResults, signal, depth = 0) {
  if (signal?.aborted) throw new ProjectToolError('客户端已断开请求', 499, 'aborted');
  if (results.length >= maxResults || state.bytes >= MAX_SEARCH_BYTES || state.files >= MAX_SEARCH_FILES || state.entries >= MAX_SEARCH_ENTRIES) return;
  let entries;
  try { entries = fs.readdirSync(start, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name)); }
  catch { return; }
  for (const entry of entries) {
    if (results.length >= maxResults || state.bytes >= MAX_SEARCH_BYTES || state.files >= MAX_SEARCH_FILES || state.entries >= MAX_SEARCH_ENTRIES || !safeEntryName(entry.name)) continue;
    state.entries++;
    const filename = path.join(start, entry.name);
    const display = path.relative(root, filename).replaceAll(path.sep, '/');
    let stat;
    try { stat = assertDirectoryEntry(filename, display); } catch { continue; }
    if (stat.isDirectory()) {
      if (depth < 12) searchFiles(root, filename, query, results, state, maxResults, signal, depth + 1);
      continue;
    }
    if (!stat.isFile() || stat.size > MAX_READ_BYTES || state.bytes + stat.size > MAX_SEARCH_BYTES) continue;
    state.files++;
    let content;
    try { content = safeRead(filename, display, signal); } catch { continue; }
    state.bytes += stat.size;
    const lines = content.split(/\r?\n/);
    for (let index = 0; index < lines.length && results.length < maxResults; index++) {
      if (lines[index].toLocaleLowerCase().includes(query)) results.push({path: display, line: index + 1, text: lines[index].slice(0, 2000)});
    }
  }
}

export function createProjectReader({root, signal} = {}) {
  if (typeof root !== 'string' || !path.isAbsolute(root)) throw new ProjectToolError('项目根目录无效', 500, 'invalid_project');
  const absoluteRoot = path.resolve(root);
  const parsedRoot = path.parse(root).root;
  let cursor = parsedRoot;
  for (const segment of path.relative(parsedRoot, absoluteRoot).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, segment);
    assertDirectoryEntry(cursor, '.');
  }
  const canonicalRoot = fs.realpathSync(absoluteRoot);
  const rootStat = assertDirectoryEntry(canonicalRoot, '.');
  if (!rootStat.isDirectory()) throw new ProjectToolError('项目根目录不是目录', 500, 'invalid_project');
  const checkAbort = () => { if (signal?.aborted) throw new ProjectToolError('客户端已断开请求', 499, 'aborted'); };
  const resolve = (raw) => { checkAbort(); const parts = relativeParts(raw); return ensureNoLinks(canonicalRoot, parts); };
  const listFiles = (args = {}) => {
    const start = resolve(args.path);
    const stat = assertDirectoryEntry(start, args.path || '.');
    if (!stat.isDirectory()) throw new ProjectToolError('list_files 的路径必须是目录', 400, 'not_directory');
    const maxDepth = textLimit(args.max_depth, 8, 'max_depth') ?? 4;
    const maxEntries = textLimit(args.max_entries, MAX_LIST_ENTRIES, 'max_entries') ?? MAX_LIST_ENTRIES;
    if (maxEntries < 1) throw new ProjectToolError('max_entries 必须为正数');
    const entries = [];
    walk(canonicalRoot, start, entries, maxDepth, maxEntries, signal);
    return {path: args.path || '.', entries, truncated: entries.length >= maxEntries};
  };
  const readFile = (args = {}) => {
    const filename = resolve(args.path);
    const content = safeRead(filename, args.path, signal);
    const startLine = textLimit(args.start_line, MAX_READ_LINES, 'start_line') ?? 1;
    const maxLines = textLimit(args.max_lines, MAX_READ_LINES, 'max_lines') ?? 400;
    if (startLine < 1 || maxLines < 1) throw new ProjectToolError('行号必须为正数');
    const lines = content.split(/\r?\n/);
    const selected = lines.slice(startLine - 1, startLine - 1 + maxLines);
    return {path: args.path, start_line: startLine, end_line: startLine + selected.length - 1, content: selected.join('\n'), truncated: startLine - 1 + selected.length < lines.length};
  };
  const search = (args = {}) => {
    if (typeof args.query !== 'string' || !args.query.trim() || args.query.length > 500) throw new ProjectToolError('搜索关键词无效');
    const start = resolve(args.path);
    const stat = assertDirectoryEntry(start, args.path || '.');
    if (!stat.isDirectory()) throw new ProjectToolError('search 的路径必须是目录', 400, 'not_directory');
    const maxResults = Math.min(textLimit(args.max_results, MAX_SEARCH_RESULTS, 'max_results') ?? 50, MAX_SEARCH_RESULTS);
    const results = [], state = {bytes: 0, files: 0, entries: 0};
    if (maxResults < 1) throw new ProjectToolError('max_results 必须为正数');
    searchFiles(canonicalRoot, start, args.query.toLocaleLowerCase(), results, state, maxResults, signal);
    return {query: args.query, results, truncated: results.length >= maxResults || state.bytes >= MAX_SEARCH_BYTES || state.files >= MAX_SEARCH_FILES || state.entries >= MAX_SEARCH_ENTRIES};
  };
  const definitions = [
    {type: 'function', function: {name: 'project.list_files', description: '列出项目中的安全文件和目录（只读）', parameters: {type: 'object', properties: {path: {type: 'string'}, max_depth: {type: 'integer'}, max_entries: {type: 'integer'}}, additionalProperties: false}}},
    {type: 'function', function: {name: 'project.read_file', description: '读取项目中的文本文件（只读）', parameters: {type: 'object', required: ['path'], properties: {path: {type: 'string'}, start_line: {type: 'integer'}, max_lines: {type: 'integer'}}, additionalProperties: false}}},
    {type: 'function', function: {name: 'project.search', description: '在项目安全文件中搜索文本（只读）', parameters: {type: 'object', required: ['query'], properties: {query: {type: 'string'}, path: {type: 'string'}, max_results: {type: 'integer'}}, additionalProperties: false}}},
  ];
  const execute = (name, args = {}) => {
    checkAbort();
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new ProjectToolError('工具参数必须是对象');
    const accepted = {'project.list_files': ['path', 'max_depth', 'max_entries'], 'project.read_file': ['path', 'start_line', 'max_lines'], 'project.search': ['query', 'path', 'max_results']};
    if (!Object.hasOwn(accepted, name)) throw new ProjectToolError('未知或不允许的项目工具', 400, 'unknown_tool');
    if (Object.keys(args).some(key => !accepted[name].includes(key))) throw new ProjectToolError('工具包含不允许的参数');
    if (name === 'project.list_files') return listFiles(args);
    if (name === 'project.read_file') return readFile(args);
    if (name === 'project.search') return search(args);
    throw new ProjectToolError('未知或不允许的项目工具', 400, 'unknown_tool');
  };
  return {root: canonicalRoot, tools: definitions, definitions, execute};
}
