import {defineConfig} from '@playwright/test';
import path from 'node:path';

const output = process.env.TRACEFIX_ACCEPTANCE_OUTPUT;
if (!output || !process.env.TRACEFIX_ACCEPTANCE_REQUEST) throw new Error('缺少验收请求或产物目录');
export default defineConfig({
  testDir: './specs', fullyParallel: false, workers: 1, retries: 0, timeout: 60000,
  expect: {timeout: 4000}, outputDir: path.join(output, 'test-results'),
  reporter: [['json', {outputFile: path.join(output, 'playwright-report.json')}],
    ['html', {outputFolder: path.join(output, 'html-report'), open: 'never'}]],
  use: {headless: true, viewport: {width: 1280, height: 800}, locale: 'en-US',
    launchOptions: process.env.TRACEFIX_ACCEPTANCE_CHROMIUM_PATH
      ? {executablePath: process.env.TRACEFIX_ACCEPTANCE_CHROMIUM_PATH} : {},
    actionTimeout: 5000, navigationTimeout: 10000, trace: 'on', screenshot: 'on'},
});
