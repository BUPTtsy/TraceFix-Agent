import pytest

from tracefix.runtime.contracts import CheckJudgement, Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools.evidence import attach_source_evidence_tools
from tracefix.tools.handlers import build_runtime_tools


@pytest.mark.parametrize('mode,phase', [('test', Phase.EXPLORE), ('repair', Phase.VERIFY)])
async def test_code_analysis_reads_overlay_and_records_current_check_evidence(tmp_path, mode, phase):
    engine, state = make_engine(tmp_path / mode)
    state.mode, state.phase, state.patch_hash = mode, phase, 'current-patch'
    engine.store.save(state)
    source = engine.workspace.root / 'src/value.ts'
    baseline = source.read_bytes()
    candidate = 'export const persisted = true;\n'
    engine.workspace.stage('src/value.ts', candidate)
    context = {'available_evidence_refs': ['existing-evidence']}
    previous_context = context
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    attach_source_evidence_tools(engine, state, CheckJudgement, lambda: context, runtime)
    context = {'available_evidence_refs': ['current-evidence']}

    receipt = await runtime.pipeline().execute('CodeAnalyze', {
        'paths': [str(source)], 'check': 'regex', 'config': {'pattern': 'persisted = true'},
    }, 'analyze-overlay')

    assert not receipt.is_error
    assert receipt.result['status'] == 'fail'
    assert receipt.result['findings'][0]['match'] == 'persisted = true'
    assert receipt.result['source_manifest'] == state.source_manifest
    assert receipt.result['content_versions'] == {'src/value.ts': digest(candidate.encode())}
    reference = receipt.result['artifact_ref']
    evidence = engine.get(state, reference)
    assert evidence['tool'] == 'code.analyze'
    assert evidence['source_manifest'] == state.source_manifest
    assert evidence['patch_hash'] == 'current-patch'
    assert reference in engine.store.load(state.run_id, state.scope_id).evidence_refs
    assert context['available_evidence_refs'] == ['current-evidence', reference]
    assert context['evidence_refs'] == context['available_evidence_refs']
    assert previous_context == {'available_evidence_refs': ['existing-evidence']}
    assert source.read_bytes() == baseline


async def test_code_analysis_rejects_outside_paths_and_preserves_analyzer_fallback(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'run')
    state.phase = Phase.EXPLORE
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    pipeline = runtime.pipeline()
    denied = await pipeline.execute('CodeAnalyze', {'paths': [str(tmp_path / 'outside.ts')]}, 'denied')
    assert denied.is_error and denied.error['executed'] is False

    def unavailable(files, config):
        raise ImportError('parser unavailable')

    monkeypatch.setattr('tracefix.rules.analyzers._source_analysis', unavailable)
    result = await pipeline.execute('CodeAnalyze', {
        'paths': [str(engine.workspace.root / 'src/value.ts')],
    }, 'analyzer-failed')
    assert not result.is_error
    assert result.result['status'] == 'error'
    assert result.result['fallback_required'] is True
    assert 'parser unavailable' in result.result['errors'][0]['message']
    assert engine.get(state, result.result['artifact_ref'])['fallback_required'] is True
