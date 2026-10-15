import {readFileSync} from 'node:fs';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const bundled = await build({stdin: {contents: "export * from './src/task-lifecycle.ts'; export * from './src/database.ts';", resolveDir: root},
  bundle: true, platform: 'node', format: 'esm', write: false});
const {TaskLifecycle, ConsoleDatabase} = await import('data:text/javascript;base64,' + Buffer.from(bundled.outputFiles[0].text).toString('base64'));
const request = JSON.parse(readFileSync(0, 'utf8'));
const store = new ConsoleDatabase(request.databasePath);
const tasks = new TaskLifecycle(store, fields => fields, (fields, id) =>
  ({id, projectId: fields.projectId, status: 'running', startedAt: new Date().toISOString(), outcome: null, executor: 'explicit-test-double'}));
try {
  const started = tasks.start(request.taskId, {}, request.key, 'process-' + process.pid);
  if (request.phase === 'launching' && !started.replayed) tasks.launchIntent(request.taskId, started.run.id, 'process-' + process.pid);
  process.stdout.write(JSON.stringify(started));
} catch (error) {
  process.stdout.write(JSON.stringify({error: {code: error.code, status: error.status}}));
} finally {store.db.close();}
