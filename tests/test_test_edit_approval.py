"""人工测试授权的来源、时序与恢复绑定。"""
from types import SimpleNamespace

import pytest

from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import Phase, RunState, digest
from tracefix.runtime.test_edit_approval import (authorize_test_edits, restore_test_edit_approval,
                                               verified_test_edit_files)
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.store import MemoryStore


@pytest.fixture
def context(tmp_path):
    root = tmp_path / 'workspace'
    (root / 'src').mkdir(parents=True)
    (root / 'src/value.test.ts').write_text('assert(value);\n', encoding='utf-8')
    workspace = Workspace(root, ['src/**'])
    store, artifacts = MemoryStore(), Artifacts(tmp_path / 'artifacts')
    state = RunState(scope_id='project', goal='修正已明确批准的测试错误', mode='repair', url='http://app:3000',
                     source_manifest=digest({'files': ['src/value.test.ts']}),
                     repo_snapshot_ref='0001_source.json')
    store.save(state)
    return SimpleNamespace(workspace=workspace, store=store, artifacts=artifacts), state


def authorize(engine, state, files=('src/value.test.ts',)):
    return authorize_test_edits(engine.store, engine.artifacts, state, engine.workspace,
                                list(files), reviewer='cli:human-reviewer')


def test_only_trusted_consumed_approval_restores_exact_test_file(context):
    engine, state = context
    with pytest.raises(PermissionError):
        engine.workspace.path('src/value.test.ts', write=True)
    authorize(engine, state)
    assert engine.workspace.path('src/value.test.ts', write=True).is_file()
    resumed = engine.store.load(state.run_id, state.scope_id)
    resumed.test_spec_ref, resumed.test_spec_hash = '0002_spec.json', digest({'frozen': True})
    resumed.phase, resumed.patch_hash = Phase.VERIFY, digest(b'candidate')
    engine.workspace.approved_test_files = ()
    restore_test_edit_approval(engine, resumed)
    assert engine.workspace.approved_test_files == ('src/value.test.ts',)
    with pytest.raises(PermissionError):
        engine.workspace.path('src/other.test.ts', write=True)


@pytest.mark.parametrize('damage', ['source', 'scope', 'files', 'approval', 'decision', 'consumed', 'artifact'])
def test_restoration_rejects_tampered_or_missing_evidence(context, damage):
    engine, state = context
    authorize(engine, state)
    if damage == 'source':
        state.source_manifest = digest({'other': True})
    elif damage == 'scope':
        state.scope_id = 'other-project'
    elif damage == 'files':
        state.approved_test_files.append('src/other.test.ts')
    elif damage == 'approval':
        state.test_edit_approval_id = 'req_forged'
    elif damage == 'decision':
        engine.store.approvals[state.test_edit_approval_id]['decision'] = 'reject'
    elif damage == 'consumed':
        engine.store.approvals[state.test_edit_approval_id]['consumed'] = False
    else:
        engine.artifacts._path(state.scope_id, state.run_id, state.test_edit_approval_ref).write_bytes(b'{}')
    with pytest.raises((PermissionError, ValueError, FileNotFoundError)):
        verified_test_edit_files(engine.store, engine.artifacts, state, engine.workspace)


@pytest.mark.parametrize('path', ['src/**', '../outside.test.ts', 'src/oracle/test.ts',
                                  'src/__snapshots__/test.snap', 'src/tests/yarn.lock',
                                  '.github/test.yml', 'outside/value.test.ts', 'src/value.ts'])
def test_approval_cannot_expand_absolute_protection_or_project_scope(context, path):
    engine, state = context
    with pytest.raises((PermissionError, ValueError)):
        authorize(engine, state, [path])
    assert not state.test_edit_approval_ref
    assert engine.workspace.approved_test_files == ()


def test_frozen_spec_and_bare_model_file_list_cannot_authorize(context):
    engine, state = context
    state.test_spec_ref = '0002_spec.json'
    with pytest.raises(PermissionError, match='冻结前'):
        authorize(engine, state)
    state.approved_test_files = ['src/value.test.ts']
    with pytest.raises(PermissionError, match='不完整'):
        restore_test_edit_approval(engine, state)


def test_default_run_has_no_new_write_authority(context):
    engine, state = context
    assert verified_test_edit_files(engine.store, engine.artifacts, state, engine.workspace) == ()
    assert not engine.store.approvals
