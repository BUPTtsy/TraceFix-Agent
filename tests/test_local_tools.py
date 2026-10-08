import asyncio
import csv
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.execution.repository import safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.runtime.contracts import PatchProposal, Phase, digest
from tracefix.tools.effects import (EffectBoundaryError, file_resource,
                                     make_operation_executor)
from tracefix.tools.local import (BashInput, EditInput, GlobInput, GrepInput,
    LocalTools, NotebookEditInput, ReadInput, WriteInput, local_tool_context)
from tracefix.runtime.smoke import make_engine
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.core import ToolRejected
from tracefix.storage.store import MemoryStore, UnknownOperation


@pytest.fixture
def engine(tmp_path, request):
    import pypdf

    engine, state = make_engine(tmp_path / 'run')
    project = engine.scopes.projects[state.scope_id]
    project.allowed_files = ['src/**', '*.py', '*.ipynb']
    files = {'app.py': 'value = 1\nvalue = 1\n', 'other.txt': 'secret\n',
             'example.ipynb': json.dumps({'nbformat': 4, 'nbformat_minor': 5,
                                         'metadata': {'name': 'demo'}, 'cells': []})}
    files.update(getattr(request, 'param', {}))
    for relative, content in files.items():
        (project.root / relative).write_text(content, encoding='utf-8', newline='')
    (project.root / 'example.png').write_bytes(b'\x89PNG\r\n\x1a\n')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with (project.root / 'example.pdf').open('wb') as stream:
        writer.write(stream)
    safe_index_files(project.root, [*files, 'example.png', 'example.pdf'])
    commit_workspace(project.root, 'Local tools fixture baseline')
    context = engine.scopes.context(state.scope_id)
    workspace, source = Workspace.export(engine.scopes, context, 'HEAD', tmp_path / 'workspace')
    engine.workspace, engine.source, engine.context = workspace, source, context
    engine.browser.workspace = engine.model.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, source)
    state.mode, state.phase = 'repair', Phase.DIAGNOSE
    engine.store.save(state)
    assert isinstance(engine.store, MemoryStore)
    assert workspace.require_repository_snapshot() == source
    return engine, state


def worker_context(*, writing=False, shell_mode='disabled', allowed_tools=None):
    tools = ['Read', 'Glob', 'Grep']
    if writing:
        tools.extend(['Write', 'Edit', 'NotebookEdit'])
    if shell_mode != 'disabled':
        tools.append('Bash')
    return {'worker_depth': 1, 'worker_allowed_files': ['app.py'],
            'worker_write_enabled': writing, 'worker_writable_files': ['app.py'] if writing else [],
            'worker_allowed_tools': tools if allowed_tools is None else allowed_tools,
            'worker_shell_mode': shell_mode}


async def fenced_bash(tools, arguments):
    operation = make_operation_executor(tools.engine.store, tools.state)
    return await operation('Bash', {'tool_name': 'Bash', 'arguments': arguments.model_dump(mode='json'),
                                  'resources': [file_resource(tools.root)]},
                           lambda: tools.bash(arguments))


def test_common_registry_readonly_and_explicit_worker_write(engine):
    engine, state = engine
    context = worker_context(shell_mode='readonly')
    runtime = build_runtime_tools(engine, state, PatchProposal, context)
    assert all(runtime.registry.contains(name) for name in ['Read', 'Glob', 'Grep', 'Bash'])
    assert not any(runtime.registry.contains(name) for name in ['Write', 'Edit', 'NotebookEdit', 'agent.delegate'])
    context = worker_context(writing=True, shell_mode='patch')
    runtime = build_runtime_tools(engine, state, PatchProposal, context)
    assert all(runtime.registry.contains(name) for name in ['Write', 'Edit', 'NotebookEdit'])
    assert not runtime.registry.contains('code.read')
    assert not runtime.registry.contains('code.search')


