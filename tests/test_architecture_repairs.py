from functools import partial

import pytest

from tracefix.knowledge.assembler import ContextAssembler, ContextWindowError, TokenCounter
from tracefix.knowledge.context import build_context
from tracefix.knowledge.memory import MemoryLibrary
from tracefix.runtime.continuation import continuation_state, derive_run
from tracefix.runtime.contracts import Contract, Phase, RunStatus, digest
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.tools import ToolPipeline, ToolRegistry, ToolSpec


@pytest.mark.parametrize('derived', [False, True])
def test_execution_epoch_resets_loop_and_context_state(tmp_path, derived):
    engine, previous = make_engine(tmp_path)
    previous.run_status = RunStatus.ABNORMAL
    previous.loop_state_fingerprints = ['old']
    previous.loop_error_signatures = ['error']
    previous.loop_no_progress_steps = 30
    previous.loop_warnings = [{'reason': 'old'}]
    previous.loop_evidence = {'error': 'old'}
    previous.last_error_signature = 'old'
    previous.working_memory_ref = 'old-memory'
    previous.context_phase = 'diagnose'
    previous.context_compaction_cursor = 40
    previous.context_manifest_refs = ['old-manifest']
    previous.compaction_refs = ['old-compaction']
    previous.model_exchange_refs = ['old-exchange']
    previous.reasoning_refs = ['old-reasoning']
    previous.patch_base_commit = 'a' * 40
    current = derive_run(previous) if derived else continuation_state(previous, 'retry', [])
    assert not current.loop_state_fingerprints and not current.loop_error_signatures
    assert current.loop_no_progress_steps == 0 and not current.loop_warnings
    assert current.loop_evidence is None and current.last_error_signature is None
    assert current.working_memory_ref is None and current.context_phase is None
    assert current.context_compaction_cursor == 0
    assert current.patch_base_commit == (None if derived else previous.patch_base_commit)
    for field in ('context_manifest_refs', 'compaction_refs', 'model_exchange_refs', 'reasoning_refs'):
        assert getattr(current, field) == ([] if derived else getattr(previous, field))
    assert previous.loop_no_progress_steps == 30


@pytest.mark.parametrize('structured', [False, True])
def test_index_redacts_content_before_storage_hash_and_search(tmp_path, structured):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    secret = 'sensitive-value'
    content = {'password': secret} if structured else 'password=' + secret
    library.upsert({'id': 'secret', 'scope_id': 'project', 'layer': 'M3', 'kind': 'failure_recipe',
                    'revision': 1, 'source_revision': 'source', 'content': content})
    with library.connect() as connection:
        row = connection.execute('SELECT content,content_hash,search_text FROM local_memory_items').fetchone()
    assert secret not in row['content'] and secret not in row['search_text']
    assert '[REDACTED]' in row['content']
    assert row['content_hash'] == digest(row['content'])


def test_pruning_keeps_failure_facts_and_diagnostic_channels(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    failure = {'result': {'isError': True, 'error': 'critical failure'}, 'evidence_refs': ['ev']}
    history = [failure] + [{'step': index, 'result': 'ok ' * 60} for index in range(8)]
    assert failure in build_context(state, {}, pairs=history)['recent_action_results']
    assembler = ContextAssembler(context_window=850, output_tokens=100, overhead_tokens=50,
                                 counter=TokenCounter(lambda value: len(value)))
    memory = {'finding': [{'text': 'regression', 'evidence_refs': ['ev']}],
              'excluded': [{'text': 'not network'}], 'progress': [{'text': 'old ' * 80}]}
    result = assembler.assemble({'recent_action_results': history, 'working_memory': memory,
        'observation': {'snapshot': 'large ' * 200, 'console': 'error: stale', 'network': 'GET /api 500'}})
    facts = result.context['pruning_facts']
    assert facts['recent_action_results'] == [failure]
    assert facts['observation'] == {'console': 'error: stale', 'network': 'GET /api 500'}
    assert facts['working_memory']['finding'] == memory['finding']
    assert facts['working_memory']['excluded'] == memory['excluded']
    assert result.manifest['tokens_after'] <= result.manifest['input_limit']
    assert next(block for block in result.manifest['blocks'] if block['name'] == 'pruning_facts')['protected']
    with pytest.raises(ContextWindowError):
        assembler.assemble({'recent_steps': [{'error': 'too large ' * 200}]})


async def test_stable_operation_identity_uses_real_store_and_current_call_id(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    executed = []

    class WriteInput(Contract):
        value: str
        label: str = 'default'

    async def write(arguments, call_id):
        executed.append(arguments.value)
        return {'saved': arguments.value}

    registry = ToolRegistry([ToolSpec('memory.note', 'Save note', WriteInput,
        side_effect='write', idempotency_key=digest)])
    for call_id in ('first', 'regenerated'):
        pipeline = ToolPipeline(registry, {'memory.note': write}, Phase.DIAGNOSE,
                                operation=partial(engine.operation, state))
        arguments = {'value': 'same'}
        if call_id == 'regenerated':
            arguments['label'] = 'default'
        result = await pipeline.execute('memory.note', arguments, call_id)
        assert result.call_id == call_id and not result.is_error
    assert executed == ['same'] and len(engine.store.operations) == 1
    await pipeline.execute('memory.note', {'value': 'changed'}, 'different')
    assert executed == ['same', 'changed'] and len(engine.store.operations) == 2
