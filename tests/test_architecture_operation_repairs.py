import asyncio
import copy
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.config import Project
from tracefix.execution.repository import run_git, safe_commit, safe_index_files
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.memory import MemoryLibrary, MemoryNote
from tracefix.knowledge.scope import ScopeResolver
from tracefix.runtime.contracts import Phase, RunState, digest
from tracefix.runtime.effects import (EffectBoundaryError, file_resource, make_operation_executor,
                                     operation_identity, resources_intersect, run_effect)
from tracefix.runtime.tool_handlers import build_runtime_tools
from tracefix.storage.store import MemoryStore, PostgresStore, UnknownOperation


def state_for(run_id='run-r05', revision=0, scope='r05'):
    return RunState(run_id=run_id, scope_id=scope, goal='R05 regressions',
                    url='http://localhost', mode='repair', phase=Phase.DIAGNOSE, revision=revision)


def intent_for(resource='resource:a'):
    return {'tool_name': 'Write', 'arguments': {'value': 'updated'}, 'resources': [resource]}


def database_url():
    configured = os.getenv('TRACEFIX_DATABASE_URL')
    if configured:
        return configured
    environment = Path(__file__).resolve().parents[1] / '.env'
    if environment.is_file():
        for line in environment.read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('TRACEFIX_DATABASE_URL='):
                return line.split('=', 1)[1].strip().strip('"\'')
    return 'postgresql://tracefix:tracefix@127.0.0.1:55432/tracefix'


@pytest.fixture
def postgres_pair():
    import psycopg
    from psycopg import sql

    try:
        admin = psycopg.connect(database_url(), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip('Postgres TCP connection unavailable; isolated integration not executed')
    schema = 'r05_' + uuid.uuid4().hex
    assert schema.startswith('r05_') and len(schema) == 36
    stores = []
    created = False
    try:
        extension = admin.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n "
            "ON n.oid=e.extnamespace WHERE e.extname='vector'").fetchone()
        if not extension:
            pytest.skip('Existing pgvector extension required; public extension changes forbidden')
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        created = True
        for _index in range(2):
            store = PostgresStore(database_url())
            stores.append(store)
            store.conn.execute(sql.SQL('SET search_path TO {}, {}').format(
                sql.Identifier(schema), sql.Identifier(extension[0])))
            assert store.conn.execute('SELECT current_schema() AS name').fetchone()['name'] == schema
        yield stores
    finally:
        for store in stores:
            store.close()
        if created:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture(params=['memory', 'postgres'])
def ledger(request):
    if request.param == 'memory':
        return MemoryStore()
    store, _other = request.getfixturevalue('postgres_pair')
    store.setup()
    return store


def record_for(store, op_id):
    if isinstance(store, PostgresStore):
        return store.conn.execute('SELECT * FROM operations WHERE id=%s', (op_id,)).fetchone()
    return copy.deepcopy(store.operations[op_id])


@pytest.mark.parametrize('revision,new_run,new_call', [(0, False, False), (1, False, True),
                                                     (0, False, True), (0, True, True)])
def test_unknown_fence_survives_identity_changes(ledger, revision, new_run, new_call):
    original = state_for()
    ledger.save(original)
    intent = intent_for()
    ledger.begin(original, 'first', intent, owner='owner-first')
    ledger.mark_unknown(original, 'first', owner='owner-first', reason='timeout')
    changed = state_for('run-new' if new_run else original.run_id, revision)
    ledger.save(changed)
    with pytest.raises(UnknownOperation):
        ledger.begin(changed, 'new-call' if new_call else 'first', intent, owner='owner-next')
    assert record_for(ledger, 'first')['status'] == 'UNKNOWN'
    ledger.begin(changed, 'independent', intent_for('resource:b'), owner='independent-owner')
    ledger.finish(changed, 'independent', {'executed': True}, owner='independent-owner')


