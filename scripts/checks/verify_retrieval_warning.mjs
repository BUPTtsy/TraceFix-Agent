import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {build} from 'esbuild';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const projectRoot = fileURLToPath(new URL('../../', import.meta.url));
const compiled = await build({
  entryPoints: [path.join(projectRoot, 'frontend/packages/runs/src/RunsPage.tsx')],
  bundle: true, write: false, format: 'esm', platform: 'node', jsx: 'automatic',
});
const {RetrievalWarning} = await import('data:text/javascript;base64,' + Buffer.from(compiled.outputFiles[0].text).toString('base64'));
const render = events => renderToStaticMarkup(createElement(RetrievalWarning, {events}));
const presentation = await build({
  entryPoints: [path.join(projectRoot, 'frontend/packages/presentation/src/presentation.ts')],
  bundle: true, write: false, format: 'esm', platform: 'node',
});
const {eventLabel} = await import('data:text/javascript;base64,' + Buffer.from(presentation.outputFiles[0].text).toString('base64'));

test('retrieval warning is hidden for runs without a degradation event', () => {
  assert.equal(render([]), '');
  // 历史运行记录里仍可能有已下线的 knowledge.selected 事件：不触发降级提示。
  assert.equal(render([{type: 'knowledge.selected', payload: {}}]), '');
});

test('historical knowledge.selected events fall back to the raw event type label', () => {
  assert.equal(eventLabel('knowledge.selected'), 'knowledge.selected');
  assert.equal(eventLabel('retrieval.degraded'), '修复记忆未启用');
});

test('retrieval degradation is visible once and escapes untrusted message text', () => {
  const warning = {type: 'retrieval.degraded', payload: {message: '修复记忆未启用 <script>alert(1)</script>'}};
  const html = render([warning, warning]);
  assert.equal((html.match(/role="status"/g) || []).length, 1);
  assert.ok(html.includes('修复记忆未启用'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
});

test('retrieval warning gives capability boundaries when an old event lacks a message', () => {
  const html = render([{type: 'retrieval.degraded', payload: {}}]);
  assert.ok(html.includes('修复记忆的读写未启用'));
  assert.ok(html.includes('未连接 PostgreSQL'));
  assert.ok(!html.includes('代码检索') && !html.includes('文档检索'));
});
