"""Harness M3 候选 A/B 交接的边界回归。"""

import os
from copy import deepcopy

import pytest

from tracefix.execution.candidate_export import SDKWorkspace, export_candidate, materialize_candidate
from test_sdk_workspace import sdk_workspace as _sdk_workspace_fixture
from tracefix.runtime.contracts import digest


@pytest.fixture
def sdk_workspace(tmp_path):
    return _sdk_workspace_fixture.__wrapped__(tmp_path)


def _export(sdk, authority):
    return export_candidate(sdk, authority, summary='候选', evidence_refs=['frozen-evidence'])


def test_candidate_b_rebuilds_from_frozen_input_without_a_contamination(sdk_workspace, tmp_path):
    authority, sdk = sdk_workspace
    original_source_identity = sdk.authority_snapshot['source_tree']
    (sdk.source / 'src/app.py').write_bytes(b'value = 2\n')
    (sdk.source / 'src/a-only.py').write_bytes(b'a = True\n')
    candidate_a = _export(sdk, authority)
    workspace_a = materialize_candidate(sdk, candidate_a, tmp_path / 'candidate-a')
    workspace_a.apply(candidate_a)
    assert workspace_a.read('src/a-only.py') == 'a = True\n'

    (sdk.source / 'src/app.py').write_bytes(b'value = 1\n')
    (sdk.source / 'src/a-only.py').unlink()
    (sdk.source / 'src/b-only.py').write_bytes(b'b = True\n')
    candidate_b = _export(sdk, authority)
    workspace_b = materialize_candidate(sdk, candidate_b, tmp_path / 'candidate-b')
    workspace_b.apply(candidate_b)

    assert workspace_b.read('src/app.py') == 'value = 1\n'
    assert workspace_b.read('src/b-only.py') == 'b = True\n'
    with pytest.raises(FileNotFoundError):
        workspace_b.read('src/a-only.py')
    assert sdk.authority_snapshot['source_tree'] == original_source_identity
    assert workspace_b.repository_snapshot['source_tree'] == original_source_identity
    assert workspace_b.head() == authority.head()


def test_loaded_invocation_keeps_source_identity_and_previous_session_changes(sdk_workspace):
    authority, sdk = sdk_workspace
    loaded = SDKWorkspace.load(sdk.execution_dir, authority)
    (loaded.source / 'src/app.py').write_bytes(b'value = 2\n')
    invocation = loaded.fork_execution('feedback-execution')
    restored = SDKWorkspace.load(invocation.execution_dir, authority)
    assert restored.source == loaded.source
    assert restored.source.joinpath('src/app.py').read_bytes() == b'value = 2\n'
    assert restored.authority_snapshot['source_tree'] == sdk.authority_snapshot['source_tree']


def test_stable_path_swap_preserves_frozen_manifest_for_feedback_candidate(sdk_workspace, tmp_path):
    authority, sdk = sdk_workspace
    frozen_source = deepcopy(authority.repository_snapshot)
    frozen_hash = digest(frozen_source)
    stable = authority.root
    for name, value in [('a', b'value = 2\n'), ('b', b'value = 3\n')]:
        (sdk.source / 'src/app.py').write_bytes(value)
        candidate = _export(sdk, authority)
        fresh = materialize_candidate(sdk, candidate, tmp_path / ('fresh-' + name))
        assert fresh.head() == frozen_source['workspace_repository']['head']
        archive = tmp_path / ('archive-' + name)
        os.replace(stable, archive)
        os.replace(fresh.root, stable)
        fresh.root = stable
        fresh.repository_snapshot = deepcopy(frozen_source)
        fresh.check_frozen(frozen_source)
        fresh.apply(candidate)
        authority = fresh
        authority.check_frozen(frozen_source)
        assert authority.read('src/app.py').encode() == value
        assert digest(authority.repository_snapshot) == frozen_hash
        sdk = SDKWorkspace.load(sdk.execution_dir, authority)
        assert (sdk.baseline / 'src/app.py').read_bytes() == b'value = 1\n'
