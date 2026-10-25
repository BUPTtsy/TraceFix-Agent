from tracefix.runtime.contracts import Outcome, Phase, RunState
from tests.test_harness_graph import create_harness_engine


async def verified_sdk_candidate(tmp_path, *, configure=None, runner_factory=None,
                                 stop_before_verify=False):
    engine, state, harness, _ = await create_harness_engine(
        tmp_path, configure=configure, runner_factory=runner_factory)
    review = harness.review
    verify = engine.verify

    async def stop_before_acceptance(current, active):
        return current.output(active, 'end')

    if stop_before_verify:
        engine.verify = stop_before_acceptance
    else:
        harness.review = stop_before_acceptance
    try:
        output = await engine.run(state)
    finally:
        harness.review = review
        engine.verify = verify
    active = RunState.model_validate(output['data'])
    assert active.phase == (Phase.VERIFY if stop_before_verify else Phase.REVIEW), active.error
    if not stop_before_verify:
        assert active.outcome == Outcome.FIX_VERIFIED, active.error
        assert engine.verification_passed(active)
    return engine, active


async def finalize_sdk_candidate(engine, state):
    output = await engine.harness.review(engine, state)
    active = RunState.model_validate(output['data'])
    assert output['next_node'] == 'finalize', active.error
    await engine.finalize(active, None)
    return engine.store.load(active.run_id, active.scope_id)
