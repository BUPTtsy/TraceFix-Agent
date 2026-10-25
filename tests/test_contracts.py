import time
import pytest

from tracefix.config import Project
from tracefix.runtime.contracts import (Budget, Phase, RunState, RunStatus,
    Validation, reduce_state, verification_gate)
from tracefix.runtime.smoke import make_engine
from tests.support_sdk_candidate import verified_sdk_candidate


def state(**kwargs):
    return RunState(scope_id='b',goal='test persisted task',url='http://app:3000',**kwargs)


def test_illegal_transition_and_revision():
    s=state()
    with pytest.raises(ValueError): reduce_state(s,0,phase=Phase.PATCH)
    with pytest.raises(ValueError): reduce_state(s,5,phase=Phase.EXPLORE)
    with pytest.raises(ValueError): reduce_state(s,0,scope_id='c')
    assert reduce_state(s,0,phase=Phase.EXPLORE).revision==1


def test_patch_requires_alignment_and_reproduction():
    s=state(phase=Phase.DIAGNOSE)
    with pytest.raises(ValueError): reduce_state(s,0,phase=Phase.PATCH)
    s.reproduced=True;s.source_aligned=True
    assert reduce_state(s,0,phase=Phase.PATCH).phase==Phase.PATCH


def test_terminal_blocks_all_changes():
    s=state(run_status=RunStatus.COMPLETED)
    with pytest.raises(ValueError): reduce_state(s,0,pending_action={'write':'x'})


def test_run_state_tracks_frozen_agent_instructions_and_model_artifacts():
    s=state(agent_instructions_ref='instructions.txt',agent_instructions_hash='hash',
            agent_instructions_path='AGENTS.md',model_exchange_refs=['request.json','response.json'])
    assert s.agent_instructions_path=='AGENTS.md'
    assert s.model_exchange_refs==['request.json','response.json']


def test_project_agent_instructions_must_stay_inside_project(tmp_path):
    assert Project(id='b',repo_id='r',root=tmp_path).agent_instructions=='AGENTS.md'
    with pytest.raises(ValueError):
        Project(id='b',repo_id='r',root=tmp_path,agent_instructions='../AGENTS.md')


def test_patch_invalidates_validation_and_approval():
    s=state(phase=Phase.PATCH,patch_hash='old',validation_refs=['passed'],approval_ref='old-approval',source_aligned=True,reproduced=True)
    new=reduce_state(s,0,patch_hash='new')
    assert new.validation_refs==[] and new.approval_ref is None


async def test_current_evidence_and_independent_regression_required(tmp_path):
    engine, current = await verified_sdk_candidate(tmp_path)
    validations = [Validation(**engine.get(current, ref)) for ref in current.validation_refs]
    assert {validation.kind for validation in validations} == {'static', 'unit', 'build', 'health', 'original', 'regression'}

    def gate(saved, values):
        return verification_gate(saved, values,
            lambda ref: engine.bundle_exists(saved, ref),
            artifact_read=lambda ref: engine.get(saved, ref),
            artifact_read_bytes=lambda ref: engine.artifacts.read(saved.scope_id, saved.run_id, ref))

    assert gate(current, validations)
    assert not gate(current, [validation.model_copy(update={'patch_hash': 'old'}) for validation in validations])
    assert not gate(current, [validation for validation in validations if validation.kind != 'regression'])
    missing = current.model_copy(update={'evidence_refs': current.evidence_refs + ['missing-failure.json']})
    assert not gate(missing, validations)


def test_budget_costs_never_refunded():
    b=Budget(max_model_calls=1)
    b=b.charge('model_calls')
    previous = b
    b = b.charge('model_calls')
    assert previous.model_calls == 1 and b.model_calls == 2
    with pytest.raises(ValueError): b.charge('tokens',-1)
    assert Budget(deadline=time.time()-1).check_time() is None
