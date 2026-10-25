import csv
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.execution.repository import safe_index_files
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.runtime.contracts import PatchProposal, Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.storage.store import MemoryStore
from tracefix.tools.core import ToolProtocolError, ToolRejected
from tracefix.tools.effects import file_resource, make_operation_executor
from tracefix.tools.handlers import build_runtime_tools
from tracefix.tools.local import (BashInput, GlobInput, GrepInput, LocalTools,
                                  ReadInput, local_tool_context)


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


def worker_context(*, shell_mode='disabled', allowed_tools=None):
    tools = ['Read', 'Glob', 'Grep']
    if shell_mode != 'disabled':
        tools.append('Bash')
    return {'worker_depth': 1, 'worker_allowed_files': ['app.py'],
            'worker_allowed_tools': tools if allowed_tools is None else allowed_tools,
            'worker_shell_mode': shell_mode}


async def fenced_bash(tools, arguments):
    operation = make_operation_executor(tools.engine.store, tools.state)
    return await operation('Bash', {'tool_name': 'Bash', 'arguments': arguments.model_dump(mode='json'),
                                  'resources': [file_resource(tools.root)]},
                           lambda: tools.bash(arguments))


def test_common_registry_is_readonly_for_supervisor_and_worker(engine):
    engine, state = engine
    for context in ({}, worker_context(shell_mode='readonly'),
                    {**worker_context(), 'worker_write_enabled': True,
                     'worker_writable_files': ['app.py']}):
        runtime = build_runtime_tools(engine, state, PatchProposal, context)
        assert all(runtime.registry.contains(name) for name in ['Read', 'Glob', 'Grep'])
        assert not any(runtime.registry.contains(name) for name in [
            'Write', 'Edit', 'NotebookEdit', 'RunTests', 'finish_repair', 'revert_overlay'])


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


def test_read_pagination_and_path_scope(engine):
    engine, state = engine
    tools = LocalTools(engine, worker_context(), state)
    path = str(engine.workspace.root / 'app.py')
    assert tools.read(ReadInput(file_path=path, offset=2, limit=1))['content'] == '2\tvalue = 1'
    for denied in ['app.py', str(engine.workspace.root.parent / 'escape.py'),
                   str(engine.workspace.root / 'other.txt')]:
        with pytest.raises(ToolRejected):
            tools.read(ReadInput(file_path=denied))


def test_grep_modes_multiline_glob_and_worker_isolation(engine):
    engine, state = engine
    tools = LocalTools(engine, worker_context(), state)
    assert tools.grep(GrepInput(pattern='secret'))['matches'] == []
    assert tools.grep(GrepInput(pattern='value', output_mode='count'))['matches'][0]['count'] == 2
    assert tools.grep(GrepInput(pattern='1\nvalue', multiline=True, output_mode='content'))['matches'][0]['line'] == 1
    assert tools.glob(GlobInput(pattern='**/*.py'))['files'] == [str(engine.workspace.root / 'app.py')]


@pytest.mark.parametrize('engine', [{'app.py': 'line\n' * 2001}], indirect=True)
def test_read_defaults_to_2000_lines(engine):
    engine, state = engine
    path = engine.workspace.root / 'app.py'
    result = LocalTools(engine, {}, state).read(ReadInput(file_path=str(path)))
    assert result['total_lines'] == 2001
    assert len(result['content'].splitlines()) == 2000


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


def test_glob_orders_by_modification_time(engine):
    engine, state = engine
    older = engine.workspace.root / 'app.py'
    newer = engine.workspace.root / 'src/value.ts'
    os.utime(older, ns=(1000000000, 1000000000))
    os.utime(newer, ns=(2000000000, 2000000000))
    assert LocalTools(engine, {}, state).glob(GlobInput(pattern='**/*.[pt][sy]'))['files'] == [
        str(newer), str(older)]


@pytest.mark.parametrize(('context', 'expected_tools', 'expected_shell'), [
    ({}, [], 'disabled'),
    ({'worker_write_enabled': True}, [], 'disabled'),
    ({'allowed_tools': []}, [], 'disabled'),
    ({'worker_allowed_tools': []}, [], 'disabled'),
    ({'allowed_tools': ['code.read', 'file.write'],
      'worker_allowed_tools': ['Read', 'Edit']}, ['Read'], 'disabled'),
    ({'allowed_tools': ['Bash'], 'worker_shell_mode': 'patch'}, [], 'disabled'),
    ({'allowed_tools': ['shell.patch'], 'worker_allowed_tools': ['Bash'],
      'worker_shell_mode': 'readonly'}, [], 'disabled'),
    ({'allowed_tools': ['shell.readonly'], 'worker_allowed_tools': ['Bash'],
      'worker_shell_mode': 'readonly'}, ['Bash'], 'readonly'),
])
def test_worker_context_fail_closed_and_capability_intersection(context, expected_tools, expected_shell):
    normalized = local_tool_context(SimpleNamespace(), {'worker_depth': 1, **context})
    assert normalized['worker_allowed_tools'] == expected_tools
    assert normalized['worker_shell_mode'] == expected_shell
    assert local_tool_context(SimpleNamespace(), normalized) == normalized


