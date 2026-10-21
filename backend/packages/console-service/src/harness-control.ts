import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
import type {RepositoryControlOptions} from './config.js';

export type HarnessControlOperation = 'view' | 'stop' | 'resume.check';
export interface HarnessControlOptions extends RepositoryControlOptions {
  databasePath?: string;
  harnessControl?: (operation: HarnessControlOperation, fields: Record<string, any>) => Record<string, any>;
}

export function harnessControl(operation: HarnessControlOperation, fields: Record<string, any>,
                               options: HarnessControlOptions = {}): Record<string, any> {
  if (options.harnessControl) return options.harnessControl(operation, fields);
  const failure = (code: string, message: string, status = 503) => Object.assign(new Error(message), {code, status});
  const root = path.resolve(options.engineRoot || process.env.TRACEFIX_ENGINE_ROOT || process.cwd());
  const local = path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
  const python = options.pythonCommand || process.env.TRACEFIX_PYTHON ||
    (fs.existsSync(local) ? local : process.platform === 'win32' ? 'py' : 'python3');
  const result = spawnSync(python, [...(python === 'py' ? ['-3.12'] : []), '-m', 'tracefix.harness_control'], {
    cwd: root, windowsHide: true, encoding: 'utf8', timeout: options.repositoryTimeoutMs || 15000, maxBuffer: 1048576,
    input: JSON.stringify({operation, fields}), env: {...process.env, PYTHONIOENCODING: 'utf-8',
      PYTHONPATH: [path.join(root, 'backend/packages/agent/src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      ...(options.dataRoot ? {TRACEFIX_DATA: path.resolve(options.dataRoot)} : {}),
      ...(options.databasePath ? {TRACEFIX_CONSOLE_DB: path.resolve(options.databasePath)} : {})},
  });
  if (result.error) throw failure(result.error.code === 'ETIMEDOUT' ? 'HARNESS_CONTROL_TIMEOUT' : 'HARNESS_CONTROL_UNAVAILABLE',
    result.error.code === 'ETIMEDOUT' ? 'SDK 控制服务超时' : 'SDK 控制服务不可用');
  let response;
  try {response = JSON.parse(result.stdout);} catch {throw failure('HARNESS_CONTROL_PROTOCOL', 'SDK 控制服务响应无效');}
  if (!response || typeof response.ok !== 'boolean') throw failure('HARNESS_CONTROL_PROTOCOL', 'SDK 控制服务响应无效');
  if (!response.ok) {
    const error = response.error;
    if (!error || typeof error.code !== 'string' || typeof error.message !== 'string' ||
        ![400, 401, 403, 404, 409, 413, 503].includes(error.status))
      throw failure('HARNESS_CONTROL_PROTOCOL', 'SDK 控制服务响应无效');
    throw failure(error.code, error.message, error.status);
  }
  const data = response.data;
  if (result.status !== 0 || !data || data.coding_backend !== 'claude_sdk' || typeof data.control_status !== 'string' ||
      typeof data.can_resume !== 'boolean' || typeof data.can_stop !== 'boolean' ||
      !Array.isArray(data.resume_blockers) || data.resume_blockers.some((entry: any) => typeof entry !== 'string'))
    throw failure('HARNESS_CONTROL_PROTOCOL', 'SDK 控制服务响应无效');
  return data;
}
