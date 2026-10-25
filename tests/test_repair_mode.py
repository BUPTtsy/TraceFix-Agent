import pytest
from pydantic import ValidationError

from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.runtime.continuation import continuation_state
from tracefix.runtime.contracts import Guidance, Outcome, RunState, RunStatus, reduce_state
from tracefix.runtime.guidance import retarget_state
from tracefix.service.contracts import ExecutionRequest


@pytest.mark.parametrize('mode', ['pipeline', 'hybrid', 'legacy', 'claude_sdk'])
def test_removed_repair_mode_cannot_be_requested(mode):
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate({'repair_mode': mode})


@pytest.mark.parametrize('backend', ['legacy', 'pipeline', 'hybrid'])
def test_coding_configuration_rejects_non_sdk_backend(backend):
    with pytest.raises(ValidationError):
        ClaudeSdkConfig(backend=backend, model_id='claude-sonnet-4-6',
            credential_ref='env:TEST_KEY', worker_image='sha256:' + 'a' * 64,
            egress_allowlist=['https://api.anthropic.com'])


def test_historical_repair_mode_remains_loadable_and_immutable():
    previous = RunState(scope_id='demo', goal='修复持久化', url='http://app:3000',
                        repair_mode='hybrid', repair_mode_requested='hybrid')
    assert RunState.model_validate(previous.model_dump()).repair_mode == 'hybrid'
    with pytest.raises(ValueError, match='不可变更'):
        reduce_state(previous, previous.revision, repair_mode='pipeline')


def test_continuation_and_retarget_preserve_historical_metadata():
    previous = RunState(scope_id='demo', goal='修复持久化', url='http://app:3000', mode='repair',
        repair_mode='pipeline', repair_mode_requested='hybrid', repair_mode_reason='not_implemented',
        run_status=RunStatus.FAILED, outcome=Outcome.INFRA_FAILURE)
    continued = continuation_state(previous, '继续检查', [])
    previous.run_status = RunStatus.RUNNING
    entry = Guidance(run_id=previous.run_id, scope_id=previous.scope_id,
        level='retarget', text='修复另外的持久化问题', confirmed_at=1.0)
    child = retarget_state(previous, entry)
    for field in ['repair_mode', 'repair_mode_requested', 'repair_mode_reason']:
        assert getattr(continued, field) == getattr(child, field) == getattr(previous, field)
