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
  if (fs.statSync(filename).size > 16 * 1024 * 1024) throw new DataError('文件索引超出边界');
  return JSON.parse(fs.readFileSync(filename, 'utf8'));
}

export function readArtifactBytes(root: string, projectId: string, runId: string, ref: string): Buffer {
  if (!/^(?:[a-f0-9]{64}|[0-9]{4,}_[A-Za-z0-9_\-\u4e00-\u9fff]{1,100})\.(json|txt|png|html|diff)$/.test(ref))
    throw new DataError('证据引用无效');
  const base = directory(root, projectId, runId);
  const filename = path.join(base, ref);
  if (fs.lstatSync(filename).isSymbolicLink()) throw new DataError('证据路径不能是符号链接');
  if (fs.statSync(filename).size > 16 * 1024 * 1024) throw new DataError('证据文件超出边界');
  const raw = fs.readFileSync(filename);
  const expected = /^[a-f0-9]{64}\./.test(ref) ? ref.split('.')[0] : artifactIndex(root, projectId, runId)[ref]?.SHA256;
  if (createHash('sha256').update(raw).digest('hex') !== expected) throw new DataError('证据完整性校验失败');
  return raw;
}

export function readArtifact(root: string, projectId: string, runId: string, ref: string): string {
  return readArtifactBytes(root, projectId, runId, ref).toString('utf8');
}
export function evidenceClosure(root: string, projectId: string, runId: string, reportRef: string, index: ArtifactIndex): Set<string> {
  const allowed = new Set<string>(), pending = [reportRef];
  if (!reportRef) return allowed;
  const report = JSON.parse(readArtifact(root, projectId, runId, reportRef));
  if (report.run_id !== runId || report.scope_id !== projectId) throw new DataError('报告归属不一致');
  const traceRef = Object.keys(index).reverse().find(ref => ref.endsWith('.json') && index[ref]['用途']?.endsWith('完整事件数据'));
  if (traceRef) {
    const events = JSON.parse(readArtifact(root, projectId, runId, traceRef));
    if (!Array.isArray(events) || events.length > 16384) throw new DataError('报告事件超出边界');
    const finished = events.find(event => event.type === 'run.finished' && event.run_id === runId &&
      event.scope_id === projectId && event.payload?.report_ref === reportRef);
    if (finished?.payload?.html_ref) pending.push(finished.payload.html_ref);
  }
  const edges = new Set(['evidence_refs', 'validation_refs', 'baseline_validation_refs', 'patch_diff_ref',
    'artifact_ref', 'observation_ref', 'screenshot_ref', 'checkpoint_refs', 'issue_report_ref',
    'check_plan_ref', 'check_result_refs', 'initial_check_result_refs']);
  const containers = new Set(['issues', 'issue_report', 'check_plan', 'check_results', 'items']);
  let bytes = 0;
  function collect(value: unknown, depth = 0): void {
    if (depth > 16) throw new DataError('证据嵌套超出边界');
    if (Array.isArray(value)) {for (const item of value) collect(item, depth + 1);}
    else if (value && typeof value === 'object') {
      for (const [key, item] of Object.entries(value)) {
        if (edges.has(key) && item !== null) {
          for (const ref of Array.isArray(item) ? item : [item]) {
            if (typeof ref !== 'string') throw new DataError('证据引用无效');
            pending.push(ref);
            if (pending.length > 512) throw new DataError('证据引用超出边界');
          }
        } else if (key === 'images') {
          if (!Array.isArray(item)) throw new DataError('图片证据格式无效');
          collect(item.map(image => {
            if (!image || typeof image !== 'object' || typeof image.ref !== 'string' ||
                !image.ref.endsWith('.png') || image.mime !== 'image/png') throw new DataError('图片证据引用无效');
            return {screenshot_ref: image.ref};
          }), depth + 1);
        } else if (containers.has(key)) collect(item, depth + 1);
      }
    }
  }
  while (pending.length) {
    const ref = pending.pop()!;
    if (allowed.has(ref)) continue;
    if (allowed.size >= 256) throw new DataError('证据闭包超出边界');
    const entry = index[ref];
    if (!entry || typeof entry['用途'] !== 'string' || /模型|推理|上下文|指令|记忆|源码|状态|事件/.test(entry['用途']))
      throw new DataError('不允许发布私密或未索引证据');
    const raw = readArtifactBytes(root, projectId, runId, ref);
    bytes += raw.length;
    if (bytes > 32 * 1024 * 1024 || entry['字节数'] !== raw.length) throw new DataError('证据大小超出边界');
    allowed.add(ref);
    if (ref.endsWith('.json')) {
      const value = JSON.parse(raw.toString('utf8'));
      if (value && typeof value === 'object' && (('run_id' in value && value.run_id !== runId) ||
          ('scope_id' in value && value.scope_id !== projectId))) throw new DataError('证据归属不一致');
      collect(value);
    }
  }
  return allowed;
}
