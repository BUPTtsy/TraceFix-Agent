import csv
import json
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from tracefix.execution.repository import safe_index_files
from tracefix.execution.runner import DockerRunner, process
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.runtime.contracts import PatchProposal, Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools import local
from tracefix.tools.fingerprints import diff_fingerprint
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.test_runner import RunTestsInput


@pytest.fixture
def engine(tmp_path):
    engine, state = make_engine(tmp_path / 'run')
    project = engine.scopes.projects[state.scope_id]
    notebook = project.root / 'src' / 'demo.ipynb'
    notebook.write_text(json.dumps({'nbformat': 4, 'nbformat_minor': 5,
                                   'metadata': {}, 'cells': []}), encoding='utf-8')
    safe_index_files(project.root, ['src/demo.ipynb'])
    commit_workspace(project.root, 'H1工具测试基线')
    context = engine.scopes.context(state.scope_id)
    workspace, source = Workspace.export(engine.scopes, context, 'HEAD', tmp_path / 'workspace')
    engine.workspace, engine.source, engine.context = workspace, source, context
    engine.browser.workspace = engine.model.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, source)
    state.mode, state.phase = 'repair', Phase.DIAGNOSE
    state.patch_base_commit = workspace.head()
    engine.store.save(state)
    return engine, state


def started_runner(engine, state):
    runner = DockerRunner(engine.profile, engine.workspace, state.run_id)
    runner._started = True
    runner.resolved_image_ids = {'app': 'sha256:' + 'a' * 64}
    engine.runner = runner
    return runner


def staging_path(command):
    fields = next(csv.reader([command[command.index('--mount') + 1]]))
    return Path(next(field[4:] for field in fields if field.startswith('src=')))


def test_run_tests_restricts_input_and_registration(engine):
    engine, state = engine
    with pytest.raises(ValidationError):
        RunTestsInput(target='unit', argv=['echo', 'bad'])
    with pytest.raises(ValidationError):
        RunTestsInput(target='reset')
    runtime = build_runtime_tools(engine, state, PatchProposal)
    spec = runtime.registry.get('RunTests', Phase.DIAGNOSE)
    assert spec.side_effect == 'read' and not spec.parallel_safe
    assert spec.phases == {Phase.DIAGNOSE}
    assert spec.timeout_s == engine.profile.timeout_seconds + 30
    assert not runtime.registry.contains('RunTests', Phase.VERIFY)
    worker = build_runtime_tools(engine, state, PatchProposal, {
        'worker_depth': 1, 'worker_allowed_files': ['src/value.ts'],
        'worker_allowed_tools': ['Read'], 'worker_shell_mode': 'disabled'})
    assert not worker.registry.contains('RunTests')


@pytest.mark.asyncio
async def test_run_tests_without_started_sandbox_never_executes_host(engine, monkeypatch):
    engine, state = engine

    async def forbidden(*args, **kwargs):
        pytest.fail('未启动 sandbox 不得执行任何命令')

    monkeypatch.setattr(local, 'process', forbidden)
    result = await build_runtime_tools(engine, state, PatchProposal).pipeline().execute(
        'RunTests', {}, 'unstarted')
    assert result.is_error and not result.executed
    assert '已启动的 Docker sandbox' in result.error['message']
    assert state.action_fingerprints == []