def test_gui_child_engine_uses_supervisor_permissions(engine):
    engine, state = engine
    engine.subagent_depth = 1
    engine.worker_allowed_files = ['app.py']
    engine.worker_allowed_tools = ['Read']
    engine.worker_shell_mode = 'disabled'
    state.phase = Phase.EXPLORE
    runtime = build_runtime_tools(engine, state, PatchProposal)
    assert runtime.registry.contains('Read')
    assert not runtime.registry.contains('Write')
    assert not runtime.registry.contains('agent.delegate')


@pytest.mark.asyncio
async def test_read_pagination_edit_uniqueness_and_write_scope(engine):
    engine, state = engine
    context = worker_context(writing=True)
    tools = LocalTools(engine, context, state)
    path = str(engine.workspace.root / 'app.py')
    assert tools.read(ReadInput(file_path=path, offset=2, limit=1))['content'] == '2\tvalue = 1'
    with pytest.raises(ToolRejected):
        tools.edit(EditInput(file_path=path, old_string='value = 1', new_string='value = 2'))
    result = await build_runtime_tools(engine, state, PatchProposal, context).pipeline().execute(
        'Edit', {'file_path': path, 'old_string': 'value = 1', 'new_string': 'value = 2',
                 'replace_all': True}, 'replace-all')
    assert result.executed and not result.is_error
    assert engine.workspace.read('app.py') == 'value = 2\nvalue = 2\n'
    assert Path(path).read_bytes() == b'value = 1\nvalue = 1\n'
    with pytest.raises(ToolRejected):
        tools.write(WriteInput(file_path=str(engine.workspace.root / 'other.txt'), content='bad'))
    with pytest.raises(ToolRejected):
        tools.read(ReadInput(file_path='app.py'))


def test_grep_modes_multiline_glob_and_worker_isolation(engine):
    engine, state = engine
    tools = LocalTools(engine, worker_context(), state)
    assert tools.grep(GrepInput(pattern='secret'))['matches'] == []
    assert tools.grep(GrepInput(pattern='value', output_mode='count'))['matches'][0]['count'] == 2
    assert tools.grep(GrepInput(pattern='1\nvalue', multiline=True, output_mode='content'))['matches'][0]['line'] == 1
    assert tools.glob(GlobInput(pattern='**/*.py'))['files'] == [str(engine.workspace.root / 'app.py')]


@pytest.mark.asyncio
async def test_write_rejects_file_creation_and_glob_orders_by_modification_time(engine):
    engine, state = engine
    tools = LocalTools(engine, {}, state)
    path = engine.workspace.root / 'new.py'
    with pytest.raises(ToolRejected, match='不支持创建新文件'):
        tools.write(WriteInput(file_path=str(path), content='created\n'))
    result = await build_runtime_tools(engine, state, PatchProposal).pipeline().execute(
        'Write', {'file_path': str(path), 'content': 'created\n'}, 'create')
    assert result.is_error and not result.executed
    assert not path.exists()
    assert engine.workspace.staged_content('new.py') is None
    os.utime(engine.workspace.root / 'app.py', ns=(1000000000, 1000000000))
    recent = engine.workspace.root / 'src/value.ts'
    os.utime(recent, ns=(2000000000, 2000000000))
    assert tools.glob(GlobInput(pattern='**/*.[pt][sy]'))['files'] == [
        str(recent), str(engine.workspace.root / 'app.py')]


@pytest.mark.parametrize('engine', [{'app.py': 'line\n' * 2001}], indirect=True)
def test_read_defaults_to_2000_lines(engine):
    engine, state = engine
    path = engine.workspace.root / 'app.py'
    result = LocalTools(engine, {}, state).read(ReadInput(file_path=str(path)))
    assert result['total_lines'] == 2001
    assert len(result['content'].splitlines()) == 2000


