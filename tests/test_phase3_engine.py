import pytest

from tracefix.model.gateway import ModelOutputError
from tracefix.runtime.contracts import FileEdit, PatchProposal, Phase, RunState, digest
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.tool_handlers import build_runtime_tools


def whole(engine, state, content='export const persisted = true;\n'):
    return PatchProposal(summary='局部修正持久化', evidence_refs=state.evidence_refs,
        edits=[FileEdit(path='src/value.ts', before_hash=digest(engine.workspace.path('src/value.ts').read_bytes()),
                        content=content)])


@pytest.mark.asyncio
async def test_diagnose_materializes_final_refs_without_model_whole_content(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'run')
    engine.subagent_enabled = False
    state.phase = Phase.DIAGNOSE
    state.reproduced = state.source_aligned = True
    state.evidence_refs = [engine.put(state, {'public_failure': 'reload'})]
    engine.store.save(state)
    runtime = build_runtime_tools(engine, state, PatchProposal)
    staged = await runtime.pipeline().execute('Edit', {
        'file_path': str(engine.workspace.root / 'src/value.ts'),
        'edits': [{'old_string': 'persisted = false', 'new_string': 'persisted = true'}],
        'expected_overlay_revision': 0}, 'stage')
    seen = []

    async def model_call(current, schema, context):
        seen.append(context)
        return PatchProposal(summary='修正 persist', evidence_refs=current.evidence_refs,
                             staged_refs=[staged.result['staged_ref']])

    monkeypatch.setattr(engine, 'model_call', model_call)
    output = await engine.diagnose(state, None)
    current = RunState(**output['data'])
    proposal = PatchProposal(**engine.get(current, current.patch_ref))
    assert current.phase == Phase.PATCH and proposal.edits[0].content.endswith('true;\n')
    assert proposal.staged_refs and 'staged_refs' in seen[0]['instruction']
    assert 'false' in engine.workspace.path('src/value.ts').read_text()


def test_engine_syntax_rejection_keeps_original_and_accepts_correction(tmp_path):
    engine, state = make_engine(tmp_path / 'run')
    state.evidence_refs = [engine.put(state, {'public_failure': True})]
    invalid = whole(engine, state, 'export const persisted = ;\n')
    with pytest.raises(ModelOutputError) as rejected:
        engine.validate_patch_candidate(invalid, state=state)
    assert rejected.value.details['error_code'] == 'NEW_SYNTAX_ERROR'
    assert rejected.value.details['written'] is False
    engine.validate_patch_candidate(whole(engine, state), state=state)
    assert 'false' in engine.workspace.read('src/value.ts')


@pytest.mark.asyncio
async def test_public_failure_feedback_binds_candidate_and_blocks_repeat(tmp_path):
    engine, state = make_engine(tmp_path / 'run', runner_fail='static')
    state.evidence_refs = [engine.put(state, {'public_failure': True})]
    proposal = whole(engine, state)
    state.patch_ref = engine.put(state, proposal.model_dump())
    state.patch_base_commit = engine.workspace.head()
    state.patch_hash = engine.workspace.apply(proposal, base=state.patch_base_commit)['patch_hash']
    state.phase = Phase.VERIFY
    state.environment_digest = 'public-fixture-environment'
    engine.store.save(state)
    output = await engine.verify(state, None)
    current = RunState(**output['data'])
    assert current.phase == Phase.DIAGNOSE
    feedback = engine.get(current, current.diagnosis_feedback_refs[-1])
    assert feedback['source'] == 'public_development'
    assert feedback['status'] == 'failed' and feedback['failure_class'] == 'editing'
    assert feedback['binding']['candidate_ref'] == current.patch_ref
    assert feedback['binding']['patch_hash'] == current.patch_hash
    assert current.failed_candidate_signatures[0]['patch_hash'] == current.patch_hash
    assert engine.current_diagnosis_feedback(current) == [feedback]
    repeated = whole(engine, current)
    with pytest.raises(ModelOutputError) as duplicate:
        engine.validate_patch_candidate(repeated, state=current)
    assert duplicate.value.details['error_code'] in {'NO_CHANGE', 'FAILED_CANDIDATE_REPEATED'}
    engine.validate_patch_candidate(whole(engine, current, 'export const persisted = true;\nexport const corrected = 1;\n'),
                                    state=current)
    drifted = current.model_copy(update={'patch_hash': 'different'})
    assert engine.current_diagnosis_feedback(drifted) == []


@pytest.mark.asyncio
async def test_skill_injection_restores_frozen_body_after_source_change(tmp_path):
    engine, state = make_engine(tmp_path / 'run')
    from tracefix.knowledge.context import SkillCatalog
    path = tmp_path / 'recipes' / 'edit' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('---\nname: edit\ndescription: 编辑配方\nphases: [DIAGNOSE]\n---\n精确 old/new。\n', encoding='utf-8')
    engine.skills = SkillCatalog(path.parents[1])
    state.phase = Phase.DIAGNOSE
    engine.store.save(state)
    first = engine.inject_skills(state, {'load_skills': ['edit']})
    path.write_text('changed recipe', encoding='utf-8')
    second = engine.inject_skills(state, {'load_skills': ['edit']})
    assert first['skills'][0]['content'] == second['skills'][0]['content']
    assert first['skills'][0]['snapshot_ref'] == second['skills'][0]['snapshot_ref']
    assert len([event for event in engine.store.trace(state.run_id, state.scope_id)
                if event['type'] == 'skill.loaded']) == 1
