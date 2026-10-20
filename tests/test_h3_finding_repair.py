import pytest

from tracefix.model.contracts import ModelResult
from tracefix.runtime.contracts import FinishRepair, Outcome
from tests.test_finding_repair_runtime import executable_repair_fixture


@pytest.mark.parametrize('runner_fail', [None, 'unit'])
async def test_hybrid_finding_repair_keeps_independent_source_and_validation_gates(tmp_path, runner_fail):
    engine, repair, library, finding = await executable_repair_fixture(tmp_path, runner_fail=runner_fail)
    original = engine.model

    class HybridModel:
        supports_tool_executor = True

        async def generate(self, schema, context, **options):
            if schema is not FinishRepair:
                return await original.generate(schema, context, **options)
            assert repair.reproduced or engine.store.load(repair.run_id, repair.scope_id).reproduced
            pipeline = options['tool_pipeline']
            write = await pipeline.execute('Write', {
                'file_path': str(engine.workspace.root / 'src/value.ts'),
                'content': 'export const persisted = true;\n'}, 'hybrid-write')
            assert not write.is_error, write.error
            finish = await pipeline.execute('FinishRepair', {'summary': '修复来源 Finding 的持久化问题'}, 'hybrid-finish')
            assert not finish.is_error, finish.error
            return ModelResult(FinishRepair(summary='提交候选'), {}, 'scripted', 'tool_calls')

    engine.model = HybridModel()
    repair.repair_mode, repair.repair_mode_requested, repair.repair_mode_reason = 'hybrid', 'hybrid', None
    engine.store.save(repair)
    assert not repair.reproduced and repair.outcome is None and not repair.validation_refs
    await engine.run(repair)
    completed = engine.store.load(repair.run_id, repair.scope_id)
    assert completed.reproduced and completed.trial == 3
    assert completed.repair_mode == 'hybrid'
    result = library.finding(finding.id)
    report = engine.get(completed, completed.report_ref)
    if runner_fail:
        assert completed.outcome != Outcome.FIX_VERIFIED
        assert result.status != 'fixed'
        assert report['patch_verification'] != 'verified' and not report['provenance']['verified']
    else:
        assert completed.outcome == Outcome.FIX_VERIFIED
        assert completed.repair_segment >= 1 and completed.validation_refs
        assert result.status == 'fixed' and result.repair_run_id == completed.run_id
        assert result.patch_hash == completed.patch_hash and result.verification_refs == completed.validation_refs
        assert report['provenance']['verified'] and report['source_findings'][0]['id'] == finding.id
