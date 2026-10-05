import json

import pytest

from tracefix.knowledge.assembler import ContextAssembler, ContextWindowError, TokenCounter, compact_steps
from tracefix.knowledge.memory import MemoryLibrary, MemoryNote
from tracefix.knowledge.retrieval import Retriever
from tracefix.knowledge.scope import ProjectContext
from tracefix.runtime.smoke import make_engine


def test_context_assembler_counts_tokens_and_preserves_protected_blocks():
    assembler = ContextAssembler(context_window=900, output_tokens=160, overhead_tokens=40,
                                 counter=TokenCounter(lambda value: len(value)))
    result = assembler.assemble({
        'policy': 'P' * 20,
        'goal': 'frozen goal',
        'test_spec': {'assertions': ['must remain']},
        'user_guidance': {'text': 'ack required', 'ack_required': True},
        'instruction': 'do this',
        'observation': {'snapshot': '\n'.join(f'- button "{i}" [ref=e{i}]' for i in range(200))},
        'recent_steps': [{'step': i, 'action': {'kind': 'observe'}} for i in range(20)],
    })
    assert result.context['goal'] == 'frozen goal'
    assert result.context['test_spec']['assertions'] == ['must remain']
    assert result.context['user_guidance']['ack_required'] is True
    assert result.manifest['tokens_after'] <= result.manifest['input_limit']
    assert result.manifest['context_hash']
    assert result.compacted


def test_context_assembler_pauses_when_protected_blocks_do_not_fit():
    assembler = ContextAssembler(context_window=180, output_tokens=100, overhead_tokens=20,
                                 counter=TokenCounter(lambda value: len(value)))
    with pytest.raises(ContextWindowError) as raised:
        assembler.assemble({'policy': 'x' * 100, 'goal': 'y' * 100})
    assert raised.value.status == 'PAUSED'
    assert raised.value.details['protected_blocks']


def test_compaction_retains_failures_and_recent_steps():
    steps = [{'step': 1, 'action': {'kind': 'click'}},
             {'step': 2, 'action': {'kind': 'type'}, 'error': 'tool failed'},
             {'step': 3, 'action': {'kind': 'observe'}},
             {'step': 4, 'action': {'kind': 'finish'}}]
    value = compact_steps(steps, keep_recent=1)
    assert value['merged_steps'] == 3
    assert value['failures'][0]['fact']['status'] == 'failed'
    assert value['failures'][0]['fact']['error'] == 'tool failed'
    assert value['recent_steps'][0]['step'] == 4


def test_selected_skill_bodies_are_preserved_or_pause_when_window_is_full():
    assembler = ContextAssembler(context_window=900, output_tokens=160, overhead_tokens=40,
                                 counter=TokenCounter(lambda value: len(value)))
    skills = [{'name': 'verify', 'content': 'guidance ' * 40}]
    result = assembler.assemble({'goal': 'verify state', 'skills': skills,
                                'observation': {'snapshot': 'large snapshot ' * 200}})
    assert result.context['skills'] == skills
    block = next(block for block in result.manifest['blocks'] if block['name'] == 'skills')
    assert block['protected'] and block['trimmed_ids'] == []
    with pytest.raises(ContextWindowError) as raised:
        assembler.assemble({'skills': [{'name': 'verify', 'content': 'guidance ' * 200}]})
    assert 'skills' in raised.value.details['protected_blocks']


def test_memory_library_scopes_run_notes_and_requires_authorized_evidence(tmp_path):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    note = library.note('project-a', 'run-1', MemoryNote(kind='hypothesis', text='state is stale', evidence_refs=['ev-1']),
                        allowed_evidence_refs=['ev-1'], source_manifest='rev-a')
    assert note['run_id'] == 'run-1'
    assert library.working_memory('project-a', 'run-1')['hypothesis'][0]['text'] == 'state is stale'
    assert library.working_memory('project-a', 'run-2')['hypothesis'] == []
    with pytest.raises(PermissionError):
        library.note('project-a', 'run-1', {'text': 'bad', 'evidence_refs': ['other']}, allowed_evidence_refs=['ev-1'])


async def test_retriever_uses_sqlite_fallback_without_postgres(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    memory = MemoryLibrary(tmp_path / 'memory.sqlite3')
    retriever = Retriever(engine.store, engine.scopes, engine.context, fallback=memory)
    await retriever.index(engine.workspace, state.source_manifest)
    values = await retriever.retrieve('persisted', state.source_manifest, limit=3)
    assert values and json.loads(values[0]['content'])['path'] == 'src/value.ts'
    assert retriever.degradation()['reason'] == 'sqlite_fallback'
