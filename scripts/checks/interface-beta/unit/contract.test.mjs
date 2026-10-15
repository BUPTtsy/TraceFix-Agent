import test from 'node:test';
import assert from 'node:assert/strict';
import {compileCases, digest, oracleResult} from '../contract.mjs';
import {completionFixture} from '../fixtures/completion.mjs';

test('受控映射保持场景和冻结 hash，禁止代码/跨 origin/旧 refs', async () => {
  const fixture = await completionFixture();
  try {
    assert.equal(compileCases(fixture.input).length, 4);
    const changed = structuredClone(fixture.input);
    changed.spec.behavior_scenarios[0].steps[0].action.code = 'process.exit(0)';
    changed.binding.spec_hash = digest(changed.spec);
    assert.throws(() => compileCases(changed), /动作/);
    delete changed.spec.behavior_scenarios[0].steps[0].action.code;
    changed.spec.behavior_scenarios[0].steps[0].action = {kind: 'navigate', value: 'http://example.com'};
    changed.binding.spec_hash = digest(changed.spec);
    assert.throws(() => compileCases(changed), /origin/);
    changed.spec.behavior_scenarios[0].steps[0].action = {kind: 'click', locator: {role: 'checkbox', name: 'task'}, element_ref: 'e1'};
    changed.binding.spec_hash = digest(changed.spec);
    assert.throws(() => compileCases(changed), /旧页面/);
  } finally {await fixture.close();}
});

test('Oracle 二值结果只能来自确定性通过或失败', () => {
  assert.throws(() => oracleResult({status: 'inconclusive'}, 'B01'), /不确定/);
  assert.equal(oracleResult({status: 'fail', cases: [{id: 'reverse', status: 'fail'}]}, 'B01').passed, false);
});
