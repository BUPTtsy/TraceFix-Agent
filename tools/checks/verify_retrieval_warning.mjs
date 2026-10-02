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

test('retrieval warning is hidden for runs without a degradation event', () => {
  assert.equal(render([]), '');
  assert.equal(render([{type: 'knowledge.selected', payload: {}}]), '');
});

test('retrieval degradation is visible once and escapes untrusted message text', () => {
  const warning = {type: 'retrieval.degraded', payload: {message: '代码检索未启用 <script>alert(1)</script>'}};
  const html = render([warning, warning]);
  assert.equal((html.match(/role="status"/g) || []).length, 1);
  assert.ok(html.includes('代码检索未启用'));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('<script>'));
});

test('retrieval warning gives capability boundaries when an old event lacks a message', () => {
  const html = render([{type: 'retrieval.degraded', payload: {}}]);
  assert.ok(html.includes('未连接 PostgreSQL'));
  assert.ok(html.includes('项目文档检索仍可用'));
});