@pytest.mark.asyncio
async def test_run_tests_uses_audited_argv_readonly_overlay_and_full_output(engine, monkeypatch):
    engine, state = engine
    runner = started_runner(engine, state)
    engine.profile.commands['unit'] = ['node', '--test', 'literal;$(whoami)']
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    source = engine.workspace.root / 'src/value.ts'
    baseline = source.read_bytes()
    edited = await pipeline.execute('Write', {'file_path': str(source),
        'content': 'export const persisted = true;\n'}, 'write')
    assert not edited.is_error
    output = ('# diagnostic padding\n' * 6000 + 'TAP version 13\n'
              'not ok 1 - title validation rejects blank input\n'
              '  ---\n  error: expected blank input to fail\n  ...\n1..1\n')
    observed = []

    async def fake_process(command, timeout, **kwargs):
        observed.append((command, timeout, kwargs))
        if 'run' in command:
            assert kwargs == {'output_limit': None}
            assert timeout == engine.profile.timeout_seconds
            staging = staging_path(command)
            assert (staging / 'src/value.ts').read_bytes() != baseline
            mount = next(csv.reader([command[command.index('--mount') + 1]]))
            assert 'readonly' in mount
            (staging / 'src/value.ts').write_bytes(b'discarded mutation\n')
            (staging / 'dist').mkdir(exist_ok=True)
            (staging / 'dist/generated.js').write_bytes(b'output')
            return {'passed': False, 'exit_code': 1, 'output': output}
        return {'passed': True, 'exit_code': 0, 'output': ''}

    monkeypatch.setattr(local, 'process', fake_process)
    result = await pipeline.execute('RunTests', {'target': 'unit'}, 'test')
    assert not result.is_error and result.executed
    feedback = result.result
    assert not feedback['passed'] and feedback['exit_code'] == 1
    assert feedback['summary']['failed'] == 1
    assert feedback['cases'][0]['name'] == 'title validation rejects blank input'
    assert feedback['failure_signatures']
    assert feedback['overlay_revision'] == engine.workspace.overlay_revision
    assert feedback['diff_fingerprint'] == diff_fingerprint(engine.workspace, state)
    assert engine.artifacts.read(state.scope_id, state.run_id, feedback['output_ref']).decode() == output
    command = observed[0][0]
    entry = command.index('--entrypoint')
    assert command[entry + 1:] == ['node', runner.resolved_image_ids['app'], '--test', 'literal;$(whoami)']
    assert '/bin/bash' not in command and '--network=none' in command
    assert f'type=volume,src={runner.dependency_volume},dst=/workspace/node_modules,readonly' in command
    assert '/workspace/dist:rw,nosuid,size=128m,mode=1777' in command
    assert source.read_bytes() == baseline
    assert engine.workspace.read('src/value.ts') == 'export const persisted = true;\n'
    assert not (engine.workspace.root / 'dist/generated.js').exists()
    assert len(state.action_fingerprints) == 2
    assert engine.store.load(state.run_id, state.scope_id).action_fingerprints == state.action_fingerprints
    assert state.phase == Phase.DIAGNOSE and not state.validation_refs
    await pipeline.execute('RunTests', {'target': 'unit'}, 'test')
    assert len(observed) == 2 and len(state.action_fingerprints) == 2


@pytest.mark.asyncio
async def test_successful_writes_record_once_failed_writes_and_receipt_replays_do_not(engine):
    engine, state = engine
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    path = str(engine.workspace.root / 'src/value.ts')
    for call_id in ['write-first', 'write-replay']:
        result = await pipeline.execute('Write', {'file_path': path,
            'content': 'export const persisted = true;\n'}, call_id)
        assert not result.is_error
    assert len(state.action_fingerprints) == 1
    result = await pipeline.execute('Edit', {'file_path': path, 'old_string': 'missing',
        'new_string': 'true'}, 'invalid-edit')
    assert result.is_error and len(state.action_fingerprints) == 1
    result = await pipeline.execute('Edit', {'file_path': path, 'old_string': 'true',
        'new_string': 'false'}, 'valid-edit')
    assert not result.is_error and len(state.action_fingerprints) == 2
    result = await pipeline.execute('NotebookEdit', {
        'notebook_path': str(engine.workspace.root / 'src/demo.ipynb'),
        'cell_number': 0, 'edit_mode': 'insert', 'new_source': 'print(1)'}, 'notebook')
    assert not result.is_error and len(state.action_fingerprints) == 3


@pytest.mark.asyncio
async def test_bash_reuses_dependency_mounts_and_preserves_safe_writeback(engine, monkeypatch):
    engine, state = engine
    started_runner(engine, state)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    passed = True

    async def fake_process(command, timeout):
        if 'run' in command:
            staging = staging_path(command)
            (staging / 'src/value.ts').write_text('export const persisted = true;\n', encoding='utf-8')
            (staging / 'node_modules').mkdir(exist_ok=True)
            (staging / 'node_modules/cache').write_text('ignore', encoding='utf-8')
            (staging / 'dist').mkdir(exist_ok=True)
            (staging / 'dist/index.js').write_text('ignore', encoding='utf-8')
        return {'passed': passed, 'exit_code': 0 if passed else 1, 'output': 'command output'}

    monkeypatch.setattr(local, 'process', fake_process)
    result = await pipeline.execute('Bash', {'command': 'build', 'description': '构建'}, 'bash')
    assert not result.is_error and result.result['files_changed'] == ['src/value.ts']
    assert len(state.action_fingerprints) == 1
    passed = False
    result = await pipeline.execute('Bash', {'command': 'false', 'description': '失败'}, 'bash-fail')
    assert not result.result['passed'] and len(state.action_fingerprints) == 1


