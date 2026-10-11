import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from prompt_toolkit.document import Document
from rich.console import Console

from tracefix.cli.input import CommandCompleter
from tracefix.cli.main import Session, application
from tracefix.cli.registry import parse
from tracefix.console import dispatch
from tracefix.knowledge.documents import ConflictError, DocumentLibrary
from tracefix.model.chat import stream_chat
from tracefix.model import protocol
from tracefix.runtime.contracts import RunState, RunStatus
from tracefix.runtime.smoke import make_engine


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    monkeypatch.delenv('TRACEFIX_DATABASE_URL', raising=False)
    registry = tmp_path / 'profiles'
    registry.mkdir()
    projects = []
    for name in ('alpha', 'beta'):
        target = tmp_path / name
        (target / '.git').mkdir(parents=True)
        projects.append({'id': name, 'root': str(target), 'repo_id': name + '-repo'})
        (registry / (name + '.yaml')).write_text(yaml.safe_dump({'project': name, 'source_commit': 'HEAD',
            'commands': {command: ['node', name] for command in ('start', 'reset', 'static', 'unit', 'build')}}), encoding='utf-8')
    (registry / 'projects.yaml').write_text(yaml.safe_dump({'projects': projects}), encoding='utf-8')
    args = SimpleNamespace(project='alpha', projects=str(registry / 'projects.yaml'), profile=None,
                           console_db=str(tmp_path / 'console.sqlite3'), data=str(tmp_path / 'data'),
                           goal='', mode='test', plain=True, url=None, spec=None, skills='skills', run=False, command=None)
    session = Session(args, None, None)
    output = io.StringIO()
    session.render.console = Console(file=output, no_color=True, markup=False, width=120)
    session.output = output
    return session


def web(session, operation, fields=None):
    return dispatch(operation, fields or {}, library=DocumentLibrary(session.args.console_db),
                    projects_path=session.args.projects, data_root=session.args.data)


def fields(**changes):
    return {'title': '刷新状态测试', 'content': '验证 PATCH 请求成功后，刷新页面检查状态。',
            'kind': 'testing', 'projectId': 'alpha', 'enabled': True, 'tags': ['React'], **changes}


async def test_cli_import_and_web_edit_share_versions_and_retrieval(session, tmp_path):
    source = tmp_path / '测试 方法.md'
    source.write_text('检查 PATCH 响应与刷新后的状态。', encoding='utf-8')
    await session.dispatch(f'/knowledge import "{source}" --title "刷新状态测试" --kind testing --tags React,持久化')
    record = web(session, 'documents', {'projectId': 'alpha'})[0]
    current = web(session, 'document', {'id': record['id']})
    assert current['version'] == 1 and current['content'] == source.read_text(encoding='utf-8')
    updated = web(session, 'document.save', {**current, 'content': '通过全新页面验证 PATCH 状态。'})
    await session.dispatch('/knowledge show ' + record['id'])
    assert '通过全新页面验证' in session.output.getvalue()
    with pytest.raises(ConflictError):
        await session.dispatch(f'/knowledge edit {record["id"]} --file "{source}" --version 1')
    assert web(session, 'document', {'id': record['id']})['version'] == 2
    await session.dispatch('/knowledge disable ' + record['id'])
    assert web(session, 'search', {'projectId': 'alpha', 'query': 'PATCH'}) == []
    await session.dispatch('/knowledge enable ' + record['id'])
    assert web(session, 'search', {'projectId': 'alpha', 'query': 'PATCH'})[0]['id'] == record['id']
    assert updated['version'] == 2