def test_ownership_epoch_and_missing_row_protection(ledger):
    state = state_for()
    ledger.save(state)
    ledger.begin(state, 'owned', intent_for(), owner='winner')
    for changed, operation, token in [(state, 'owned', 'loser'),
                                      (state_for(revision=1), 'owned', 'winner'),
                                      (state_for(run_id='different'), 'owned', 'winner'),
                                      (state_for(scope='different'), 'owned', 'winner'),
                                      (state, 'missing', 'winner')]:
        with pytest.raises(PermissionError):
            ledger.finish(changed, operation, {'executed': True}, owner=token)
    assert record_for(ledger, 'owned')['status'] == 'STARTED'
    ledger.finish(state, 'owned', {'executed': True}, owner='winner')
    with pytest.raises(PermissionError):
        ledger.finish(state, 'owned', {'executed': True}, owner='winner')
    with pytest.raises(PermissionError):
        ledger.begin(state_for(run_id='other'), 'owned', intent_for(), owner='other')
    assert ledger.begin(state, 'owned', intent_for(), owner='other') == {'executed': True}


def test_unknown_reconciliation_requires_identity_and_receipt(ledger):
    state = state_for()
    ledger.save(state)
    ledger.begin(state, 'uncertain', intent_for(), owner='original')
    ledger.mark_unknown(state, 'uncertain', owner='original', reason='external-cancel')
    record = record_for(ledger, 'uncertain')
    receipt = dict(operation_id='uncertain', scope_id=state.scope_id, run_id=state.run_id,
                   intent_hash=record['intent_hash'], resources=record['resources'],
                   outcome='not_applied', evidence='operator inspected destination',
                   execution_stopped=True,
                   result={'executed': False, 'isError': True})
    for updates in [{'scope_id': 'other'}, {'run_id': 'other'}, {'resources': ['resource:b']},
                    {'intent_hash': 'wrong'}, {'evidence': ''}, {'result': None},
                    {'execution_stopped': False},
                    {'result': {'executed': True}}]:
        with pytest.raises(PermissionError):
            ledger.reconcile(state, 'uncertain', {**receipt, **updates}, reviewer='operator')
    with pytest.raises(PermissionError):
        ledger.reconcile(state, 'uncertain', receipt, reviewer='')
    assert ledger.reconcile(state, 'uncertain', receipt, reviewer='operator')['executed'] is False
    ledger.begin(state, 'after-review', intent_for(), owner='next')


def test_legacy_started_is_conservatively_locked(ledger):
    state = state_for()
    ledger.save(state)
    if isinstance(ledger, PostgresStore):
        ledger.conn.execute("INSERT INTO operations(id,run_id,scope_id,intent_hash,status) "
                            "VALUES (%s,%s,%s,%s,'STARTED')",
                            ('legacy', state.run_id, state.scope_id, digest(intent_for())))
        ledger.setup()
    else:
        ledger.operations['legacy'] = dict(run_id=state.run_id, scope_id=state.scope_id,
            intent_hash=digest(intent_for()), status='STARTED', receipt=None)
    with pytest.raises(UnknownOperation):
        ledger.begin(state_for(revision=10), 'new-epoch', intent_for('resource:b'), owner='next')
    record = record_for(ledger, 'legacy')
    assert record['status'] == 'UNKNOWN' and record['resources'] == ['*']
    with pytest.raises(PermissionError):
        ledger.finish(state, 'legacy', {'executed': True}, owner='forged')


def test_historical_unknown_without_resources_migrates_and_can_be_reviewed(ledger):
    state = state_for()
    ledger.save(state)
    if isinstance(ledger, PostgresStore):
        ledger.conn.execute("INSERT INTO operations(id,run_id,scope_id,intent_hash,status) "
            "VALUES (%s,%s,%s,%s,'UNKNOWN')", ('legacy-unknown', state.run_id, state.scope_id,
                                            digest(intent_for())))
        ledger.setup()
    else:
        ledger.operations['legacy-unknown'] = dict(run_id=state.run_id, scope_id=state.scope_id,
            intent_hash=digest(intent_for()), status='UNKNOWN', receipt=None)
    with pytest.raises(UnknownOperation):
        ledger.begin(state, 'probe', intent_for('resource:b'), owner='next')
    record = record_for(ledger, 'legacy-unknown')
    assert record['resources'] == ['*']
    ledger.reconcile(state, 'legacy-unknown', dict(operation_id='legacy-unknown', run_id=state.run_id,
        scope_id=state.scope_id, intent_hash=record['intent_hash'], resources=['*'], outcome='not_applied',
        evidence='reviewed historic operation', execution_stopped=True,
        result={'executed': False}), reviewer='operator')
    ledger.begin(state, 'after-historical-review', intent_for(), owner='next')