@pytest.mark.asyncio
async def test_notebook_insert_replace_delete_preserves_metadata(engine):
    engine, state = engine
    path = engine.workspace.root / 'example.ipynb'
    baseline = path.read_bytes()
    tools = LocalTools(engine, {}, state)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    for call_id, arguments in [('insert', {'edit_mode': 'insert', 'new_source': 'print(1)'}),
                               ('replace', {'new_source': 'hello', 'cell_type': 'markdown'})]:
        result = await pipeline.execute('NotebookEdit', {'notebook_path': str(path),
            'cell_number': 0, **arguments}, call_id)
        assert result.executed and not result.is_error
    notebook = json.loads(engine.workspace.read('example.ipynb'))
    assert notebook['metadata'] == {'name': 'demo'}
    assert notebook['cells'][0]['cell_type'] == 'markdown'
    assert 'outputs' not in notebook['cells'][0]
    result = await pipeline.execute('NotebookEdit', {'notebook_path': str(path),
        'cell_number': 0, 'edit_mode': 'delete'}, 'delete')
    assert result.executed and not result.is_error
    assert tools.read(ReadInput(file_path=str(path)))['cells'] == []
    assert path.read_bytes() == baseline


def test_pdf_pages_and_image_read(engine):
    engine, state = engine
    pdf = engine.workspace.root / 'example.pdf'
    tools = LocalTools(engine, {}, state)
    assert tools.read(ReadInput(file_path=str(pdf)))['total_pages'] == 1
    with pytest.raises(ToolRejected):
        tools.read(ReadInput(file_path=str(pdf), pages=[2]))
    image = engine.workspace.root / 'example.png'
    assert tools.read(ReadInput(file_path=str(image)))['type'] == 'image'


@pytest.mark.asyncio
async def test_read_image_pipeline_keeps_visual_payload_out_of_text(engine):
    engine, state = engine
    image = engine.workspace.root / 'example.png'
    runtime = build_runtime_tools(engine, state, PatchProposal)
    result = await runtime.pipeline().execute('Read', {'file_path': str(image)}, 'image-read')
    assert result.images[0]['image_url']['url'].startswith('data:image/png;base64,')
    assert 'base64' not in result.to_content()


@pytest.mark.asyncio
async def test_seven_tools_execute_through_registered_pipeline(engine, monkeypatch):
    engine, state = engine
    from tracefix.tools import local as local_tools
    import csv
    from pathlib import Path

    async def fake_process(command, timeout):
        if 'run' in command:
            fields = next(csv.reader([command[command.index('--mount') + 1]]))
            staging = Path(next(value[4:] for value in fields if value.startswith('src=')))
            assert b'value = 4' in (staging / 'app.py').read_bytes()
        return {'passed': True, 'exit_code': 0, 'output': 'value = 4'}

    monkeypatch.setattr(local_tools, 'process', fake_process)
    runtime = build_runtime_tools(engine, state, PatchProposal)
    pipeline = runtime.pipeline()
    path = str(engine.workspace.root / 'app.py')
    written = await pipeline.execute('Write', {'file_path': path, 'content': 'value = 3\n'}, 'write')
    edited = await pipeline.execute('Edit', {'file_path': path, 'old_string': '3', 'new_string': '4'}, 'edit')
    read = await pipeline.execute('Read', {'file_path': path}, 'read')
    assert written.executed and edited.executed and not written.is_error and not edited.is_error
    assert read.result['content'] == '1\tvalue = 4'
    assert (await pipeline.execute('Glob', {'pattern': '**/*.py'}, 'glob')).result['files'] == [path]
    assert (await pipeline.execute('Grep', {'pattern': 'value', 'type': 'py'}, 'grep')).result['matches'] == [path]
    notebook = engine.workspace.root / 'example.ipynb'
    baseline = notebook.read_bytes()
    result = await pipeline.execute('NotebookEdit', {'notebook_path': str(notebook), 'cell_number': 0,
        'edit_mode': 'insert', 'new_source': 'print(4)'}, 'notebook')
    assert result.executed and not result.is_error
    assert json.loads(engine.workspace.read('example.ipynb'))['cells'][0]['source'] == ['print(4)']
    assert notebook.read_bytes() == baseline
    assert (await pipeline.execute('Bash', {'command': 'cat app.py', 'description': 'read'}, 'bash')).result['exit_code'] == 0
    assert Path(path).read_bytes() == b'value = 1\nvalue = 1\n'
    records = list(engine.store.operations.values())
    assert len(records) == 4 and all(record['status'] == 'DONE' for record in records)
    assert records[0]['resources'] == [file_resource(path)]
    assert records[-1]['resources'] == [file_resource(engine.workspace.root)]


