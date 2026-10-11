import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {completionFixture} from '../fixtures/completion.mjs';
import {runAcceptance} from '../runner.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..');
for (const [mode, expected] of [['defect', 'fail'], ['fixed', 'pass'], ['regression', 'fail']]) {
  test(`真实浏览器 ${mode} 保持冻结完成/取消/刷新断言`, {timeout: 90000}, async () => {
    await fs.mkdir(path.join(root, 'artifacts/interface-beta'), {recursive: true});
    const output = await fs.mkdtemp(path.join(root, 'artifacts/interface-beta/browser-'));
    const fixture = await completionFixture(mode);
    try {
      const result = await runAcceptance(fixture.input, output);
      assert.equal(result.status, expected, JSON.stringify(result));
      if (mode === 'regression') assert.equal(result.cases.find(item => item.id === 'reverse').status, 'fail');
      for (const item of result.cases.filter(item => item.status !== 'inconclusive'))
        assert.ok(item.evidence_refs.some(reference => reference.endsWith('.zip')));
    } finally {await fixture.close();}
  });
}