@pytest.mark.parametrize('same_resource', [False, True])
def test_two_connection_or_memory_concurrent_admission(ledger, request, same_resource):
    state = state_for()
    ledger.save(state)
    stores = request.getfixturevalue('postgres_pair') if isinstance(ledger, PostgresStore) else [ledger, ledger]
    barrier = threading.Barrier(2)

    def attempt(index):
        barrier.wait(timeout=5)
        resource = 'resource:a' if same_resource else 'resource:' + str(index)
        try:
            stores[index].begin(state, 'op-' + str(index), intent_for(resource), owner='owner-' + str(index))
            return 'admitted'
        except UnknownOperation:
            return 'blocked'

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))
    assert outcomes.count('admitted') == (1 if same_resource else 2)
    for index, outcome in enumerate(outcomes):
        if outcome == 'admitted':
            stores[index].finish(state, 'op-' + str(index), {'executed': True}, owner='owner-' + str(index))


def test_postgres_old_table_migration_and_rowcount(postgres_pair):
    store, other = postgres_pair
    state = state_for()
    store.conn.execute('''CREATE TABLE operations(id text PRIMARY KEY,run_id text NOT NULL,
        scope_id text NOT NULL,intent_hash text NOT NULL,status text NOT NULL,receipt jsonb)''')
    store.conn.execute("INSERT INTO operations VALUES (%s,%s,%s,%s,'STARTED',NULL)",
                       ('old', state.run_id, state.scope_id, digest(intent_for())))
    store.setup()
    store.save(state)
    assert record_for(other, 'old')['status'] == 'UNKNOWN'
    assert record_for(other, 'old')['resources'] == ['*']
    record = record_for(store, 'old')
    store.reconcile(state, 'old', dict(operation_id='old', run_id=state.run_id, scope_id=state.scope_id,
        intent_hash=record['intent_hash'], resources=['*'], outcome='not_applied',
        evidence='manual inspection', execution_stopped=True, result={'executed': False}), reviewer='operator')
    store.begin(state, 'rowcount', intent_for(), owner='owner')
    store.conn.execute('''CREATE FUNCTION suppress_finish() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN RETURN NULL; END $$''')
    store.conn.execute('''CREATE TRIGGER suppress_finish BEFORE UPDATE ON operations FOR EACH ROW
        WHEN (NEW.status='DONE') EXECUTE FUNCTION suppress_finish()''')
    with pytest.raises(PermissionError, match='ownership'):
        store.finish(state, 'rowcount', {'executed': True}, owner='owner')
    assert record_for(other, 'rowcount')['status'] == 'STARTED'


def test_path_resource_intersection(tmp_path):
    root = file_resource(tmp_path)
    child = file_resource(tmp_path / 'source.py')
    peer = file_resource(tmp_path.parent / (tmp_path.name + '-peer'))
    assert resources_intersect([root], [child])
    assert not resources_intersect([root], [peer])
    assert resources_intersect(['*'], [peer])


def test_physical_file_fence_survives_scope_alias(ledger, tmp_path):
    state, alias = state_for(), state_for(run_id='alias-run', scope='alias-scope')
    ledger.save(state)
    ledger.save(alias)
    intent = intent_for(file_resource(tmp_path / 'shared.sqlite3'))
    ledger.begin(state, 'shared', intent, owner='owner')
    ledger.mark_unknown(state, 'shared', owner='owner', reason='cancel')
    with pytest.raises(UnknownOperation):
        ledger.begin(alias, 'alias', intent, owner='other')
    ledger.begin(alias, 'independent-alias', intent_for(file_resource(tmp_path / 'different')), owner='other')


def test_unidentified_resource_fence_cannot_be_bypassed_by_scope_alias(ledger):
    state, alias = state_for(), state_for(run_id='alias-run', scope='alias-scope')
    ledger.save(state)
    ledger.save(alias)
    ledger.begin(state, 'unknown-resource', intent_for('*'), owner='owner')
    ledger.mark_unknown(state, 'unknown-resource', owner='owner', reason='unknown external')
    with pytest.raises(UnknownOperation):
        ledger.begin(alias, 'scope-change', intent_for('resource:a'), owner='next')


def test_postgres_owner_cannot_be_borrowed_from_another_connection(postgres_pair):
    store, other = postgres_pair
    store.setup()
    state = state_for()
    store.save(state)
    store.begin(state, 'owned', intent_for())
    with pytest.raises(PermissionError):
        other.finish(state, 'owned', {'executed': True})
    store.finish(state, 'owned', {'executed': True})


