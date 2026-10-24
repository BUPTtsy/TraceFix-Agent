import json
import pytest

from tracefix.execution.candidate_export import SDKWorkspace, SDKCandidateRejected
from tracefix.execution.repository import file_manifest, repository_binding, safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace, git
from tracefix.runtime.contracts import digest


@pytest.fixture
def sdk_workspace(tmp_path):
    root = tmp_path / 'authority'
    root.mkdir()
    for relative, text in {'src/app.py': 'value = 1\n', 'tests/test_app.py': 'assert False\n',
                           '.gitignore': 'build/\n', 'package-lock.json': '{}\n'}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
    git(root, 'init', '-q', '--initial-branch=main')
    files = {path.relative_to(root).as_posix(): digest(path.read_bytes())
             for path in root.rglob('*') if path.is_file() and '.git' not in path.parts}
    safe_index_files(root, sorted(files))
    commit_workspace(root, '测试冻结源码')
    authority = Workspace(root, ['src/**', 'tests/**', '.gitignore', 'package-lock.json'])
    authority.prepare_mountpoints(create=True)
    binding = repository_binding(root)
    authority.repository_snapshot = {'repository_version': 1, 'scope_id': 'scope', 'repo_id': 'repo',
        'source_root': str(root), 'source_repository': binding, 'source_tree': binding['tree'],
        'workspace_root': str(root), 'workspace_repository': binding, 'commit': authority.head(),
        'files': files, 'entries': file_manifest(root), 'subdir': ''}
    return authority, SDKWorkspace.create(authority, tmp_path / 'execution')


def test_sdk_copy_is_persistent_and_excludes_host_metadata(sdk_workspace):
    authority, sdk = sdk_workspace
    assert not (sdk.source / '.git').exists()
    assert (sdk.source / 'tests/test_app.py').read_bytes() == b'assert False\n'
    assert sdk.execution_dir.is_dir()
    assert (sdk.execution_dir / 'input_manifest.json').is_file()
    assert sdk.freeze() == sdk.input_snapshot
    assert authority.read('src/app.py') == 'value = 1\n'


def test_full_scan_finds_untracked_hidden_deleted_and_mode_changes(sdk_workspace):
    _, sdk = sdk_workspace
    (sdk.source / 'src/app.py').unlink()
    (sdk.source / '.hidden').write_text('hidden', encoding='utf-8')
    (sdk.source / 'src/new.py').write_text('new', encoding='utf-8')
    frozen = sdk.freeze()
    assert 'src/app.py' not in frozen.entries
    assert '.hidden' in frozen.entries
    assert 'src/new.py' in frozen.entries
    assert json.loads((sdk.execution_dir / 'output_manifest.json').read_text())['content_hash'] == frozen.content_hash


def test_input_damage_and_baseline_exclusions_are_rejected(sdk_workspace, tmp_path):
    authority, sdk = sdk_workspace
    with pytest.raises(SDKCandidateRejected, match='排除规则'):
        SDKWorkspace.create(authority, tmp_path / 'excluded', exclusions=['src/**'])
    (sdk.baseline / 'src/app.py').write_text('tampered', encoding='utf-8')
    with pytest.raises(SDKCandidateRejected, match='损坏'):
        sdk.check_input()


def test_load_and_new_invocation_preserve_source_and_session_config(sdk_workspace):
    authority, sdk = sdk_workspace
    loaded = SDKWorkspace.load(sdk.execution_dir, authority)
    (loaded.source / 'src/app.py').write_bytes(b'value = 2\n')
    config = loaded.execution_dir / 'sdk-config' / 'session.json'
    config.write_text('{}', encoding='utf-8')
    invocation = loaded.fork_execution('execution-2')
    restored = SDKWorkspace.load(invocation.execution_dir, authority)
    assert restored.source == loaded.source
    assert restored.source.joinpath('src/app.py').read_bytes() == b'value = 2\n'
    assert restored.config_dir.joinpath('session.json').is_file()
    assert invocation.execution_dir != loaded.execution_dir


def test_feedback_invocations_stay_flat_and_old_nested_layout_still_loads(sdk_workspace):
    authority, sdk = sdk_workspace
    second = SDKWorkspace.load(sdk.execution_dir, authority).fork_execution('execution-2')
    third = SDKWorkspace.load(second.execution_dir, authority).fork_execution('execution-3')
    assert sdk.execution_dir.parent == second.execution_dir.parent == third.execution_dir.parent
    restored = SDKWorkspace.load(third.execution_dir, authority)
    assert restored.source == sdk.source and restored.config_dir == sdk.execution_dir / 'sdk-config'
    with pytest.raises(SDKCandidateRejected, match='已存在'):
        restored.fork_execution('execution-2')
    # 旧版本把 fork 嵌套在首个 execution 下并以其为 session_root；已持久化的记录仍须可恢复。
    nested = sdk.execution_dir / 'executions' / 'legacy-execution'
    nested.mkdir(parents=True)
    metadata = json.loads((second.execution_dir / 'input_manifest.json').read_text(encoding='utf-8'))
    metadata['session_root'] = str(sdk.execution_dir)
    (nested / 'input_manifest.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
    assert SDKWorkspace.load(nested, authority).source == sdk.source


def test_runner_fails_closed_without_network(sdk_workspace):
    import asyncio
    from types import SimpleNamespace
    from tracefix.execution.agent_runner import AgentExecutionRunner
    _, sdk = sdk_workspace
    request = SimpleNamespace(execution_id='execution', config=SimpleNamespace(
        credential_ref='env:KEY', egress_allowlist=[], worker_image='sha256:' + '0' * 64,
        sdk_version='0.2.168', cli_version='2.1.301'))
    with pytest.raises(PermissionError, match='受控出口网络'):
        asyncio.run(AgentExecutionRunner(sdk).start(request, None))