@pytest.mark.asyncio
async def test_supervisor_delegate_passes_explicit_write_contract(tmp_path, monkeypatch):
    from tracefix.runtime.smoke import make_engine
    from tracefix.workers.contracts import WorkerResult
    engine, state = make_engine(tmp_path / 'run')
    state.mode, state.phase = 'repair', Phase.DIAGNOSE
    engine.store.save(state)
    observed = []

    async def model_call(child, schema, context):
        observed.append(context)
        assert child.parent_run_id == state.run_id
        assert child.run_id != state.run_id
        return WorkerResult(summary='done')

    monkeypatch.setattr(engine, 'model_call', model_call)
    runtime = build_runtime_tools(engine, state, PatchProposal)
    arguments = {'task_id': 'delegate-1', 'role': 'code-explorer', 'phase': 'DIAGNOSE',
                 'objective': 'inspect the authorized source file',
                 'prompt': 'Read the file and return a concrete evidence backed result.',
                 'allowed_tools': ['Read'], 'allowed_files': ['src/value.ts'],
                 'expected_output': 'evidence', 'completion_criteria': ['cite files'],
                 'constraints': ['stay in scope']}
    await runtime.handlers['agent.delegate'](arguments, 'delegate-1')
    assert observed[-1]['worker_write_enabled'] is False
    arguments.update(write_enabled=True, writable_files=['src/value.ts'])
    await runtime.handlers['agent.delegate'](arguments, 'delegate-2')
    assert observed[-1]['worker_write_enabled'] is True
    assert observed[-1]['worker_writable_files'] == ['src/value.ts']


@pytest.mark.asyncio
async def test_bash_docker_readonly_and_scoped_writeback(engine, monkeypatch):
    engine, state = engine
    import csv
    from pathlib import Path
    from tracefix.tools import local as local_tools
    commands = []

    async def fake_process(command, timeout):
        commands.append(command)
        if 'run' in command:
            mount = next(csv.reader([command[command.index('--mount') + 1]]))
            staging = Path(next(value[4:] for value in mount if value.startswith('src=')))
            assert not (staging / 'other.txt').exists()
            if 'readonly' not in mount:
                (staging / 'app.py').write_bytes(b'value = 3\n')
        return {'passed': True, 'exit_code': 0, 'output': 'done'}

    monkeypatch.setattr(local_tools, 'process', fake_process)
    context = worker_context(shell_mode='readonly')
    tools = LocalTools(engine, context, state)
    result = await fenced_bash(tools, BashInput(command='cat app.py', description='read'))
    assert result['files_changed'] == []
    assert '--network=none' in commands[0]
    context = worker_context(writing=True, shell_mode='patch')
    result = await fenced_bash(LocalTools(engine, context, state), BashInput(command='edit', description='write'))
    assert result['files_changed'] == ['app.py']
    assert engine.workspace.read('app.py') == 'value = 3\n'
    assert (engine.workspace.root / 'app.py').read_bytes() == b'value = 1\nvalue = 1\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['unauthorized', 'conflict', 'overlay_conflict', 'failed'])