async def test_project_switch_updates_profile_and_scopes_knowledge(session):
    own = web(session, 'document.save', fields())
    shared = web(session, 'document.save', fields(projectId=None, title='全局 PATCH'))
    await session.dispatch('/projects use beta')
    assert session.scope == 'beta'
    assert Path(session.args.profile).name == 'beta.yaml'
    assert session.project_profile().project == 'beta'
    records = session.workspace_commands.call('search', {'projectId': 'beta', 'query': 'PATCH'})
    assert [record['id'] for record in records] == [shared['id']]
    with pytest.raises(PermissionError):
        await session.dispatch('/knowledge show ' + own['id'])
    await session.dispatch('/scope use alpha')
    assert session.project_profile().project == 'alpha'


async def test_file_export_is_exact_and_never_overwrites(session, tmp_path):
    record = web(session, 'document.save', fields(content='# 标题\n\n原始文本 <script>example</script>\n'))
    destination = tmp_path / '导出 经验.md'
    await session.dispatch(f'/knowledge export {record["id"]} "{destination}"')
    assert destination.read_text(encoding='utf-8') == record['content']
    with pytest.raises(FileExistsError):
        await session.dispatch(f'/knowledge export {record["id"]} "{destination}"')
    with pytest.raises(ValueError, match='版本'):
        await session.dispatch(f'/knowledge edit {record["id"]} --file "{destination}"')


async def test_external_editor_conflict_preserves_draft(session, tmp_path, monkeypatch):
    record = web(session, 'document.save', fields())
    draft = tmp_path / 'edited-draft.md'
    draft.write_text('编辑器中的更新', encoding='utf-8')

    def editor(content, directory):
        web(session, 'document.save', {**record, 'content': 'Web 同时保存的更新'})
        return draft.read_text(encoding='utf-8'), draft

    monkeypatch.setattr('tracefix.cli.workspace.edit_document', editor)
    with pytest.raises(ConflictError):
        await session.dispatch('/knowledge edit ' + record['id'])
    assert draft.exists()
    assert '草稿保留' in session.output.getvalue()
    assert web(session, 'document', {'id': record['id']})['content'] == 'Web 同时保存的更新'


async def test_new_and_edit_commands_share_metadata_and_scope(session, tmp_path):
    source = tmp_path / 'new.md'
    source.write_text('检查浏览器 PATCH 请求。', encoding='utf-8')
    await session.dispatch(f'/knowledge new "修复经验" --file "{source}" --disabled')
    record = web(session, 'documents', {'projectId': 'alpha'})[0]
    assert not record['enabled']
    await session.dispatch(f'/knowledge edit {record["id"]} --title "共享修复方法" --kind repair --tags PATCH,React --scope global')
    shared = web(session, 'document', {'id': record['id']})
    assert shared['title'] == '共享修复方法' and shared['kind'] == 'repair'
    assert shared['projectId'] is None and shared['tags'] == ['PATCH', 'React']
    source.write_text('更新后的修复方法。', encoding='utf-8')
    await session.dispatch(f'/knowledge edit {record["id"]} --file "{source}" --version 2')
    assert web(session, 'document', {'id': record['id']})['content'] == '更新后的修复方法。'


async def test_cli_failed_launch_is_visible_to_web_without_postgres(session):
    session.goal = '复现刷新状态丢失'
    with pytest.raises(ValueError, match='TRACEFIX_DATABASE_URL'):
        await session.create()
    record = web(session, 'runs')[0]
    assert record['origin'] == 'cli' and record['status'] == 'failed'
    assert record['goal'] == session.goal and record['projectId'] == 'alpha'
    await session.dispatch('/status')
    assert 'TRACEFIX_DATABASE_URL' in session.output.getvalue()


async def test_web_launch_is_adopted_without_duplicate_record(session, monkeypatch):
    record = web(session, 'run.create', {'projectId': 'alpha', 'goal': 'Web goal', 'mode': 'test'})
    session.web_console_run_id = record['id']
    session.goal = 'Web goal'
    with pytest.raises(ValueError, match='TRACEFIX_DATABASE_URL'):
        await session.create()
    assert len(web(session, 'runs')) == 1
    assert web(session, 'run', {'id': record['id']})['status'] == 'running'
    ended = web(session, 'run.ended', {'id': record['id'], 'exitCode': 2})
    assert ended['status'] == 'failed' and ended['exitCode'] == 2


