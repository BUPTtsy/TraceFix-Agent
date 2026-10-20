import pytest
from pydantic import ValidationError

from tracefix.runtime.contracts import RunState
from tracefix.runtime.harness_contracts import HarnessJob, HarnessSession, HarnessWorkItem, TaskPlan, VerificationCommand


def work(identity='one', **updates):
    return HarnessWorkItem(harness_task_id=identity, job_id='job', scope_id='scope',
        title='修复', acceptance_refs=['original'], allowed_files=['src/**'], writable_files=['src/**'], **updates)


def test_old_run_schema_remains_loadable():
    state = RunState(scope_id='scope', goal='old', url='http://localhost')
    assert state.harness_job_id is None
    assert RunState.model_validate(state.model_dump()).sdk_execution_id is None


def test_contracts_reject_unknown_fields_and_invalid_session_identity():
    with pytest.raises(ValidationError):
        HarnessJob(scope_id='scope', repo_id='repo', console_task_id='console', goal_hash='goal',
            origin_base_commit='base', acceptance_refs=['original'], unexpected=True)
    with pytest.raises(ValidationError):
        HarnessSession(job_id='job', scope_id='scope', kind='coder', run_id='run', input_commit='base')
    initializer = HarnessSession(job_id='job', scope_id='scope', kind='initializer', run_id='run', input_commit='base')
    assert initializer.harness_task_id is None


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/outside', 'src\\file'])
def test_plan_paths_cannot_escape(path):
    with pytest.raises(ValidationError):
        HarnessWorkItem(job_id='job', scope_id='scope', title='escape', acceptance_refs=['original'], allowed_files=[path])


def test_plan_dag_and_acceptance_coverage_are_enforced():
    identity = dict(scope_id='scope', job_id='job', session_id='init', run_id='run', plan_version=1, acceptance_refs=['original'])
    with pytest.raises(ValidationError):
        TaskPlan(**identity, work_items=[work(depends_on=['missing'])])
    with pytest.raises(ValidationError):
        TaskPlan(**identity, work_items=[work(depends_on=['one'])])
    with pytest.raises(ValidationError):
        TaskPlan(**{**identity, 'acceptance_refs': ['original', 'regression']}, work_items=[work()])
    assert TaskPlan(**identity, work_items=[work(), work('two', depends_on=['one'])])


def test_writable_range_must_be_authorized():
    with pytest.raises(ValidationError):
        HarnessWorkItem(job_id='job', scope_id='scope', title='escape', acceptance_refs=['original'],
            allowed_files=['src/**'], writable_files=['tests/**'])


def test_boolean_exit_code_is_not_an_actual_integer_receipt():
    with pytest.raises(ValidationError):
        VerificationCommand(command=['pytest'], exit_code=False, evidence_ref='receipt')
