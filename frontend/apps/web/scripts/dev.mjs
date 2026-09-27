import {spawn} from 'node:child_process';

const childEnv = {...process.env, NODE_ENV: 'development'};
console.log('正在启动 BugBoard 服务与前端开发服务。');
const app = spawn(process.execPath, ['--watch', '../../../backend/apps/console-api/server/index.mjs'], {stdio: 'inherit', env: childEnv});
const vite = spawn(process.execPath, ['../../../node_modules/vite/bin/vite.js'], {stdio: 'inherit', env: childEnv});
const shutdown = signal => {app.kill(signal); vite.kill(signal);};
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => shutdown(signal));
app.on('exit', (code, signal) => {if (!signal && code && !vite.killed) vite.kill();});
vite.on('exit', (code, signal) => {if (!signal && code && !app.killed) app.kill();});
