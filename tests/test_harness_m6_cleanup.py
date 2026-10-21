"""M6 回归：SDK 编码不得复用 legacy 消息历史转换。"""

import asyncio
import inspect

import pytest

from tracefix.runtime.repair_loop import (
    exchange_history,
    is_sdk_coding_state,
    run_segment,
    segment_history,
)


class _State:
    harness_job_id = "job-sdk"
    harness_stage = "coding"


def test_sdk_coding_history_helpers_are_noops():
    state = _State()
    assert is_sdk_coding_state(state)
    assert exchange_history(None, state) == []
    assert segment_history(None, state, {}) == []


def test_legacy_state_keeps_history_helpers():
    state = _State()
    state.harness_job_id = None
    assert not is_sdk_coding_state(state)
    assert "history_ref" in inspect.getsource(segment_history)
    assert "message_history" in inspect.getsource(exchange_history)


@pytest.mark.asyncio
async def test_sdk_coding_cannot_enter_legacy_repair_loop():
    with pytest.raises(RuntimeError, match="HarnessRuntime"):
        await run_segment(None, _State())
