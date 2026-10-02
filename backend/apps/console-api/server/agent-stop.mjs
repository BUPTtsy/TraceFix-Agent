import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const executeFile = promisify(execFile);
const activeStatuses = new Set(['running', 'paused', 'waiting_input', 'waiting_approval', 'stopping']);
export const stopUnavailableReason = '当前 API 无法停止此任务；请在启动该 Run 的终端停止';

export function childRunning(child) {
  return Boolean(child?.pid && child.exitCode == null && child.signalCode == null);
}

export function stopCapabilities(agent, child, isAlive) {
  const running = childRunning(child) || Boolean(agent.pid && isAlive(agent.pid));
  const canStop = running && activeStatuses.has(agent.status) && agent.status !== 'stopping' &&
    Boolean(childRunning(child) || agent.processControlId);
  const reason = !running || canStop ? undefined : agent.status === 'stopping' ? '正在停止，请等待进程退出' :
    !activeStatuses.has(agent.status) ? '任务已结束，等待进程退出' : stopUnavailableReason;
  return {running, canStop, stopUnavailableReason: reason};
}

export async function terminateChildTree(child, {platform = process.platform, execute = executeFile,
  isAlive = pid => {try {process.kill(pid, 0); return true;} catch {return false;}}, kill = process.kill} = {}) {
  if (!childRunning(child) || !isAlive(child.pid)) return;
  if (platform === 'win32') {
    try {await execute('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {windowsHide: true, timeout: 10000});}
    catch (error) {if (isAlive(child.pid)) throw error;}
  } else {
    try {kill(-child.pid, 'SIGKILL');}
    catch (error) {if (error.code !== 'ESRCH' && isAlive(child.pid)) throw error;}
  }
}

export function createAgentStopper({bridge, view, ownedProcess, isAlive, terminate = terminateChildTree,
  graceMs = 15000, reportError = console.error}) {
  let pending = null;
  const timers = new Map();

  async function stop(current) {
    if (!current.running || !activeStatuses.has(current.status)) return {status: 200, agent: current};
    if (current.status === 'stopping') return {status: 202, agent: current};
    if (!current.canStop) throw Object.assign(new Error(stopUnavailableReason), {status: 409});
    const owned = ownedProcess();
    const child = owned.id === current.id ? owned.child : null;
    const record = await bridge('run.stop.request', {id: current.id, processControlId: current.processControlId});
    if (record.status !== 'stopping') return {status: 200, agent: await view()};
    const fail = error => bridge('run.stop.failed', {id: record.id, processControlId: record.processControlId,
      stopRequestedAt: record.stopRequestedAt, error: `停止 Agent 失败：${error.message}`});
    if (childRunning(child) && ownedProcess().child === child) {
      if (record.processControlId) {
        const timer = setTimeout(async () => {
          timers.delete(child);
          if (!childRunning(child) || ownedProcess().child !== child) return;
          try {
            const latest = await bridge('run', {id: record.id});
            if (latest.status !== 'stopping' || latest.stopRequestedAt !== record.stopRequestedAt ||
                latest.processControlId !== record.processControlId) return;
            await terminate(child, {isAlive});
          } catch (error) {
            try {await fail(error);} catch (failure) {reportError('记录停止失败结果失败：', failure);}
            reportError('强制停止 Agent 失败：', error);
          }
        }, graceMs);
        timer.unref?.();
        timers.set(child, timer);
        child.once('close', () => {clearTimeout(timer); timers.delete(child);});
      } else {
        try {await terminate(child, {isAlive});}
        catch (error) {await fail(error); throw error;}
      }
    }
    return {status: 202, agent: await view()};
  }

  return {
    async stop() {
      if (pending) return pending;
      const request = (async () => stop(await view()))();
      pending = request;
      try {return await request;}
      finally {if (pending === request) pending = null;}
    },
    close() {for (const timer of timers.values()) clearTimeout(timer); timers.clear();},
  };
}