async def test_run_sources_export_and_experience_keep_shared_links(session, tmp_path):
    state = RunState(scope_id='alpha', goal='验证完成状态', mode='repair', url='http://app:3000')
    session.goal = state.goal
    session.begin_console_run(state)
    session.run_id = state.run_id
    session.state = lambda: state
    state.run_status = RunStatus.COMPLETED
    state.report_ref = session.artifacts.put('alpha', state.run_id, {'run_id': state.run_id}, label='修复报告数据')
    selection = {'phase': 'DIAGNOSE', 'queries': ['刷新'], 'documents': [{'id': 'doc-1', 'title': '经验', 'version': 2}],
                 'artifact_ref': session.artifacts.put('alpha', state.run_id, {'snapshot': '原始经验片段'}, label='知识库检索')}
    session.publish_console({'type': 'knowledge.selected', 'payload': selection})
    record = web(session, 'runs')[0]
    assert record['status'] == 'completed' and record['pid'] is None
    assert record['knowledge'][0]['documents'][0]['version'] == 2
    await session.dispatch('/memory sources')
    assert 'doc-1' in session.output.getvalue()
    exported = tmp_path / 'source.json'
    await session.dispatch(f'/runs export {state.run_id} {selection["artifact_ref"]} "{exported}"')
    assert json.loads(exported.read_text(encoding='utf-8')) == {'snapshot': '原始经验片段'}
    await session.dispatch('/runs remember ' + state.run_id)
    document = web(session, 'documents', {'projectId': 'alpha'})[0]
    assert document['sourceRunId'] == record['id'] and document['enabled'] is False


async def test_cli_full_engine_publishes_lifecycle_to_shared_history(session, tmp_path):
    engine, state = make_engine(tmp_path / 'fake-engine', bugfree=True)
    session.scope, session.store, session.engine, session.run_id = 'b', engine.store, engine, state.run_id
    session.args.data = str(tmp_path / 'fake-engine')
    session.begin_console_run(state)
    engine.notify = session.notify
    await session.drive(state)
    record = session.documents.run(session.console_run_id)
    assert record['agentRunId'] == state.run_id
    assert record['status'] == 'completed' and record['outcome'] == 'NO_BUG_FOUND'
    assert record['reportRef'] and record['logs'] and record['pid'] is None


async def test_damaged_evidence_copy_is_recorded_in_the_run_trace(session, tmp_path):
    engine, state = make_engine(tmp_path / 'evidence-engine')
    session.scope, session.store, session.engine, session.run_id = 'b', engine.store, engine, state.run_id
    engine.notify = session.notify
    engine.store.save(state)
    snapshot = {'commit': 'fake-ci-source', 'files': {}}
    ref = session.artifacts.put('b', state.run_id, snapshot, label='源码快照')
    session.artifacts._path('b', state.run_id, ref).write_bytes(b'{"tampered": true}')
    assert session.artifacts.put('b', state.run_id, snapshot, label='源码快照') != ref
    assert '证据告警' in session.output.getvalue()
    damaged = [event for event in engine.store.trace(state.run_id, 'b') if event['type'] == 'evidence.damaged']
    assert [event['payload']['reason'] for event in damaged] == ['证据校验和不匹配']
    assert damaged[0]['payload']['ref'] == ref and damaged[0]['payload']['scope'] == 'b'


def test_windows_paths_and_new_subcommand_completion():
    command, args = parse('/knowledge import "C:\\知识 库\\回归.md" --tags React,刷新')
    assert command == 'knowledge' and args[1] == 'C:\\知识 库\\回归.md'
    assert list(CommandCompleter().get_completions(Document('/knowledge im'), None))[0].text == 'import'
    assert list(CommandCompleter().get_completions(Document('/mode ch'), None))[0].text == 'chat'