async def test_bash_rejects_unsafe_writeback(engine, monkeypatch, mode):
    engine, state = engine
    import csv
    from pathlib import Path
    from tracefix.tools import local as local_tools

    async def fake_process(command, timeout):
        if 'run' in command:
            fields = next(csv.reader([command[command.index('--mount') + 1]]))
            staging = Path(next(value[4:] for value in fields if value.startswith('src=')))
            (staging / 'app.py').write_bytes(b'changed\n')
            if mode == 'unauthorized':
                (staging / 'escape.py').write_bytes(b'bad\n')
            if mode == 'conflict':
                (engine.workspace.root / 'app.py').write_bytes(b'concurrent\n')
            if mode == 'overlay_conflict':
                engine.workspace.stage('app.py', 'concurrent-overlay\n')
        return {'passed': mode != 'failed', 'exit_code': 1 if mode == 'failed' else 0, 'output': ''}

    monkeypatch.setattr(local_tools, 'process', fake_process)
    tools = LocalTools(engine, worker_context(writing=True, shell_mode='patch'), state)
    arguments = BashInput(command='modify', description='test')
    if mode == 'failed':
        result = await fenced_bash(tools, arguments)
        assert result['files_changed'] == []
    else:
        with pytest.raises(ToolRejected):
            await fenced_bash(tools, arguments)
    expected = ('concurrent\n' if mode == 'conflict' else 'concurrent-overlay\n'
                if mode == 'overlay_conflict' else 'value = 1\nvalue = 1\n')
    assert engine.workspace.read('app.py') == expected
    assert not (engine.workspace.root / 'escape.py').exists()
    assert (engine.workspace.root / 'app.py').read_bytes() == (
        b'concurrent\n' if mode == 'conflict' else b'value = 1\nvalue = 1\n')


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('TRACEFIX_TEST_LOCAL_DOCKER') != '1', reason='opt-in Docker test')
async def test_real_bash_readonly_and_explicit_writeback(engine):
    engine, state = engine
    engine.profile.image = 'tracefix-bugboard:1.0'
    context = worker_context(shell_mode='readonly')
    result = await fenced_bash(LocalTools(engine, context, state), BashInput(
        command="printf 'changed\\n' > app.py", description='verify read-only sandbox'))
    assert result['exit_code'] != 0
    assert engine.workspace.read('app.py') == 'value = 1\nvalue = 1\n'
    context = worker_context(writing=True, shell_mode='patch')
    runtime = build_runtime_tools(engine, state, PatchProposal, context)
    receipt = await runtime.pipeline().execute('Bash', {
        'command': "printf 'changed\\n' > app.py", 'description': 'verify scoped writeback'}, 'real-bash')
    assert not receipt.is_error, receipt.error
    assert receipt.result['exit_code'] == 0, receipt.result['output']
    assert engine.workspace.read('app.py') == 'changed\n'
    assert (engine.workspace.root / 'app.py').read_bytes() == b'value = 1\nvalue = 1\n'


@pytest.mark.parametrize(('context', 'expected_tools', 'expected_shell'), [
    ({}, [], 'disabled'),
    ({'worker_write_enabled': True}, [], 'disabled'),
    ({'allowed_tools': []}, [], 'disabled'),
    ({'worker_allowed_tools': []}, [], 'disabled'),
    ({'allowed_tools': [], 'worker_allowed_tools': ['Read', 'Bash'],
      'worker_shell_mode': 'patch'}, [], 'disabled'),
    ({'allowed_tools': ['Read', 'Bash'], 'worker_allowed_tools': [],
      'worker_shell_mode': 'patch'}, [], 'disabled'),
    ({'allowed_tools': ['code.read', 'file.write'], 'worker_allowed_tools': ['Read', 'Edit']},
     ['Read', 'Edit'], 'disabled'),
    ({'allowed_tools': ['Bash']}, [], 'disabled'),
    ({'allowed_tools': ['Bash'], 'worker_shell_mode': 'invalid'}, [], 'disabled'),
    ({'allowed_tools': ['shell.readonly'], 'worker_allowed_tools': ['shell.patch'],
      'worker_shell_mode': 'patch'}, ['Bash'], 'readonly'),
    ({'allowed_tools': ['shell.patch'], 'worker_allowed_tools': ['shell.readonly'],
      'worker_shell_mode': 'patch'}, ['Bash'], 'readonly'),
    ({'allowed_tools': ['Bash', 'shell.readonly', 'shell.patch'],
      'worker_shell_mode': 'patch'}, ['Bash'], 'readonly'),
    ({'allowed_tools': ['shell.patch'], 'worker_allowed_tools': ['Bash'],
      'worker_shell_mode': 'patch'}, ['Bash'], 'patch'),
])
def test_worker_context_fail_closed_and_capability_intersection(context, expected_tools, expected_shell):
    normalized = local_tool_context(SimpleNamespace(), {'worker_depth': 1, **context})
    assert normalized['worker_allowed_tools'] == expected_tools
    assert normalized['worker_shell_mode'] == expected_shell
    assert local_tool_context(SimpleNamespace(), normalized) == normalized


