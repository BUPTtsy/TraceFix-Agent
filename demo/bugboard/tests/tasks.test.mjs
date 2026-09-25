import test from 'node:test';
import assert from 'node:assert/strict';
import {seed,validateTitle,applyFields} from '../server/tasks.mjs';
test('seed is deterministic and isolated',()=>{const a=seed();a.pop();assert.equal(seed().length,6);});
test('title validation rejects blank input',()=>{assert.throws(()=>validateTitle('   '));assert.equal(validateTitle(' A '),'A');});
test('patch preserves unrelated task fields',()=>{const t=seed()[0];const p=applyFields(t,{status:'Done'});assert.equal(p.title,t.title);assert.equal(p.status,'Done');assert.equal(t.status,'Todo');});
test('status rejects out-of-contract values',()=>assert.throws(()=>applyFields(seed()[0],{status:'hidden'})));
