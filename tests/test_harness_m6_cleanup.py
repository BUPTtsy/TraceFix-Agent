import pytest

from tracefix.runtime.contracts import Phase
from tracefix.runtime.smoke import make_engine


@pytest.mark.parametrize('phase', [Phase.DIAGNOSE, Phase.REVIEW])
def test_host_coding_routes_require_sdk_harness(tmp_path, phase):
    engine, state = make_engine(tmp_path)
    state.phase = phase
    with pytest.raises(RuntimeError, match='Claude Agent SDK Harness'):
        engine.route(state)


@pytest.mark.parametrize('phase', [Phase.DIAGNOSE, Phase.REVIEW])
def test_sdk_job_cannot_fall_back_when_runtime_is_missing(tmp_path, phase):
    engine, state = make_engine(tmp_path)
    state.phase = phase
    state.harness_job_id = 'sdk-job'
    state.harness_stage = 'coding'
    with pytest.raises(RuntimeError, match='持久化 Harness'):
        engine.route(state)
