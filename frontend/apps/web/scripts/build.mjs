import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
const require = createRequire(import.meta.url);
console.log('正在构建 BugBoard 前端。');
const esbuild = require('esbuild');
await fs.mkdir('dist', {recursive:true});
await esbuild.build({entryPoints:['src/main.tsx'],bundle:true,outfile:'dist/app.js',format:'esm',jsx:'automatic',nodePaths:[process.env.NODE_PATH || 'node_modules'],minify:true});
await fs.writeFile('dist/index.html','<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>TraceFix · Agent 工作台</title><link rel="stylesheet" href="/app.css"><div id="root"></div><script type="module" src="/app.js"></script></html>');
