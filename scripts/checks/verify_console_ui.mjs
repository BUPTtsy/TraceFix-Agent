import assert from 'node:assert/strict';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.TRACEFIX_PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto((process.env.TRACEFIX_VERIFY_URL || 'http://127.0.0.1:3099') + '/#overview');
  const title = `浏览器验收：刷新状态回归 ${Date.now()}`;
  await page.getByRole('navigation', {name: '工作台导航'}).getByRole('link', {name: /项目空间/}).click();
  await page.getByRole('heading', {name: /的运行配置/}).waitFor();
  await page.getByRole('button', {name: '查看项目运行'}).click();
  await page.getByRole('heading', {name: /Agent Runs/}).waitFor();
  assert.equal(await page.getByLabel('Agent 对话面板').count(), 1);
  await page.getByLabel('新的运行目标', {exact: true}).fill('浏览器验收：验证失败启动记录 ' + title);
  await page.getByRole('button', {name: '启动 Run ↑'}).click();
  await page.locator('.run-row').filter({hasText: title}).click();
  await page.locator('.log-view').filter({hasText: 'TRACEFIX_DATABASE_URL'}).waitFor();
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: '.tracefix/console-mobile.png', fullPage: true});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  assert.deepEqual(errors, []);
  console.log('通过：导航、失败启动、移动端布局');
} finally {await browser.close();}
