import os

import pytest

from test_sdk_workspace import sdk_workspace
from tracefix.execution.candidate_export import (
    SDKCandidateRejected, export_candidate, materialize_candidate,
)


def proposal(sdk, authority):
    return export_candidate(sdk, authority, summary='修复', evidence_refs=['evidence'])


def test_actual_edits_export_existing_patch_contract_and_rebuild(sdk_workspace, tmp_path):
    authority, sdk = sdk_workspace
    (sdk.source / 'src/app.py').write_bytes(b'value = 2\r\n')
    (sdk.source / 'src/new.py').write_bytes(b'new_value = 3\n')
    patch = proposal(sdk, authority)
    assert {edit.operation for edit in patch.edits} == {'create', 'modify'}
    validation = materialize_candidate(sdk, patch, tmp_path / 'validation')
    assert validation.head() == authority.head()
    assert validation.repository_snapshot['workspace_repository']['origin'] == authority.repository_snapshot['workspace_repository']['origin']
    validation.apply(patch)
    assert validation.read('src/app.py') == 'value = 2\r\n'
    validation.check_frozen(validation.repository_snapshot)
    assert authority.read('src/app.py') == 'value = 1\n'


@pytest.mark.parametrize('relative,content', [
    ('tests/test_app.py', 'assert True\n'), ('.gitignore', '*\n'),
    ('package-lock.json', '{"changed": true}'), ('outside.py', 'value=2\n'),
    ('src/.hidden.py', 'value=2\n'), ('src/app.py', '\x00binary'),
])
def test_rejects_unauthorized_frozen_hidden_and_binary_changes(sdk_workspace, relative, content):
    authority, sdk = sdk_workspace
    (sdk.source / relative).write_text(content, encoding='utf-8')
    with pytest.raises((PermissionError, ValueError)):
        proposal(sdk, authority)
    assert authority.read('src/app.py') == 'value = 1\n'


def test_rejects_deletion_and_hardlinks(sdk_workspace):
    authority, sdk = sdk_workspace
    original = sdk.source / 'src/app.py'
    original.unlink()
    with pytest.raises(SDKCandidateRejected, match='删除'):
        proposal(sdk, authority)
    original.write_text('value = 2\n', encoding='utf-8')
    os.link(original, sdk.source / 'src/link.py')
    with pytest.raises(SDKCandidateRejected, match='链接'):
        proposal(sdk, authority)


def test_rejects_mode_changes(sdk_workspace):
    authority, sdk = sdk_workspace
    original = sdk.source / 'src/app.py'
    original.chmod(0o444)
    try:
        with pytest.raises(SDKCandidateRejected, match='模式'):
            proposal(sdk, authority)
    finally:
        original.chmod(0o666)


def test_candidate_b_rematerializes_input_and_reverts_a(sdk_workspace, tmp_path):
    authority, sdk = sdk_workspace
    (sdk.source / 'src/app.py').write_bytes(b'value = 2\n')
    (sdk.source / 'src/new.py').write_bytes(b'new = 3\n')
    first = proposal(sdk, authority)
    first_copy = materialize_candidate(sdk, first, tmp_path / 'candidate-a')
    first_copy.apply(first)
    (sdk.source / 'src/app.py').write_bytes((sdk.baseline / 'src/app.py').read_bytes())
    second = proposal(sdk, authority)
    second_copy = materialize_candidate(sdk, second, tmp_path / 'candidate-b')
    second_copy.apply(second)
    assert second_copy.read('src/app.py') == 'value = 1\n'
    assert first_copy.read('src/app.py') == 'value = 2\n'
    assert [edit.path for edit in second.edits] == ['src/new.py']
