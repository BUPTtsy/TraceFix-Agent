import json
import time
from types import SimpleNamespace

import pytest

from tracefix.knowledge.memory import MemoryLibrary
from tracefix.knowledge.retrieval import Retriever
from tracefix.knowledge.scope import ProjectContext
from tracefix.runtime.contracts import RunState, digest
from test_architecture_validation_repairs import bundle as validation_bundle


def _context():
    return ProjectContext('scope', (), (('scope', 1),), ())


def _state():
    return RunState(scope_id='scope', run_id='run', goal='persist', url='http://app:3000',
                    source_manifest='source', environment_digest='env', test_spec_hash='spec', patch_hash='patch')


def _upsert(library, key='item', **updates):
    record = {'id': key, 'scope_id': 'scope', 'layer': 'M3', 'kind': 'success_recipe',
              'revision': 1, 'source_revision': 'source', 'content': 'persistence refresh failure',
              'status': 'trusted'}
    record.update(updates)
    library.upsert(record)


def test_expired_revoked_wrong_scope_and_wildcard_experience_do_not_activate(tmp_path):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    _upsert(library, 'exact')
    _upsert(library, 'expired', expires_at=time.time() - 1)
    _upsert(library, 'wrong', scope_id='other')
    _upsert(library, 'wildcard', source_revision='*')
    _upsert(library, 'revoked')
    library.revoke('revoked', 'scope', 'invalid inverse behavior')
    results = library.retrieve('persistence', _context(), 'source', 'M3')
    assert [record['id'] for record in results] == ['exact']
    with library.connect() as connection:
        row = connection.execute("SELECT * FROM local_memory_items WHERE id='revoked'").fetchone()
    assert row['status'] == 'revoked' and row['revoked_reason']


def test_l1_old_excluded_becomes_probe_clue_and_view_is_bounded(tmp_path):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    library.note('scope', 'run', {'kind': 'excluded', 'text': 'API writes correct'},
                 source_manifest='old', patch_hash='old-patch')
    library.note('scope', 'run', {'kind': 'todo', 'text': 'check inverse refresh'})
    for index in range(15):
        library.note('scope', 'run', {'kind': 'progress', 'text': f'unrelated {index}'})
    view = library.working_memory('scope', 'run', query='API', source_manifest='source',
                                  patch_hash='patch', limit=3)
    assert view['excluded'] == []
    clue = view['finding'][0]
    assert clue['previous_kind'] == 'excluded' and clue['requires_probe']
    assert view['todo'][0]['text'] == 'check inverse refresh'
    assert sum(len(items) for items in view.values()) == 3


def test_cross_run_off_bypasses_actual_reads_writes_but_preserves_l1(tmp_path, monkeypatch):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    _upsert(library)
    library.save_job_memory('scope', 'job', {'text': 'persistence refresh'},
                            source_run_id='old', source_manifest='source')
    monkeypatch.setenv('TRACEFIX_CROSS_RUN_MEMORY', 'off')
    library.note('scope', 'run', {'text': 'current persistence'})
    assert library.candidate(_state(), 'persistence lesson', 'failure_recipe', 1) is None
    assert library.save_job_memory('scope', 'job', {'text': 'new'},
                                   source_run_id='run', source_manifest='source') is None
    assert library.job_memory('scope', 'job', 'source') == []
    assert library.retrieve('persistence', _context(), 'source', 'M3') == []
    assert len(library.search('persistence', scope_id='scope', run_id='run', job_id='job',
                              source_manifest='source')) == 1
    with library.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM job_memory').fetchone()[0] == 1
        assert connection.execute('SELECT COUNT(*) FROM local_memory_items').fetchone()[0] == 1


async def test_retriever_off_skips_database_and_embedding(tmp_path):
    class Bomb:
        async def encode(self, value):
            pytest.fail('off must bypass embedding')

        def execute(self, *args):
            pytest.fail('off must bypass history database')

    retriever = Retriever(SimpleNamespace(conn=Bomb()), SimpleNamespace(assert_current=lambda context: None),
                          _context(), embedding=Bomb(), cross_run=False)
    assert await retriever.retrieve('persistence', 'source', 'M3') == []
    assert retriever.candidate(_state(), 'lesson', 'success_recipe') is None
    assert retriever.clues('persistence', 'source') == []


def test_candidate_extracts_compact_bound_lesson_and_rejects_heldout(tmp_path):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    state = _state()
    report = {'summary': 'reload loses state', 'large_observation': 'unrelated ' * 3000,
              'evidence_refs': []}
    item = library.candidate(state, json.dumps(report), 'failure_recipe', 1)
    with library.connect() as connection:
        row = connection.execute('SELECT * FROM local_memory_items WHERE id=?', (item,)).fetchone()
    assert len(row['content']) < 500 and 'large_observation' not in row['content']
    assert row['patch_hash'] == 'patch' and row['environment_digest'] == 'env'
    with pytest.raises(PermissionError):
        library.candidate(state, {'source': 'held_out', 'summary': 'secret'}, 'success_recipe', 1)
    with pytest.raises(PermissionError):
        library.promote(item, 'scope', outcome='FIX_VERIFIED', approved=True, maintainer=True)


