import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {build} from 'esbuild';

const repo = fileURLToPath(new URL('../../../../', import.meta.url));
const directory = fs.mkdtempSync(path.join(repo, '.tmp-check-report-ui-'));
const output = path.join(directory, 'check-reports.mjs');
await build({entryPoints: [path.join(repo, 'frontend/packages/runs/src/RunsPage.tsx')], bundle: true,
  platform: 'node', format: 'esm', jsx: 'automatic', external: ['react', 'react/jsx-runtime'], outfile: output});
const {CheckReportPanel} = await import(pathToFileURL(output).href);
process.on('exit', () => fs.rmSync(directory, {recursive: true, force: true}));
const item = (id, name, source = 'rule') => ({id, name, criteria: name + '必须符合配置指标', source, severity: 'blocker', detector: 'guided'});
const result = (check, status = 'pass', fields = {}) => ({...check, status, actual: '真实检测结果', evidence_refs: [], ...fields});
const render = fields => renderToStaticMarkup(createElement(CheckReportPanel, {run: {id: 'console_current', artifacts: [], ...fields}}));

test('逐项报告完整展示规则、用户目标、未执行项和可信图片证据', () => {
  const rule = item('rule_1', '输入标签'), goal = item('goal_1', '保存反馈', 'user_goal'), missing = item('rule_2', '按钮可达');
  const markup = render({check_plan: [rule, goal, missing], overall_status: 'FAILED',
    check_results: [result(rule, 'fail'), result(goal, 'inconclusive', {evidence_refs: ['0001_observation.json'],
      fallback: {reason: 'AST 解析失败', status: 'inconclusive'}})],
    check_summary: {total: 3, passed: 0, failed: 1, error: 0, inconclusive: 1, blocker_failed: 2, missing_ids: ['rule_2']},
    artifacts: [{ref: '0001_observation.json'}, {ref: '0002_image.png'}],
    images: [{ref: '0002_image.png', mime: 'image/png', alt: '保存反馈截图'}, {ref: '0003_private.png', mime: 'image/png', alt: '禁止发布'}]});
  for (const text of ['输入标签', '保存反馈', '按钮可达', '配置规则', '用户目标', '检测内容 / 指标', '未执行', '阻断检查失败', '已执行 2 / 3', 'AST 解析失败']) assert.ok(markup.includes(text), text);
  assert.match(markup, /href="\/api\/runs\/console_current\/artifacts\/0001_observation.json"/);
  assert.match(markup, /src="\/api\/runs\/console_current\/artifacts\/0002_image.png"/);
  assert.match(markup, /alt="保存反馈截图"/);
  assert.doesNotMatch(markup, /0003_private.png|undefined/);
});

test('repair 报告保留修复前失败和修复后检测结果', () => {
  const check = item('goal', '目标检查', 'user_goal');
  const markup = render({check_plan: [check], check_results: [result(check, 'pass', {stage: 'verify', actual: '修复后反馈已显示'})],
    initial_check_results: [result(check, 'fail', {stage: 'explore', actual: '修复前缺少反馈'})], overall_status: 'PASSED'});
  for (const text of ['修复前检查结果', '修复后检查结果', '修复前缺少反馈', '修复后反馈已显示', '全部通过']) assert.ok(markup.includes(text), text);
});

test('兼容历史 issueReport 中的检查结果并处理尚无报告的 Run', () => {
  const check = item('legacy', '已有报告');
  assert.ok(render({issueReport: {check_plan: [check], check_results: [result(check)]}}).includes('已有报告'));
  assert.equal(render({}), '');
});
