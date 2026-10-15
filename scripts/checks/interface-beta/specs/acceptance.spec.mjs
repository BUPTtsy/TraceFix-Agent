import {test as base, expect} from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import {compileCases, serviceUrl} from '../contract.mjs';

const input = JSON.parse(fs.readFileSync(process.env.TRACEFIX_ACCEPTANCE_REQUEST, 'utf8'));
const cases = compileCases(input);
const output = process.env.TRACEFIX_ACCEPTANCE_OUTPUT;
const test = base.extend({
  acceptancePage: async ({page}, use) => {
    await page.route('**/*', async route => {
      const url = route.request().url();
      if (/^https?:/.test(url) && serviceUrl(url).origin !== serviceUrl(input.url).origin)
        return route.abort('blockedbyclient');
      return route.continue();
    });
    if (input.reset_path) {
      const reset = await page.request.post(new URL(input.reset_path, input.url).href,
        {maxRedirects: 0, timeout: 5000});
      if (!reset.ok()) throw new Error(`隔离场景 reset 失败: ${reset.status()}`);
    }
    await page.goto(input.url, {waitUntil: 'domcontentloaded'});
    await use(page);
  },
});

async function check(page, assertion, timeout = 4000) {
  const target = page.getByRole(assertion.locator.role, {name: assertion.locator.name, exact: true});
  if (assertion.condition === 'absent') return expect(target).toHaveCount(0, {timeout});
  await expect(target).toHaveCount(1, {timeout});
  const condition = assertion.condition || 'visible';
  if (condition === 'visible') return expect(target).toBeVisible({timeout});
  if (condition === 'checked') return expect(target).toBeChecked({timeout});
  if (condition === 'unchecked') {
    await expect(target).not.toBeChecked({timeout});
    await expect(target).not.toHaveAttribute('aria-checked', 'mixed', {timeout});
    return expect.poll(() => target.evaluate(element => Boolean(element.indeterminate)), {timeout}).toBe(false);
  }
  if (condition === 'disabled') return expect(target).toBeDisabled({timeout});
  if (condition === 'enabled') return expect(target).toBeEnabled({timeout});
  throw new Error('不支持的断言');
}

async function act(page, action) {
  const target = action.locator && page.getByRole(action.locator.role, {name: action.locator.name, exact: true});
  if (action.kind === 'navigate') return page.goto(new URL(action.value, input.url).href, {waitUntil: 'domcontentloaded'});
  if (action.kind === 'observe') return page.waitForLoadState('domcontentloaded');
  if (action.kind === 'press' && !target) return page.keyboard.press(action.value);
  await expect(target).toHaveCount(1);
  const dialogs = page.getByRole('dialog').or(page.getByRole('alertdialog'));
  if (await dialogs.count() && !await dialogs.getByRole(action.locator.role, {name: action.locator.name, exact: true}).count())
    throw new Error('目标元素位于活动弹窗之外');
  if (action.kind === 'click') return target.click();
  if (action.kind === 'type') return target.fill(action.value);
  if (action.kind === 'select') return target.selectOption(action.value);
  if (action.kind === 'press') return target.press(action.value);
  throw new Error('未授权动作');
}

for (const [caseIndex, scenario] of cases.entries()) {
  test(scenario.id, async ({acceptancePage: page}, info) => {
    const result = {id: scenario.id, kind: scenario.kind, scenario_hash: scenario.scenario_hash,
      status: 'inconclusive', passed: null, checks: [], evidence_refs: [], binding: input.binding};
    let failureKind = 'infrastructure';
    let stepIndex = 0;
    let assertionIndex = 0;
    let failureContext = 'infrastructure';
    let failedAssertion = null;
    const recordChecks = async (checks, checkpoint, context = 'checkpoint', timeout = 4000) => {
      for (const [index, assertion] of checks.entries()) {
        stepIndex = checkpoint;
        assertionIndex = index;
        failureContext = context;
        failedAssertion = assertion;
        failureKind = 'assertion';
        await test.step(`${context} ${checkpoint}:${index}`, () => check(page, assertion, timeout));
        result.checks.push({step: checkpoint, assertion_index: index, context, assertion, passed: true, failure_kind: null});
      }
    };
    try {
      await recordChecks(input.spec.executable_preconditions || [], 0, 'precondition');
      for (const [index, step] of scenario.steps.entries()) {
        stepIndex = index + 1;
        await recordChecks(step.action.preconditions || [], stepIndex, 'action_precondition');
        failureKind = 'action';
        failureContext = 'action';
        failedAssertion = null;
        await test.step(`action ${stepIndex}:${step.action.kind}`, () => act(page, step.action));
        await recordChecks(step.action.wait?.assertions || [], stepIndex, 'wait', (step.action.wait?.timeout_seconds || 5) * 1000);
        await recordChecks(step.action.postconditions || [], stepIndex, 'action_postcondition');
        await recordChecks(step.assertions || [], stepIndex);
      }
      if (scenario.final) await recordChecks(scenario.final, scenario.steps.length + 1);
      result.status = 'pass';
      result.passed = true;
    } catch (error) {
      result.status = failureKind === 'assertion' ? 'fail' : 'inconclusive';
      result.passed = failureKind === 'assertion' ? false : null;
      result.error = String(error.message).slice(0, 12000);
      result.failure_kind = failureKind;
      result.checks.push({step: stepIndex, assertion_index: assertionIndex, context: failureContext, assertion: failedAssertion,
        passed: failureKind === 'assertion' ? false : null, failure_kind: failureKind});
      throw error;
    } finally {
      const evidencePath = info.outputPath('evidence.json');
      const snapshotPath = info.outputPath('page.html');
      const screenshotPath = info.outputPath('checkpoint.png');
      fs.writeFileSync(snapshotPath, await page.content().catch(() => ''));
      await page.screenshot({path: screenshotPath, fullPage: true}).catch(() => {});
      result.evidence_refs = [evidencePath, snapshotPath, screenshotPath];
      fs.writeFileSync(evidencePath, JSON.stringify(result, null, 2));
      await info.attach('deterministic-evidence', {path: evidencePath, contentType: 'application/json'});
      fs.writeFileSync(path.join(output, `case-${caseIndex}.json`), JSON.stringify(result, null, 2));
    }
  });
}