@pytest.mark.asyncio
async def test_cumulative_diff_identity_survives_materialization(engine):
    engine, state = engine
    empty = diff_fingerprint(engine.workspace, state)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    path = engine.workspace.root / 'src/value.ts'
    content = 'export const persisted = true;\n'
    await pipeline.execute('Write', {'file_path': str(path), 'content': content}, 'write')
    staged = diff_fingerprint(engine.workspace, state)
    assert staged != empty
    path.write_bytes(content.encode())
    engine.workspace.clear_staged()
    assert diff_fingerprint(engine.workspace, state) == staged
    state.action_fingerprints = [str(index) for index in range(12)]
    await pipeline.execute('Edit', {'file_path': str(path), 'old_string': 'true',
        'new_string': 'false'}, 'edit')
    assert len(state.action_fingerprints) == 12 and state.action_fingerprints[0] == '1'
    assert diff_fingerprint(engine.workspace, state) == empty


@pytest.mark.asyncio
async def test_process_can_preserve_full_output_without_changing_existing_default():
    argv = [sys.executable, '-c', "print('x' * 110000, end='')"]
    assert len((await process(argv))['output']) == 100_000
    assert len((await process(argv, output_limit=None))['output']) == 110_000


@pytest.mark.asyncio
async def test_run_tests_feedback_identifies_the_copied_view_during_concurrent_edit(engine, monkeypatch):
    engine, state = engine
    started_runner(engine, state)
    fingerprint = diff_fingerprint(engine.workspace, state)
    revision = engine.workspace.overlay_revision
    tools = local.LocalTools(engine, {}, state)
    waiting, completed = threading.Event(), threading.Event()
    threads = []

    def concurrent_write():
        waiting.set()
        with tools.mutex.write_lock(engine.workspace.root / 'src/value.ts'):
            engine.workspace.stage('src/value.ts', 'export const persisted = true;\n')
        completed.set()

    def capture_fingerprint(workspace, run):
        thread = threading.Thread(target=concurrent_write)
        threads.append(thread)
        thread.start()
        assert waiting.wait(2)
        assert not completed.wait(0.1)
        return diff_fingerprint(workspace, run)

    async def fake_process(command, timeout, **kwargs):
        if 'run' in command:
            assert 'false' in (staging_path(command) / 'src/value.ts').read_text(encoding='utf-8')
            assert completed.wait(2)
        return {'passed': True, 'exit_code': 0, 'output': 'TAP version 13\nok 1 - unit\n1..1\n'}

    monkeypatch.setattr(local, 'process', fake_process)
    monkeypatch.setattr(local, 'diff_fingerprint', capture_fingerprint)
    result = await build_runtime_tools(engine, state, PatchProposal).pipeline().execute('RunTests', {}, 'test')
    for thread in threads:
        thread.join(2)
    assert not result.is_error, result.error
    assert result.result['diff_fingerprint'] == fingerprint
    assert result.result['overlay_revision'] == revision
    assert engine.workspace.overlay_revision > revision
    assert diff_fingerprint(engine.workspace, state) != fingerprint


@pytest.mark.asyncio
async def test_protected_path_errors_preserve_code_in_edit_bash_and_proposal(engine, monkeypatch):
    engine, state = engine
    state.evidence_refs = [state.repo_snapshot_ref]
    engine.store.save(state)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    path = engine.workspace.root / 'src/value.ts'
    candidate = await pipeline.execute('Edit', {'file_path': str(path), 'old_string': 'false',
        'new_string': 'true', 'expected_overlay_revision': 0,
        'expected_overlay_hash': digest(path.read_bytes())}, 'allowed-candidate')
    assert not candidate.is_error, candidate.error
    fingerprints = list(state.action_fingerprints)
    engine.workspace.protected_paths = ['src/value.ts']
    write = await pipeline.execute('Write', {'file_path': str(path),
        'content': 'export const persisted = true;\n'}, 'protected-write')
    assert write.is_error and write.error['error_code'] == 'protected_path'
    assert write.result['path'] == 'src/value.ts'
    proposal = await pipeline.execute('propose_patch', {'summary': '修复',
        'evidence_refs': state.evidence_refs,
        'staged_refs': [candidate.result['staged_ref']]}, 'protected-submit')
    assert proposal.is_error and proposal.error['error_code'] == 'protected_path'

    async def fake_process(command, timeout):
        if 'run' in command:
            (staging_path(command) / 'src/value.ts').write_bytes(b'changed\n')
        return {'passed': True, 'exit_code': 0, 'output': ''}

    monkeypatch.setattr(local, 'process', fake_process)
    bash = await pipeline.execute('Bash', {'command': 'modify', 'description': '受保护文件'}, 'protected-bash')
    assert bash.is_error and bash.error['error_code'] == 'protected_path'
    assert state.action_fingerprints == fingerprints