def test_postgres_advisory_claim_lock_releases_at_transaction_end(postgres_pair):
    store, other = postgres_pair
    store.setup()
    state = state_for()
    store.save(state)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.conn.transaction():
            store._scope_lock(state.scope_id)
            future = executor.submit(other.begin, state, 'after-lock', intent_for(), owner='next')
            time.sleep(0.05)
            assert not future.done()
        assert future.result(timeout=3) is None
    other.finish(state, 'after-lock', {'executed': True}, owner='next')


async def test_default_owner_not_shared_by_two_async_tasks():
    store, state = MemoryStore(), state_for()
    store.save(state)
    store.begin(state, 'owned', intent_for())

    async def wrong_task():
        with pytest.raises(PermissionError):
            store.finish(state, 'owned', {'executed': True})

    await asyncio.create_task(wrong_task())
    store.finish(state, 'owned', {'executed': True})


def local_engine(tmp_path, files=None):
    root = tmp_path / 'code'
    root.mkdir()
    contents = {'source.py': 'baseline\n', **(files or {})}
    for relative, content in contents.items():
        (root / relative).write_bytes(content.encode('utf-8'))
    run_git(root, 'init', '-q', '--initial-branch=main')
    safe_index_files(root, list(contents))
    safe_commit(root, 'Operation fixture baseline', 'CI', 'ci@localhost')
    project = Project(id='r05', repo_id='ci', root=root, allowed_files=list(contents))
    scopes = ScopeResolver({'r05': project})
    workspace, snapshot = Workspace.export(scopes, scopes.context('r05'), 'HEAD', tmp_path / 'workspace')
    engine = SimpleNamespace(workspace=workspace, store=MemoryStore(), memory=None)
    state = state_for()
    state.source_manifest = digest(snapshot)
    engine.store.save(state)
    return engine, state, workspace.root / 'source.py'


async def test_spawn_local_production_handler_updates_parent_only(tmp_path):
    engine, state, source = local_engine(tmp_path)
    runtime = build_runtime_tools(engine, state, None)
    result = await runtime.pipeline().execute('Write', {'file_path': str(source), 'content': 'new\n'}, 'call-1')
    assert not result.is_error and result.executed
    assert engine.workspace.staged_content('source.py') == b'new\n'
    assert source.read_text(encoding='utf-8') == 'baseline\n'
    assert all(record['status'] == 'DONE' for record in engine.store.operations.values())
    assert not [worker for worker in multiprocessing.active_children() if worker.name == 'tracefix-r05-effect']