def test_gui_missing_supervisor_grants_cannot_inherit_request_permissions():
    context = {**worker_context(shell_mode='readonly'), 'allowed_tools': ['Read', 'Write', 'Bash']}
    normalized = local_tool_context(SimpleNamespace(subagent_depth=1), context)
    assert normalized['worker_allowed_tools'] == []
    assert normalized['worker_shell_mode'] == 'disabled'


def test_container_workspace_paths_map_to_host_root_and_keep_boundaries(engine):
    engine, state = engine
    tools = LocalTools(engine, worker_context(), state)
    root = engine.workspace.root.resolve()
    # 沙箱内的 /workspace/... 与宿主绝对路径指向同一文件（Windows 上 /workspace 并非绝对路径）。
    assert tools.container_path('/workspace/app.py') == root / 'app.py'
    assert tools.container_path('\\workspace\\app.py') == root / 'app.py'
    assert tools.container_path('/workspace') == root
    assert tools.read(ReadInput(file_path='/workspace/app.py', offset=2, limit=1))['content'] == '2\tvalue = 1'
    # 目录检索同样接受容器路径。
    assert tools.files('/workspace') is not None
    # 映射不放宽边界：相对路径、逃逸、工作区外路径、同前缀的兄弟目录仍被拒绝。
    for value in ['app.py', '/workspace/../escape.py', '/workspace/src/../../escape.py',
                  '/workspace-other/app.py', '/workspaceapp.py']:
        with pytest.raises(ToolRejected):
            tools.read(ReadInput(file_path=value))



@pytest.mark.asyncio
async def test_removed_write_tools_are_rejected_without_changing_source(engine):
    engine, state = engine
    path = engine.workspace.root / 'app.py'
    baseline = path.read_bytes()
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    for name in ['Write', 'Edit', 'NotebookEdit', 'RunTests']:
        with pytest.raises(ToolProtocolError, match='未授权的工具'):
            await pipeline.execute(name, {'file_path': str(path)}, 'denied-' + name)
    assert path.read_bytes() == baseline
    assert not engine.store.operations


@pytest.mark.asyncio
async def test_bash_uses_readonly_scoped_copy_and_discards_all_changes(engine, monkeypatch):
    from tracefix.tools import local

    engine, state = engine
    commands = []
    baseline = (engine.workspace.root / 'app.py').read_bytes()

    async def fake_process(command, timeout):
        commands.append(command)
        if 'run' in command:
            fields = next(csv.reader([command[command.index('--mount') + 1]]))
            assert 'readonly' in fields and 'dst=/workspace' in fields
            staging = Path(next(value[4:] for value in fields if value.startswith('src=')))
            assert not (staging / 'other.txt').exists()
            (staging / 'app.py').write_bytes(b'discarded\n')
            (staging / 'created.py').write_bytes(b'discarded\n')
        return {'passed': True, 'exit_code': 0, 'output': 'done'}

    monkeypatch.setattr(local, 'process', fake_process)
    result = await fenced_bash(LocalTools(engine, worker_context(shell_mode='readonly'), state),
                              BashInput(command='cat app.py', description='只读检查'))
    assert result['files_changed'] == []
    assert '--network=none' in commands[0] and '--read-only' in commands[0]
    assert commands[-1][:3] == ['docker', 'rm', '-f']
    assert (engine.workspace.root / 'app.py').read_bytes() == baseline
    assert not (engine.workspace.root / 'created.py').exists()


@pytest.mark.asyncio
async def test_bash_without_managed_state_never_executes(engine, monkeypatch):
    from tracefix.tools import local

    engine, state = engine

    async def forbidden(*args, **kwargs):
        pytest.fail('缺少受管状态时不能执行 Docker 命令')

    monkeypatch.setattr(local, 'process', forbidden)
    for invalid in [None, SimpleNamespace(mode='repair', phase=Phase.DIAGNOSE)]:
        with pytest.raises(ToolRejected):
            await LocalTools(engine, {}, invalid).bash(BashInput(command='cat app.py', description='拒绝'))