def test_compatibility_requires_current_public_probe_and_revocation_wins(tmp_path):
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    _upsert(library, source_revision='old')
    state = _state()
    probe = {'type': 'memory_compatibility_probe', 'passed': True,
             'scope_id': state.scope_id, 'run_id': state.run_id, 'source_manifest': state.source_manifest,
             'patch_hash': state.patch_hash, 'environment_digest': state.environment_digest,
             'test_spec_hash': state.test_spec_hash, 'evidence_refs': ['source.json']}
    artifacts = {'probe.json': probe, 'source.json': {'handler_hash': 'current'}}
    assert library.retrieve('persistence', _context(), 'source', 'M3', state=state, allow_compatible=True) == []
    library.mark_compatible('item', state, 'probe.json', artifact_exists=lambda ref: ref in artifacts,
                            artifact_read=artifacts.__getitem__)
    results = library.retrieve('persistence', _context(), 'source', 'M3', state=state, allow_compatible=True,
                               artifact_exists=lambda ref: ref in artifacts, artifact_read=artifacts.__getitem__)
    assert results[0]['applicability_status'] == 'compatible'
    artifacts['probe.json']['status'] = 'revoked'
    assert library.retrieve('persistence', _context(), 'source', 'M3', state=state, allow_compatible=True,
                            artifact_exists=lambda ref: ref in artifacts, artifact_read=artifacts.__getitem__) == []
    library.revoke('item', 'scope', 'probe no longer valid')
    assert library.retrieve('persistence', _context(), 'source', 'M3', state=state, allow_compatible=True) == []


def _postgres_recorder():
    calls = []

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return SimpleNamespace(fetchall=lambda: [])

    retriever = Retriever(SimpleNamespace(conn=Connection()),
                          SimpleNamespace(assert_current=lambda context: None), _context())
    return retriever, calls


async def test_postgres_expiry_and_revocation_filter(tmp_path):
    retriever, calls = _postgres_recorder()
    assert await retriever.retrieve('persistence', 'source', 'M3', state=_state()) == []
    sql, params = calls[0]
    assert "status='trusted'" in sql and 'expires_at>' in sql and 'source_revision=%s' in sql
    assert 'source' in params and '*' not in params


def _learning_bundle(validation_bundle):
    state, validations, artifacts = validation_bundle
    state.validation_refs = []
    for index, validation in enumerate(validations):
        ref = f'validation-{index}.json'
        artifacts[ref] = validation.model_dump(mode='json')
        state.validation_refs.append(ref)
    return state, artifacts


def test_promote_reloads_actual_public_artifacts_then_revoke_blocks_future_use(tmp_path, validation_bundle):
    state, artifacts = _learning_bundle(validation_bundle)
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    item = library.candidate(state, {'summary': 'stable persistence lesson'}, 'success_recipe', 1)
    reads = []

    def read(ref):
        reads.append(ref)
        return artifacts[ref]

    assert library.promote(item, state.scope_id, state=state,
        artifact_exists=lambda ref: ref in artifacts, artifact_read=read,
        artifact_read_bytes=artifacts.__getitem__) == item
    assert 'observation.json' in reads and 'spec.json' in reads
    result = library.retrieve('persistence', _context(), state.source_manifest, 'M3', state=state)
    assert result[0]['status'] == 'trusted'
    library.revoke(item, state.scope_id, 'reverse action failed later')
    assert library.retrieve('persistence', _context(), state.source_manifest, 'M3', state=state) == []


@pytest.mark.parametrize('fault', ['wrong_patch', 'failed_inverse', 'heldout', 'missing_png'])
def test_promotion_rejects_stale_incomplete_or_hidden_evidence(tmp_path, validation_bundle, fault):
    state, artifacts = _learning_bundle(validation_bundle)
    library = MemoryLibrary(tmp_path / 'memory.sqlite3')
    item = library.candidate(state, {'summary': 'lesson'}, 'success_recipe', 1)
    if fault == 'wrong_patch':
        state.patch_hash = 'different'
    elif fault == 'failed_inverse':
        artifacts['checkpoint-2.json']['passed'] = False
    elif fault == 'heldout':
        artifacts['original.json']['source'] = 'oracle'
    else:
        artifacts.pop('screenshot.png')
    with pytest.raises(PermissionError):
        library.promote(item, state.scope_id, state=state,
                        artifact_exists=lambda ref: ref in artifacts,
                        artifact_read=artifacts.__getitem__, artifact_read_bytes=artifacts.__getitem__)
    with library.connect() as connection:
        assert connection.execute('SELECT status FROM local_memory_items WHERE id=?', (item,)).fetchone()[0] == 'candidate'
