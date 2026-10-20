import fs from 'node:fs/promises';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
const request = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const url = request.url;
if (new URL(url).hostname !== 'app') throw new Error('Oracle URL must use the isolated application');
const allCases = Array.from({length: 12}, (_, index) => `B${String(index + 1).padStart(2, '0')}`);
const fixes = request.oracle_cases;
if (!Array.isArray(fixes) || fixes.some(value => !allCases.includes(value))) throw new Error('Invalid private cases');
const checks = [];
let browser;
let infrastructureError = null;

async function runCase(caseId, purpose) {
  const context = await browser.newContext({viewport: {width: 1280, height: 800}, locale: 'en-US', serviceWorkers: 'block'});
  const page = await context.newPage();
  page.setDefaultTimeout(4000);
  const steps = [];
  const by = (role, name) => page.getByRole(role, {name, exact: true});
  const ensure = (value, message) => {if (!value) throw new Error(message);};
  const visible = async (role, name) => by(role, name).waitFor({state: 'visible'});
  async function editTitleReady() {
    const title = by('textbox', 'Task title');
    await title.waitFor({state: 'visible'});
    await page.waitForFunction(input => input.value === 'Write project brief', await title.elementHandle());
    return title;
  }
  async function step(name, work) {
    try {await work(); steps.push({name, passed: true});}
    catch (error) {steps.push({name, passed: false, error: error.message}); throw error;}
  }
  async function checkbox(checked, title = 'Write project brief') {
    const target = by('checkbox', `Complete ${title}`);
    await target.waitFor({state: 'visible'});
    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
      if (await target.isEnabled() && await target.isChecked() === checked) {
        ensure(await page.getByRole('alert').count() === 0, 'Task operation displayed an error');
        return;
      }
      await page.waitForTimeout(40);
    }
    throw new Error(`Task completion state is not ${checked}`);
  }
  async function mutation(work, pattern, method) {
    const [response] = await Promise.all([
      page.waitForResponse(response => pattern.test(new URL(response.url()).pathname)
        && response.request().method() === method), work(),
    ]);
    ensure(response.ok(), 'Task mutation request failed');
    await response.finished();
  }
  async function toggle(checked) {
    await mutation(() => by('checkbox', 'Complete Write project brief').click(), /\/api\/tasks\/\d+$/, 'PATCH');
    await checkbox(checked);
  }
  let passed = false;
  let error = null;
  try {
    const reset = await context.request.post(url + '/__reset');
    ensure(reset.ok(), 'Reset failed');
    await page.goto(url);
    await visible('heading', 'Task board');
    switch (caseId) {
      case 'B01':
        await step('initial_uncompleted', () => checkbox(false));
        await step('complete', () => toggle(true));
        await step('reload_completed', async () => {await page.reload(); await checkbox(true);});
        await step('cancel_completion', () => toggle(false));
        await step('reload_uncompleted', async () => {await page.reload(); await checkbox(false);});
        break;
      case 'B02':
        await step('delete_persists', async () => {
          await mutation(() => by('button', 'Delete Write project brief').click(), /\/api\/tasks?\/\d+$/, 'DELETE');
          await page.reload();
          ensure(await by('checkbox', 'Complete Write project brief').count() === 0, 'Deleted task returned after reload');
        });
        break;
      case 'B03':
        await step('blank_title_explained', async () => {
          await by('button', 'Add task').click();
          await page.getByRole('alert').waitFor({state: 'visible'});
          ensure((await page.getByRole('alert').innerText()).includes('required'), 'Required title error absent');
        });
        break;
      case 'B04':
        await step('edit_prefilled', async () => {
          await by('button', 'Edit Write project brief').click();
          await editTitleReady();
        });
        break;
      case 'B05':
        await step('todo_filter', async () => {await by('button', 'Todo').click(); await checkbox(false);});
        break;
      case 'B06':
        await step('uppercase_search', async () => {
          await by('textbox', 'Search tasks').fill('WRITE');
          await checkbox(false);
        });
        await step('lowercase_search', async () => {
          await by('textbox', 'Search tasks').fill('write');
          await checkbox(false);
        });
        break;
      case 'B07':
        await step('create_immediately_visible', async () => {
          await by('textbox', 'New task title').fill('Fresh item');
          await mutation(() => by('button', 'Add task').click(), /^\/api\/tasks$/, 'POST');
          await checkbox(false, 'Fresh item');
        });
        break;
      case 'B08':
        await step('first_task_visible', () => checkbox(false));
        break;
      case 'B09':
        await step('create_recovers_busy', async () => {
          await by('textbox', 'New task title').fill('Fresh item');
          await mutation(() => by('button', 'Add task').click(), /^\/api\/tasks$/, 'POST');
          const deadline = Date.now() + 4000;
          while (Date.now() < deadline && !(await by('button', 'Add task').isEnabled())) await page.waitForTimeout(40);
          ensure(await by('button', 'Add task').isEnabled(), 'Create left Add task disabled');
        });
        break;
      case 'B10':
        await step('cancel_closes_dialog', async () => {
          await by('button', 'Edit Write project brief').click();
          await by('button', 'Cancel').click();
          await page.getByRole('dialog').waitFor({state: 'hidden'});
        });
        break;
      case 'B11':
        await step('rename_persists', async () => {
          await by('button', 'Edit Write project brief').click();
          await (await editTitleReady()).fill('Revised brief');
          await mutation(() => by('button', 'Save changes').click(), /\/api\/tasks\/\d+$/, 'PATCH');
          await page.reload();
          await checkbox(false, 'Revised brief');
        });
        break;
      case 'B12':
        await step('done_filter_excludes_todo', async () => {
          await by('button', 'Done').click();
          await visible('checkbox', 'Complete Prepare release checklist');
          ensure(await by('checkbox', 'Complete Write project brief').count() === 0, 'Done includes uncompleted task');
        });
        break;
      default: throw new Error('Unknown private GUI check');
    }
    passed = true;
  } catch (failure) {error = failure.message;}
  finally {await context.close();}
  return {case_id: caseId, purpose, passed, error, steps};
}

try {
  browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  for (const caseId of fixes) checks.push(await runCase(caseId, 'fix'));
  for (const caseId of allCases.filter(value => !fixes.includes(value))) checks.push(await runCase(caseId, 'regression'));
} catch (error) {infrastructureError = error.message;}
finally {if (browser) await browser.close();}

const fix = {passed: !infrastructureError && checks.filter(item => item.purpose === 'fix').every(item => item.passed),
  checks: checks.filter(item => item.purpose === 'fix')};
const regression = {passed: !infrastructureError && checks.filter(item => item.purpose === 'regression').length === allCases.length - fixes.length
  && checks.filter(item => item.purpose === 'regression').every(item => item.passed),
  checks: checks.filter(item => item.purpose === 'regression')};
const result = {case_id: request.case_id, binding: request.binding, passed: fix.passed && regression.passed,
  fix, regression, infrastructure_error: infrastructureError, oracle: 'private-docker-playwright-v1', final_scoring_only: true};
console.log(JSON.stringify(result));
process.exitCode = infrastructureError ? 2 : result.passed ? 0 : 1;