async def test_invalid_workspace_command_does_not_exit_session(session):
    with pytest.raises(ValueError):
        await session.dispatch('/knowledge edit')
    await session.dispatch('/knowledge help')
    assert '--version N' in session.output.getvalue()
    await session.dispatch('/projects list')
    assert 'alpha' in session.output.getvalue()


async def test_management_application_never_connects_to_postgres(session, monkeypatch):
    monkeypatch.setattr('tracefix.cli.main.PostgresStore', lambda *_: pytest.fail('unexpected PostgreSQL connection'))
    monkeypatch.setenv('TRACEFIX_DATABASE_URL', 'configured-but-not-needed')
    session.args.command = ['/projects list', '/knowledge list', '/runs list']
    await application(session.args)


def test_noninteractive_commands_work_without_web_database_or_key(session):
    root = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items() if not key.startswith('TRACEFIX_')}
    environment['PYTHONIOENCODING'] = 'utf-8'
    response = subprocess.run([sys.executable, '-m', 'tracefix.cli.main', '--plain', '--project', 'alpha',
        '--projects', session.args.projects, '--data', session.args.data, '--console-db', session.args.console_db,
        '--command', '/projects list', '--command', '/knowledge list'], cwd=root, env=environment,
        capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert response.returncode == 0, response.stderr
    assert 'alpha-repo' in response.stdout and '知识文档' in response.stdout
    assert '\x1b' not in response.stdout


async def test_chat_streams_scoped_references_with_identity(session, monkeypatch):
    own = web(session, 'document.save', fields())
    web(session, 'document.save', fields(projectId='beta', title='其他项目 PATCH'))
    received = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = 'data: ' + json.dumps({'model': 'fixture',
                'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '检查'}}]},
                ensure_ascii=False) + '\n\n'
            raw = payload.encode('utf-8')
            yield raw[:len(raw) - 9]
            yield raw[len(raw) - 9:]
            yield (b'data: {"model":"fixture","choices":[{"index":0,"delta":{"content":" PATCH"}}]}\n\n'
                   b'data: {"model":"fixture","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                   b'data: [DONE]\n\n')

    def provider(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Stream())

    client_class = httpx.AsyncClient
    monkeypatch.setenv('TRACEFIX_API_KEY', 'test-fixture-key')
    monkeypatch.setattr(protocol, 'create_http_client', lambda boundary: client_class(
        transport=protocol.BoundaryTransport(httpx.MockTransport(provider), boundary),
        event_hooks={'request': [boundary.before], 'response': [boundary.received]}))
    events = [event async for event in stream_chat('PATCH 刷新', [], 'alpha', session.documents)]
    assert events[0]['sources'][0]['id'] == own['id']
    assert ''.join(event.get('delta', '') for event in events) == '检查 PATCH'
    messages = received[0]['messages']
    assert messages[0]['role'] == 'system' and 'TraceFix' in messages[0]['content']
    assert '其他项目 PATCH' not in str(messages)
    assert 'test-fixture-key' not in str(messages)


async def test_chat_histories_stay_separate_and_skip_runtime(session, monkeypatch):
    histories = []

    async def fake_stream(message, history, project, library, use_knowledge):
        histories.append((project, list(history), use_knowledge))
        yield {'delta': '逐段'}
        yield {'delta': '回答'}

    monkeypatch.setattr('tracefix.cli.main.stream_chat', fake_stream)
    await session.dispatch('/mode chat')
    await session.dispatch('如何验证状态？')
    await session.dispatch('/projects use beta')
    await session.dispatch('/chat --no-knowledge 测试方法')
    assert histories[0][1] == [] and histories[1] == ('beta', [], False)
    assert session.store is None and session.documents.runs() == []
    assert '逐段回答' in session.output.getvalue()
    await session.dispatch('/chat clear')
    assert session.chat_history['beta'] == [] and session.chat_history['alpha']
