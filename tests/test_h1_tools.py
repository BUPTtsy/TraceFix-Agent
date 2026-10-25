import sys

import pytest

from tracefix.execution.runner import process
from tracefix.runtime.contracts import PatchProposal, Phase
from tracefix.runtime.smoke import make_engine
from tracefix.tools.handlers import build_runtime_tools


@pytest.mark.parametrize('phase', [Phase.PREPARE, Phase.EXPLORE, Phase.DIAGNOSE, Phase.VERIFY])
def test_host_tools_cannot_restore_removed_repair_capabilities(tmp_path, phase):
    engine, state = make_engine(tmp_path)
    state.phase = phase
    runtime = build_runtime_tools(engine, state, PatchProposal)
    assert all(runtime.registry.contains(name) for name in ['Read', 'Glob', 'Grep', 'Bash'])
    assert not any(runtime.registry.contains(name) for name in [
        'Write', 'Edit', 'NotebookEdit', 'RunTests', 'FinishRepair', 'RevertOverlay', 'git.history'])


@pytest.mark.asyncio
async def test_process_can_preserve_full_output_without_changing_existing_default():
    argv = [sys.executable, '-c', "print('x' * 110000, end='')"]
    assert len((await process(argv))['output']) == 100_000
    assert len((await process(argv, output_limit=None))['output']) == 110_000