def test_gui_missing_supervisor_grants_cannot_inherit_request_permissions():
    engine = SimpleNamespace(subagent_depth=1)
    context = worker_context(writing=True, shell_mode='patch')
    context['allowed_tools'] = ['Read', 'Write', 'Bash']
    normalized = local_tool_context(engine, context)
    assert normalized['worker_allowed_tools'] == []
    assert normalized['worker_shell_mode'] == 'disabled'
    assert normalized['worker_write_enabled'] is False


def test_write_dependencies_and_state_never_enable_standalone_fallback(engine, monkeypatch):
    engine, state = engine
    path = engine.workspace.root / 'app.py'
    baseline = path.read_bytes()
    cases = [('state', None), ('state', SimpleNamespace(mode='repair', phase=Phase.DIAGNOSE)),
             ('store', None), ('snapshot', None), ('stage', None), ('guard', None)]
    for dependency, value in cases:
        with monkeypatch.context() as patcher:
            current_state = state
            if dependency == 'state':
                current_state = value
            elif dependency == 'store':
                patcher.setattr(engine, 'store', value)
            elif dependency == 'snapshot':
                patcher.setattr(engine.workspace, 'repository_snapshot', value)
            elif dependency == 'stage':
                patcher.setattr(engine.workspace, 'stage', value)
            else:
                patcher.setattr(engine.store, 'operation_guard', value)
            tools = LocalTools(engine, {'mode': 'repair', 'phase': Phase.DIAGNOSE}, current_state)
            assert tools.read(ReadInput(file_path=str(path)))['total_lines'] == 2
            with pytest.raises((ToolRejected, PermissionError)):
                tools.write(WriteInput(file_path=str(path), content='forbidden\n'))
            assert path.read_bytes() == baseline
            assert engine.workspace.staged_content('app.py') is None
    standalone = SimpleNamespace(workspace=Workspace(engine.workspace.root, ['*.py']))
    tools = LocalTools(standalone, {})
    assert not tools.write_enabled
    with pytest.raises(ToolRejected):
        tools.write(WriteInput(file_path=str(path), content='forbidden\n'))
    assert path.read_bytes() == baseline


def test_mode_phase_and_worker_path_permissions_remain_restrictive(engine):
    engine, state = engine
    path = engine.workspace.root / 'app.py'
    for mode, phase in [('test', Phase.DIAGNOSE), ('repair', Phase.PREPARE), ('repair', Phase.EXPLORE)]:
        current = state.model_copy(update={'mode': mode, 'phase': phase})
        tools = LocalTools(engine, {}, current)
        assert not tools.write_enabled
        runtime = build_runtime_tools(engine, current, PatchProposal)
        assert not any(runtime.registry.contains(name) for name in ['Write', 'Edit', 'NotebookEdit'])
        with pytest.raises(ToolRejected):
            tools.write(WriteInput(file_path=str(path), content='forbidden\n'))
    tools = LocalTools(engine, worker_context(), state)
    with pytest.raises(ToolRejected):
        tools.write(WriteInput(file_path=str(path), content='forbidden\n'))
    for value in ['app.py', str(engine.workspace.root.parent / 'escape.py'),
                  str(engine.workspace.root / 'other.txt')]:
        with pytest.raises(ToolRejected):
            tools.read(ReadInput(file_path=value))
    context = worker_context(writing=True)
    context['worker_writable_files'] = ['other.txt']
    with pytest.raises(ToolRejected):
        LocalTools(engine, context, state)
    assert path.read_bytes() == b'value = 1\nvalue = 1\n'
    assert engine.workspace.staged_content('app.py') is None


