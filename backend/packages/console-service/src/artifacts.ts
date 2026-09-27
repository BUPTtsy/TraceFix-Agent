import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {DataError} from './database.js';

type ArtifactIndex = Record<string, {'用途': string; SHA256: string; '字节数': number}>;

function directory(root: string, projectId: string, runId: string): string {
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(projectId) || !/^[A-Za-z0-9_-]{1,80}$/.test(runId))
    throw new DataError('证据归属无效');
  const base = path.join(root, projectId, runId);
  if (fs.existsSync(base)) {
    const expected = path.join(fs.realpathSync(root), projectId, runId);
    if (fs.realpathSync(base) !== expected) throw new DataError('证据路径通过符号链接逃逸');
  }
  return base;
}

export function artifactIndex(root: string, projectId: string, runId: string): ArtifactIndex {
  const filename = path.join(directory(root, projectId, runId), '文件索引.json');
  if (!fs.existsSync(filename)) return {};
  if (fs.lstatSync(filename).isSymbolicLink()) throw new DataError('文件索引不能是符号链接');
  return JSON.parse(fs.readFileSync(filename, 'utf8'));
}

export function readArtifact(root: string, projectId: string, runId: string, ref: string): string {
  if (!/^(?:[a-f0-9]{64}|[0-9]{4,}_[A-Za-z0-9_\-\u4e00-\u9fff]{1,100})\.(json|txt|png|html|diff)$/.test(ref))
    throw new DataError('证据引用无效');
  const base = directory(root, projectId, runId);
  const filename = path.join(base, ref);
  if (fs.lstatSync(filename).isSymbolicLink()) throw new DataError('证据路径不能是符号链接');
  const raw = fs.readFileSync(filename);
  const expected = /^[a-f0-9]{64}\./.test(ref) ? ref.split('.')[0] : artifactIndex(root, projectId, runId)[ref]?.SHA256;
  if (createHash('sha256').update(raw).digest('hex') !== expected) throw new DataError('证据完整性校验失败');
  return raw.toString('utf8');
}