@pytest.mark.parametrize('changed', ['overlay', 'disk_with_overlay'])
async def test_parent_overlay_change_during_prepare_is_not_overwritten(tmp_path, monkeypatch, changed):
    import tracefix.runtime.local_tools as local_module

    engine, state, source = local_engine(tmp_path)
    if changed == 'disk_with_overlay':
        engine.workspace.stage('source.py', 'initial-overlay\n')
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(kind, payload, **kwargs):
        entered.set()
        await release.wait()
        return {'content': 'worker\n', 'before_hash': digest(payload['content'].encode('utf-8'))}

    monkeypatch.setattr(local_module, 'run_effect', prepare)
    runtime = build_runtime_tools(engine, state, None)
    pending = asyncio.create_task(runtime.pipeline().execute('Write', {
        'file_path': str(source), 'content': 'worker\n'}, 'call'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if changed == 'overlay':
            engine.workspace.stage('source.py', 'concurrent\n')
        else:
            source.write_text('concurrent-disk\n', encoding='utf-8')
    finally:
        release.set()
        result = await pending
    assert result.is_error and not result.executed
    expected = b'concurrent\n' if changed == 'overlay' else b'initial-overlay\n'
    assert engine.workspace.staged_content('source.py') == expected


def _late_worker(request, folder):
    directory = Path(folder)
    if os.name != 'nt':
        os.setsid()
    (directory / 'ready').touch()
    while not (directory / 'start').exists():
        time.sleep(0.005)
    payload = request['payload']
    subprocess.Popen([sys.executable, '-c',
        'import pathlib,time,sys; time.sleep(1.8); pathlib.Path(sys.argv[1]).write_text("late")',
        payload['descendant']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(payload['entered']).touch()
    if payload.get('normal_exit'):
        (directory / 'receipt.json').write_text(json.dumps({'nonce': request['nonce'], 'result': 'done'}))
        return
    time.sleep(5)
    Path(payload['late']).write_text('late')


def _broken_receipt_worker(request, folder):
    directory = Path(folder)
    if os.name != 'nt':
        os.setsid()
    (directory / 'ready').touch()
    while not (directory / 'start').exists():
        time.sleep(0.005)
    mode = request['payload']['mode']
    if mode == 'crash':
        os._exit(17)
    if mode == 'partial':
        (directory / 'receipt.part').write_text('{')
    elif mode == 'corrupt':
        (directory / 'receipt.json').write_text('{')
    elif mode == 'identity':
        (directory / 'receipt.json').write_text(json.dumps({'nonce': 'wrong', 'result': {}}))
    else:
        (directory / 'receipt.json').write_text(json.dumps({'nonce': request['nonce']}))


@pytest.mark.parametrize('mode', ['crash', 'partial', 'corrupt', 'identity', 'missing-result'])
async def test_worker_crash_and_ipc_errors_are_bounded_and_unknown(tmp_path, monkeypatch, mode):
    import tracefix.runtime.effects as effects

    monkeypatch.setattr(effects, '_effect_worker', _broken_receipt_worker)
    before = time.monotonic()
    with pytest.raises(EffectBoundaryError):
        await run_effect('local.prepare', {'mode': mode}, timeout_s=3)
    assert time.monotonic() - before < 4
    assert not [worker for worker in multiprocessing.active_children() if worker.name == 'tracefix-r05-effect']


@pytest.mark.parametrize('mode', ['normal', 'timeout', 'cancel'])
async def test_worker_and_descendant_exit_confirmed_on_all_paths(tmp_path, monkeypatch, mode):
    import tracefix.runtime.effects as effects

    monkeypatch.setattr(effects, '_effect_worker', _late_worker)
    payload = dict(entered=str(tmp_path / 'entered'), late=str(tmp_path / 'late'),
                   descendant=str(tmp_path / 'descendant'), normal_exit=mode == 'normal')
    task = asyncio.create_task(run_effect('local.prepare', payload, timeout_s=1.0 if mode == 'timeout' else 10))
    async with asyncio.timeout(5):
        while not (tmp_path / 'entered').exists():
            if task.done():
                await task
            await asyncio.sleep(0.005)
    if mode == 'normal':
        assert await task == 'done'
    elif mode == 'timeout':
        with pytest.raises(TimeoutError):
            await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not [worker for worker in multiprocessing.active_children() if worker.name == 'tracefix-r05-effect']
    await asyncio.sleep(2)
    assert not (tmp_path / 'descendant').exists() and not (tmp_path / 'late').exists()


@pytest.mark.parametrize('cancel', [False, True])
async def test_production_timeout_cancel_blocks_new_call_and_epoch(tmp_path, cancel):
    engine, state, source = local_engine(tmp_path)
    engine.memory = MemoryLibrary(tmp_path / 'memory.sqlite3')
    runtime = build_runtime_tools(engine, state, None)
    object.__setattr__(runtime.registry.get('memory.note', state.phase), 'timeout_s', 0.6 if not cancel else 20)
    connection = sqlite3.connect(engine.memory.path, timeout=1)
    connection.execute('BEGIN IMMEDIATE')
    task = asyncio.create_task(runtime.pipeline().execute('memory.note', {'text': 'late'}, 'first-call'))
    try:
        if cancel:
            await asyncio.sleep(0.4)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            from tracefix.runtime.tools import ToolOperationUnknown
            with pytest.raises(ToolOperationUnknown):
                await task
        assert next(iter(engine.store.operations.values()))['status'] == 'UNKNOWN'
    finally:
        connection.rollback()
        connection.close()
    for changed in [state, state.model_copy(update={'revision': 1}),
                    state.model_copy(update={'run_id': 'new-run'})]:
        engine.store.save(changed)
        with pytest.raises(UnknownOperation):
            await build_runtime_tools(engine, changed, None).pipeline().execute(
                'memory.note', {'text': 'different arguments'}, 'different-call')
    result = await build_runtime_tools(engine, state, None).pipeline().execute(
        'Write', {'file_path': str(source), 'content': 'independent\n'}, 'independent-call')
    assert not result.is_error
    await asyncio.sleep(0.7)
    assert engine.memory.working_memory(state.scope_id, state.run_id)['finding'] == []


async def test_parent_apply_requires_live_owned_fence(tmp_path, monkeypatch):
    import tracefix.runtime.local_tools as local_module

    engine, state, source = local_engine(tmp_path)

    async def prepare(kind, payload, **kwargs):
        operation_id, record = next(iter(engine.store.operations.items()))
        engine.store.mark_unknown(state, operation_id, owner=record['owner_token'], reason='lost boundary')
        return {'content': 'worker\n', 'before_hash': digest(payload['content'].encode('utf-8'))}

    monkeypatch.setattr(local_module, 'run_effect', prepare)
    with pytest.raises(EffectBoundaryError):
        await build_runtime_tools(engine, state, None).pipeline().execute('Write', {
            'file_path': str(source), 'content': 'worker\n'}, 'call')
    assert engine.workspace.staged_content('source.py') is None
    assert next(iter(engine.store.operations.values()))['status'] == 'UNKNOWN'


async def test_memory_production_note_persists_child_receipt(tmp_path):
    engine, state, _source = local_engine(tmp_path)
    engine.memory = MemoryLibrary(tmp_path / 'memory.sqlite3')
    result = await build_runtime_tools(engine, state, None).pipeline().execute(
        'memory.note', {'kind': 'progress', 'text': 'confirmed note'}, 'note-call')
    assert not result.is_error
    assert engine.memory.working_memory(state.scope_id, state.run_id)['progress'][0]['text'] == 'confirmed note'
    assert next(iter(engine.store.operations.values()))['resources'] == [file_resource(engine.memory.path)]


@pytest.mark.parametrize('cancel', [False, True])
async def test_locked_sqlite_write_timeout_or_cancel_exits_before_return(tmp_path, cancel):
    database = tmp_path / 'memory.sqlite3'
    memory = MemoryLibrary(database)
    connection = sqlite3.connect(database, timeout=1)
    connection.execute('BEGIN IMMEDIATE')
    payload = dict(path=str(database), scope_id='r05', run_id='run-r05',
        note=MemoryNote(kind='progress', text='late write forbidden').model_dump(mode='json'),
        allowed_evidence_refs=[], source_manifest='')
    before = time.monotonic()
    try:
        if cancel:
            task = asyncio.create_task(run_effect('memory.note', payload, timeout_s=20))
            await asyncio.sleep(0.4)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await run_effect('memory.note', payload, timeout_s=0.5)
        assert time.monotonic() - before < 4
        assert not [worker for worker in multiprocessing.active_children() if worker.name == 'tracefix-r05-effect']
    finally:
        connection.rollback()
        connection.close()
    await asyncio.sleep(0.7)
    assert memory.working_memory('r05', 'run-r05')['progress'] == []


async def test_external_cancel_and_auto_reconcile_keep_unknown_fence():
    store, state = MemoryStore(), state_for()
    store.save(state)
    operation = make_operation_executor(store, state)
    called = asyncio.Event()

    async def external():
        called.set()
        await asyncio.sleep(20)

    task = asyncio.create_task(operation('external', intent_for(), external))
    await called.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(store.operations.values()))['status'] == 'UNKNOWN'
    with pytest.raises(UnknownOperation) as failure:
        await operation('external', intent_for(), external, reconcile=lambda: {'executed': True})
    assert 'store.reconcile' in failure.value.details['recovery']
    with pytest.raises(UnknownOperation):
        await make_operation_executor(store, state_for(revision=1))('external', intent_for(), external)


async def test_initial_patch_reconcile_does_not_disable_first_execution_and_manual_recovery(ledger):
    state = state_for()
    ledger.save(state)
    operation = make_operation_executor(ledger, state)
    performed = []

    async def patch():
        performed.append('patch')
        return {'patch_ref': 'confirmed', 'executed': True}

    assert await operation('workspace.patch', intent_for(), patch, reconcile=lambda: None) == {
        'patch_ref': 'confirmed', 'executed': True}
    assert performed == ['patch']
    failed_intent = intent_for('resource:failed-patch')

    async def failed_patch():
        raise RuntimeError('lost receipt')

    with pytest.raises(RuntimeError):
        await operation('workspace.patch', failed_intent, failed_patch)
    op_id = operation_identity(state, 'workspace.patch', failed_intent)
    record = record_for(ledger, op_id)
    ledger.reconcile(state, op_id, dict(operation_id=op_id, scope_id=state.scope_id,
        run_id=state.run_id, intent_hash=record['intent_hash'], resources=record['resources'],
        outcome='completed', evidence='operator verified complete content hashes',
        execution_stopped=True,
        result={'patch_ref': 'verified-after-failure', 'executed': True}), reviewer='operator')
    assert (await operation('workspace.patch', failed_intent, patch, reconcile=lambda: None))[
        'patch_ref'] == 'verified-after-failure'
    assert performed == ['patch']


async def test_nested_delegation_can_write_inside_parent_fence_without_opening_it(ledger):
    parent, child = state_for(), state_for(run_id='child')
    ledger.save(parent)
    ledger.save(child)
    entered, release = asyncio.Event(), asyncio.Event()

    async def child_write():
        entered.set()
        await release.wait()
        return {'executed': True}

    async def delegate():
        return await make_operation_executor(ledger, child)('Write', intent_for(), child_write)

    pending = asyncio.create_task(make_operation_executor(ledger, parent)('agent.delegate', intent_for(), delegate))
    await entered.wait()
    with pytest.raises(UnknownOperation):
        ledger.begin(parent, 'unrelated-caller', intent_for(), owner='outsider')
    release.set()
    assert (await pending)['executed'] is True


async def test_executor_pins_epoch_while_handler_updates_state(ledger):
    state = state_for()
    ledger.save(state)

    async def perform():
        state.revision += 1
        return {'executed': True}

    assert (await make_operation_executor(ledger, state)('Write', intent_for(), perform))['executed'] is True


async def test_completed_stable_identity_never_replays_across_revision_continuation_or_call(ledger):
    state = state_for()
    ledger.save(state)
    performed = []

    async def perform():
        performed.append('effect')
        return {'executed': True, 'receipt': 'once'}

    for revision, continuation in [(0, 0), (1, 0), (2, 1), (0, 3)]:
        changed = state.model_copy(update={'revision': revision, 'continuation_count': continuation})
        result = await make_operation_executor(ledger, changed)(
            'Write', intent_for(), perform, idempotency_key='explicit-stable-write')
        assert result == {'executed': True, 'receipt': 'once'}
    assert performed == ['effect']


@pytest.mark.parametrize('name', ['browser.action', 'scenario.reset', 'sandbox.verify'])
async def test_done_actions_allow_intentional_new_epoch_without_replaying_same_epoch(ledger, name):
    original = state_for()
    ledger.save(original)
    performed = []

    async def perform():
        performed.append('action')
        return {'executed': True, 'sequence': len(performed)}

    intent = intent_for('resource:browser')
    operation = make_operation_executor(ledger, original)
    assert (await operation(name, intent, perform))['sequence'] == 1
    assert (await operation(name, intent, perform))['sequence'] == 1
    for sequence, changed in enumerate([original.model_copy(update={'revision': 1}),
        original.model_copy(update={'revision': 1, 'continuation_count': 1})], start=2):
        assert (await make_operation_executor(ledger, changed)(name, intent, perform))['sequence'] == sequence
    assert len(performed) == 3
    operation = make_operation_executor(ledger, original)
    for sequence, position in enumerate([{'phase': 'EXPLORE', 'replay_index': 1},
                                        {'phase': 'EXPLORE', 'replay_index': 2},
                                        {'phase': 'VALIDATE', 'replay_index': 1}], start=4):
        positioned_intent = {**intent, 'execution': position}
        assert (await operation(name, positioned_intent, perform))['sequence'] == sequence
        assert (await operation(name, positioned_intent, perform))['sequence'] == sequence
    assert len(performed) == 6


async def test_spawn_edit_and_notebook_handlers_apply_parent_receipts(tmp_path):
    notebook = json.dumps({'cells': [{'cell_type': 'code', 'source': ['old'],
        'metadata': {'preserve': True}, 'outputs': [], 'execution_count': None, 'id': 'existing'}]})
    engine, state, source = local_engine(tmp_path, {'analysis.ipynb': notebook})
    notebook_path = engine.workspace.root / 'analysis.ipynb'
    pipeline = build_runtime_tools(engine, state, None).pipeline()
    edited = await pipeline.execute('Edit', {'file_path': str(source), 'old_string': 'baseline',
                                           'new_string': 'edited'}, 'edit')
    assert not edited.is_error
    assert engine.workspace.staged_content('source.py') == source.read_bytes().replace(b'baseline', b'edited')
    replaced = await pipeline.execute('NotebookEdit', {'notebook_path': str(notebook_path),
        'cell_number': 0, 'new_source': 'new'}, 'notebook')
    assert not replaced.is_error
    staged = engine.workspace.staged_content('analysis.ipynb')
    assert staged.endswith(b'\n')
    cell = json.loads(staged)['cells'][0]
    assert cell['source'] == ['new'] and cell['metadata'] == {'preserve': True}
    assert cell['id'] == 'existing'


async def test_unknown_actions_do_not_admit_new_epoch_or_sequence(ledger):
    original = state_for()
    ledger.save(original)

    async def failed():
        raise RuntimeError('remote cancellation cannot be proved')

    with pytest.raises(RuntimeError):
        await make_operation_executor(ledger, original)('browser.action', intent_for('resource:browser'), failed)
    for changed, action in [(original, {'sequence': 2}),
                            (original.model_copy(update={'revision': 1}), {'sequence': 1}),
                            (original.model_copy(update={'continuation_count': 1}), {'sequence': 1})]:
        with pytest.raises(UnknownOperation):
            await make_operation_executor(ledger, changed)(
                'browser.action', {**intent_for('resource:browser'), **action}, failed)


async def test_production_completed_call_id_rebinding_does_not_replay_new_epoch(tmp_path):
    engine, state, source = local_engine(tmp_path)
    arguments = {'file_path': str(source), 'content': 'once\n'}
    first = await build_runtime_tools(engine, state, None).pipeline().execute('Write', arguments, 'first-call')
    assert first.call_id == 'first-call'
    engine.workspace.stage('source.py', 'must-survive-replay\n')
    changed = state.model_copy(update={'revision': 4, 'continuation_count': 2})
    second = await build_runtime_tools(engine, changed, None).pipeline().execute('Write', arguments, 'new-call')
    assert second.call_id == 'new-call' and not second.is_error
    assert engine.workspace.staged_content('source.py') == b'must-survive-replay\n'
    assert len(engine.store.operations) == 1


def test_owned_crashed_started_requires_exit_identity_then_manual_reconcile(ledger):
    original, resumed = state_for(), state_for(revision=7)
    ledger.save(original)
    ledger.begin(original, 'crashed-owned', intent_for(), owner='crashed-owner')
    record = record_for(ledger, 'crashed-owned')
    recovery = dict(operation_id='crashed-owned', scope_id=record['scope_id'], run_id=record['run_id'],
        intent_hash=record['intent_hash'], resources=record['resources'], owner_token=record['owner_token'],
        epoch=record['epoch'], execution_stopped=True, evidence='operator verified process exit')
    for updates in [{'owner_token': 'wrong'}, {'epoch': 'wrong'}, {'execution_stopped': False},
                    {'evidence': ''}, {'resources': ['other']}]:
        with pytest.raises(PermissionError):
            ledger.recover_started(resumed, 'crashed-owned', {**recovery, **updates}, reviewer='operator')
    with pytest.raises(UnknownOperation):
        ledger.begin(resumed, 'new-call', intent_for(), owner='new')
    ledger.recover_started(resumed, 'crashed-owned', recovery, reviewer='operator')
    assert record_for(ledger, 'crashed-owned')['status'] == 'UNKNOWN'
    with pytest.raises(PermissionError):
        ledger.finish(original, 'crashed-owned', {'executed': True}, owner='crashed-owner')
    with pytest.raises(UnknownOperation):
        ledger.begin(resumed, 'another-call', intent_for(), owner='new')
    ledger.reconcile(resumed, 'crashed-owned', {**recovery, 'outcome': 'not_applied',
        'result': {'executed': False}}, reviewer='operator')
    ledger.begin(resumed, 'verified-retry', intent_for(), owner='next')


async def test_unserializable_request_and_rejected_worker_leave_no_worker():
    with pytest.raises(TypeError):
        await run_effect('local.prepare', {'callback': lambda: None})
    from tracefix.runtime.tools import ToolRejected
    with pytest.raises(ToolRejected):
        await run_effect('local.prepare', {'tool': 'Edit', 'content': 'baseline',
            'arguments': {'old_string': 'missing', 'new_string': 'new', 'replace_all': False}})
    assert not [worker for worker in multiprocessing.active_children() if worker.name == 'tracefix-r05-effect']