@pytest.mark.asyncio
async def test_local_side_effects_without_operation_guard_fail_before_execution(engine, monkeypatch):
    from tracefix.tools import local as local_tools

    engine, state = engine
    tools = LocalTools(engine, {}, state)
    path = str(engine.workspace.root / 'app.py')
    notebook = engine.workspace.root / 'example.ipynb'
    baseline = notebook.read_bytes()

    async def forbidden(*arguments, **kwargs):
        pytest.fail('缺少资源 fence 时不能启动副作用执行器')

    monkeypatch.setattr(local_tools, 'run_effect', forbidden)
    monkeypatch.setattr(local_tools, 'process', forbidden)
    callbacks = [lambda: tools.write(WriteInput(file_path=path, content='changed\n')),
                 lambda: tools.edit(EditInput(file_path=path, old_string='1', new_string='2', replace_all=True)),
                 lambda: tools.notebook_edit(NotebookEditInput(notebook_path=str(notebook), cell_number=0,
                     edit_mode='insert', new_source='print(1)'))]
    for callback in callbacks:
        with pytest.raises(EffectBoundaryError, match='资源 fence'):
            callback()
    with pytest.raises(EffectBoundaryError, match='资源 fence'):
        await tools.write_effect(WriteInput(file_path=path, content='changed\n'), 'Write')
    with pytest.raises(EffectBoundaryError, match='资源 fence'):
        await tools.bash(BashInput(command='cat app.py', description='read'))
    assert engine.workspace._staged_files == {}
    assert Path(path).read_bytes() == b'value = 1\nvalue = 1\n'
    assert notebook.read_bytes() == baseline


@pytest.mark.asyncio
async def test_shell_readonly_authorization_cannot_be_upgraded_to_patch(engine, monkeypatch):
    from tracefix.tools import local as local_tools

    engine, state = engine
    commands = []

    async def fake_process(command, timeout):
        commands.append(command)
        if 'run' in command:
            fields = next(csv.reader([command[command.index('--mount') + 1]]))
            assert 'readonly' in fields
            staging = Path(next(value[4:] for value in fields if value.startswith('src=')))
            (staging / 'app.py').write_bytes(b'forbidden\n')
        return {'passed': True, 'exit_code': 0, 'output': ''}

    monkeypatch.setattr(local_tools, 'process', fake_process)
    for parent, worker in [(['shell.readonly'], ['shell.patch']),
                           (['shell.patch'], ['shell.readonly'])]:
        context = worker_context(writing=True, shell_mode='patch', allowed_tools=worker)
        context['allowed_tools'] = parent
        runtime = build_runtime_tools(engine, state, PatchProposal, context)
        assert runtime.registry.contains('Bash')
        result = await runtime.pipeline().execute('Bash', {'command': 'modify ' + parent[0],
            'description': 'verify readonly ceiling'}, parent[0])
        assert result.executed and not result.is_error
        assert result.result['files_changed'] == []
        assert result.result['discarded_changes'] == ['app.py']
    context = worker_context(writing=True, shell_mode='patch', allowed_tools=[])
    assert not build_runtime_tools(engine, state, PatchProposal, context).registry.contains('Bash')
    with pytest.raises(ToolRejected):
        await LocalTools(engine, context, state).bash(BashInput(command='forbidden', description='deny'))
    assert len(commands) == 4
    assert engine.workspace.staged_content('app.py') is None
    assert (engine.workspace.root / 'app.py').read_bytes() == b'value = 1\nvalue = 1\n'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['hash', 'overlay', 'disk_with_overlay'])
