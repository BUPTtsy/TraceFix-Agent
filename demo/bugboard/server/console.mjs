import {spawn} from 'node:child_process';
import path from 'node:path';
import {errorWithContext} from './errors.mjs';

export function consoleBridge(projectRoot, pythonCommand) {
  let writes = Promise.resolve();
  function invoke(operation, fields = {}) {
    return new Promise((resolve, reject) => {
      const python = pythonCommand();
      const child = spawn(python, [...(python === 'py' ? ['-3.12'] : []), '-m', 'tracefix.console'], {
        cwd: projectRoot, windowsHide: true,
        env: {...process.env, PYTHONIOENCODING: 'utf-8', PYTHONPATH: [path.join(projectRoot, 'src'), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)},
      });
      let output = '', errors = '';
      const timeout = setTimeout(() => {child.kill(); reject(new Error('控制台数据服务超时'));}, 20000);
      child.stdout.on('data', chunk => {output += chunk;});
      child.stderr.on('data', chunk => {errors = (errors + chunk).slice(-2000);});
      child.on('error', error => {clearTimeout(timeout); reject(errorWithContext('控制台数据服务启动失败', error));});
      child.stdin.on('error', error => {clearTimeout(timeout); reject(errorWithContext('向控制台数据服务写入请求失败', error));});
      child.on('close', code => {
        clearTimeout(timeout);
        try {
          if (code) throw new Error(`控制台 Python 服务退出（退出码 ${code}）：${errors}`);
          let value;
          try {value = JSON.parse(output);}
          catch (error) {throw errorWithContext('控制台数据服务响应 JSON 解析失败', error);}
          if (value.error) throw Object.assign(new Error(value.error), {status: value.status});
          resolve(value.result);
        } catch (error) {reject(error);}
      });
      child.stdin.end(JSON.stringify({operation, fields}));
    });
  }
  return (operation, fields = {}) => {
    if (!['document.save', 'run.create', 'run.continue', 'run.update', 'run.ended'].includes(operation)) return invoke(operation, fields);
    const pending = writes.then(() => invoke(operation, fields));
    writes = pending.catch(() => {});
    return pending;
  };
}