async def test_async_prepare_preserves_hash_and_concurrent_baselines(engine, monkeypatch, change):
    from tracefix.tools import local as local_tools

    engine, state = engine
    path = engine.workspace.root / 'app.py'
    if change == 'disk_with_overlay':
        engine.workspace.stage('app.py', 'initial-overlay\n')
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(kind, payload, **kwargs):
        assert kind == 'local.prepare'
        entered.set()
        await release.wait()
        return {'content': 'worker\n', 'before_hash': 'wrong' if change == 'hash'
                else digest(payload['content'].encode('utf-8'))}

    monkeypatch.setattr(local_tools, 'run_effect', prepare)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    task = asyncio.create_task(pipeline.execute('Write', {'file_path': str(path), 'content': 'worker\n'}, 'write'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if change == 'overlay':
            engine.workspace.stage('app.py', 'concurrent-overlay\n')
        elif change == 'disk_with_overlay':
            path.write_bytes(b'concurrent-disk\n')
    finally:
        release.set()
    result = await task
    assert result.is_error and not result.executed
    expected_overlay = (b'concurrent-overlay\n' if change == 'overlay' else b'initial-overlay\n'
                        if change == 'disk_with_overlay' else None)
    assert engine.workspace.staged_content('app.py') == expected_overlay
    assert path.read_bytes() == (b'concurrent-disk\n' if change == 'disk_with_overlay'
                                 else b'value = 1\nvalue = 1\n')


@pytest.mark.asyncio
async def test_unknown_operation_fences_writes_without_blocking_read(engine, monkeypatch):
    from tracefix.tools import local as local_tools

    engine, state = engine
    path = engine.workspace.root / 'app.py'
    intent = {'tool_name': 'Write', 'arguments': {'file_path': str(path), 'content': 'lost\n'},
              'resources': [file_resource(path)]}
    engine.store.begin(state, 'lost-write', intent, owner='lost-owner')
    engine.store.mark_unknown(state, 'lost-write', owner='lost-owner', reason='missing receipt')

    async def forbidden(*arguments, **kwargs):
        pytest.fail('UNKNOWN fence 必须在 prepare 前拒绝写入')

    monkeypatch.setattr(local_tools, 'run_effect', forbidden)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    assert (await pipeline.execute('Read', {'file_path': str(path)}, 'read')).result['total_lines'] == 2
    with pytest.raises(UnknownOperation):
        await pipeline.execute('Write', {'file_path': str(path), 'content': 'new\n'}, 'new-write')
    assert engine.store.operations['lost-write']['status'] == 'UNKNOWN'
    assert engine.workspace.staged_content('app.py') is None
    assert path.read_bytes() == b'value = 1\nvalue = 1\n'


@pytest.mark.asyncio
async def test_prepare_cannot_apply_after_owned_fence_becomes_unknown(engine, monkeypatch):
    from tracefix.tools import local as local_tools

    engine, state = engine
    path = engine.workspace.root / 'app.py'

    async def prepare(kind, payload, **kwargs):
        operation_id, record = next(iter(engine.store.operations.items()))
        engine.store.mark_unknown(state, operation_id, owner=record['owner_token'], reason='lost ownership')
        return {'content': 'worker\n', 'before_hash': digest(payload['content'].encode('utf-8'))}

    monkeypatch.setattr(local_tools, 'run_effect', prepare)
    with pytest.raises(EffectBoundaryError):
        await build_runtime_tools(engine, state, PatchProposal).pipeline().execute(
            'Write', {'file_path': str(path), 'content': 'worker\n'}, 'write')
    assert next(iter(engine.store.operations.values()))['status'] == 'UNKNOWN'
    assert engine.workspace.staged_content('app.py') is None
    assert path.read_bytes() == b'value = 1\nvalue = 1\n'
