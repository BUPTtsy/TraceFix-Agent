import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tracefix.service.session import Session
from tracefix.console import dispatch, project_catalog
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.runtime.contracts import Phase, RunState, RunStatus
from tracefix.storage.artifacts import Artifacts


def test_profile_error_binds_to_declared_project_and_keeps_directory_diagnosis(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    registry = tmp_path / 'profiles'
    registry.mkdir()
    (registry / 'projects.yaml').write_text(yaml.safe_dump({'projects': [
        {'id': 'alpha', 'repo_id': 'r-alpha', 'root': str(tmp_path / 'alpha')},
        {'id': 'beta', 'repo_id': 'r-beta', 'root': str(tmp_path / 'beta')}]}), encoding='utf-8')
    (tmp_path / 'alpha' / '.git').mkdir(parents=True)
    # 文件名与项目 ID 故意不同：绑定必须依据文件内的 project 字段。
    (registry / 'alpha-staging.yaml').write_text(yaml.safe_dump({'project': 'alpha', 'source_commit': 'HEAD',
        'commands': {'start': ['node', 'placeholder']}}), encoding='utf-8')
    catalog = {entry['id']: entry for entry in project_catalog(registry / 'projects.yaml')}
    assert not catalog['alpha']['ready'] and '缺少已批准的命令' in catalog['alpha']['issue']
    assert catalog['beta']['issue'] == '目标目录不存在，请先初始化或 clone 项目'


def test_process_exit_does_not_invent_success_or_erase_approval(tmp_path, monkeypatch):
    monkeypatch.setenv('TRACEFIX_CONSOLE_DB', str(tmp_path / 'console.sqlite3'))
    library = DocumentLibrary()
    library.update_run('run1', {'projectId': 'alpha', 'status': 'running'}, create=True)
    assert dispatch('run.ended', {'id': 'run1', 'exitCode': 0})['status'] == 'failed'
    library.update_run('run1', {'status': 'waiting_approval', 'outcome': 'FIX_VERIFIED'})
    record = dispatch('run.ended', {'id': 'run1', 'exitCode': 0})
    assert record['status'] == 'waiting_approval' and record['outcome'] == 'FIX_VERIFIED'


def test_session_publishes_real_state(tmp_path):
    library = DocumentLibrary(tmp_path / 'console.sqlite3')
    library.update_run('console-id', {'projectId': 'alpha', 'status': 'running'}, create=True)
    state = RunState(scope_id='alpha', goal='Test', url='http://app:3000')
    state.phase = Phase.DIAGNOSE
    state.run_status = RunStatus.WAITING_APPROVAL
    session = Session.__new__(Session)
    session.console_run_id, session.run_id, session.documents = 'console-id', state.run_id, library
    session.state = lambda: state
    session.publish_console()
    record = library.run('console-id')
    assert record['agentRunId'] == state.run_id
    assert record['status'] == 'waiting_approval'
    assert 'knowledge' not in record


def test_run_records_never_carry_forged_knowledge(tmp_path, monkeypatch):
    registry = tmp_path / 'projects.yaml'
    root = tmp_path / 'target'
    (root / '.git').mkdir(parents=True)
    registry.write_text(yaml.safe_dump({'projects': [{'id': 'alpha', 'repo_id': 'r-alpha', 'root': str(root)}]}), encoding='utf-8')
    (tmp_path / 'alpha.yaml').write_text(yaml.safe_dump({'project': 'alpha', 'source_commit': 'HEAD',
        'commands': {name: ['node', 'placeholder'] for name in ('start', 'reset', 'static', 'unit', 'build')}}), encoding='utf-8')
    library = DocumentLibrary(tmp_path / 'console.sqlite3')
    forged = [{'artifact_ref': 'secret.json'}]
    created = dispatch('run.create', {'projectId': 'alpha', 'goal': 'Test', 'mode': 'test', 'knowledge': forged},
                       library=library, projects_path=registry, data_root=tmp_path / 'data')
    assert 'knowledge' not in created and 'knowledge' not in library.run(created['id'])
    updated = dispatch('run.update', {'id': created['id'], 'changes': {'status': 'failed', 'knowledge': forged}},
                       library=library, projects_path=registry, data_root=tmp_path / 'data')
    assert updated['status'] == 'failed' and 'knowledge' not in updated
    assert 'documentCount' not in dispatch('projects', {}, library=library, projects_path=registry,
                                           data_root=tmp_path / 'data')[0]


@pytest.fixture
def web_console(tmp_path):
    registry = tmp_path / 'profiles'
    registry.mkdir()
    project_root = tmp_path / 'target'
    (project_root / '.git').mkdir(parents=True)
    (registry / 'projects.yaml').write_text(yaml.safe_dump({'projects': [
        {'id': 'alpha', 'repo_id': 'real-target', 'root': str(project_root)}]}), encoding='utf-8')
    (registry / 'alpha.yaml').write_text(yaml.safe_dump({'project': 'alpha', 'source_commit': 'HEAD',
        'commands': {name: ['node', 'placeholder'] for name in ('start', 'reset', 'static', 'unit', 'build')}}), encoding='utf-8')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    environment = {**os.environ, 'PORT': str(port), 'TRACEFIX_CONSOLE_DB': str(tmp_path / 'console.sqlite3'),
                   'TRACEFIX_PROJECTS': str(registry / 'projects.yaml'), 'TRACEFIX_DATA': str(tmp_path / 'data'),
                   'TRACEFIX_DATABASE_URL': '', 'TRACEFIX_API_KEY': '', 'NODE_ENV': 'production'}
    root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(['node', 'server/index.mjs'], cwd=root / 'backend/apps/console-api', env=environment,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=15, trust_env=False)
    try:
        for attempt in range(50):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode('utf-8'))
            try:
                if client.get('/health').status_code == 200:
                    break
            except httpx.ConnectError:
                time.sleep(.1)
        else:
            pytest.fail('console did not become healthy')
        yield client, environment
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=10)
        process.stderr.close()


def test_http_project_binding_and_failed_launch(web_console):
    client, environment = web_console
    assert client.get('/api/projects').json()[0]['id'] == 'alpha'
    assert client.get('/api/runs').json() == []
    assert client.post('/api/agent/start', json={'projectId': 'unknown', 'goal': 'Test', 'mode': 'test'}).status_code == 400
    launch = client.post('/api/agent/start', json={'projectId': 'alpha', 'goal': '检验缺少配置时真实记录失败', 'mode': 'test'})
    assert launch.status_code == 202
    run_id = launch.json()['id']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        record = client.get('/api/runs/' + run_id).json()
        if record['status'] == 'failed':
            break
        time.sleep(.1)
    assert record['status'] == 'failed'
    assert record['projectId'] == 'alpha'
    assert record['exitCode'] == 2
    assert 'TRACEFIX_DATABASE_URL' in ''.join(record['logs'])
    assert client.get('/api/projects').json()[0]['runCount'] == 1
    assert client.get('/api/runs/' + run_id + '/artifacts/.env').status_code == 400
    reopened = DocumentLibrary(environment['TRACEFIX_CONSOLE_DB'])
    assert reopened.run(run_id)['status'] == 'failed'


def test_control_auth_allows_loopback_ui_reads_and_rejects_cross_origin_writes(web_console):
    client, _ = web_console
    assert client.get('/api/tasks').status_code == 200
    assert client.get('/api/tasks', headers={'Host': 'evil.example'}).status_code == 401
    assert client.get('/api/tasks', headers={'Origin': 'http://evil.example'}).status_code == 401
    assert client.post('/__reset', headers={'Origin': 'http://evil.example'}).status_code == 401
    assert client.post('/__reset', headers={'Origin': str(client.base_url)}).status_code == 200


def test_control_token_and_origin_policy(web_console, tmp_path):
    _, environment = web_console
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    token = 'test-control-token'
    token_environment = {**environment, 'PORT': str(port), 'TRACEFIX_CONTROL_TOKEN': token,
                         'BUGBOARD_DATA': str(tmp_path / 'token-board.json')}
    process = subprocess.Popen(['node', 'server/index.mjs'], cwd=Path(__file__).resolve().parents[1] / 'backend/apps/console-api',
                               env=token_environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=15, trust_env=False)
    try:
        for _ in range(50):
            if process.poll() is not None:
                pytest.fail(process.stderr.read().decode('utf-8'))
            try:
                if client.get('/health').status_code == 200:
                    break
            except httpx.ConnectError:
                time.sleep(.1)
        else:
            pytest.fail('token control server did not become healthy')
        assert client.get('/api/tasks').status_code == 401
        assert client.get('/api/services').status_code == 401
        assert client.get('/api/rules').status_code == 401
        assert client.get('/api/tasks', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.post('/api/chat', json={'message': '健康检查'}).status_code == 401
        assert client.post('/api/tasks', json={'title': '未授权任务'}).status_code == 401
        assert client.post('/__reset').status_code == 401
        headers = {'Authorization': f'Bearer {token}'}
        assert client.post('/__reset', headers={**headers, 'Origin': 'http://evil.example'}).status_code == 401
        assert client.post('/__reset', headers={**headers, 'Origin': str(client.base_url)}).status_code == 200
        assert client.post('/__reset', headers={'X-TraceFix-Control-Token': token}).status_code == 200
        assert client.get('/api/tasks', headers=headers).status_code == 200
        assert client.get('/api/services', headers=headers).status_code == 200
        assert client.get('/api/projects', headers=headers).status_code == 200
        assert client.get('/api/runs', headers=headers).status_code == 200
        assert client.get('/api/rules', headers=headers).status_code == 200
        assert client.get('/api/agent/status', headers=headers).status_code == 200
        chat = client.post('/api/chat', headers=headers, json={'message': '健康检查'})
        assert chat.status_code == 500
        chat_request_id = chat.json()['requestId']
        assert isinstance(chat_request_id, str) and chat_request_id
        assert chat.json() == {'code': 'INTERNAL_ERROR', 'message': '服务暂不可用', 'retryable': False,
                               'requestId': chat_request_id}
        assert 'TRACEFIX_API_KEY' not in chat.text
        invalid = client.post('/api/tasks', headers={**headers, 'Content-Type': 'application/json'}, content='{invalid')
        assert invalid.status_code == 400
        assert invalid.json() == {'code': 'FST_ERR_CTP_INVALID_JSON_BODY',
                                  'message': "Body is not valid JSON but content-type is set to 'application/json'",
                                  'retryable': False, 'requestId': invalid.headers['x-request-id']}
        assert invalid.json()['requestId']
        assert '{invalid' not in invalid.text
        created = client.post('/api/tasks', headers=headers, json={'title': '带令牌创建的任务'})
        assert created.status_code == 201
        assert created.json()['id'] in {task['id'] for task in client.get('/api/tasks', headers=headers).json()}
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=10)
        process.stderr.close()


def test_historical_reports_import_once_and_keep_unknown_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('TRACEFIX_CONSOLE_DB', str(tmp_path / 'console.sqlite3'))
    monkeypatch.setenv('TRACEFIX_DATA', str(tmp_path))
    monkeypatch.setattr('tracefix.console.project_catalog', lambda: [{'id': 'alpha'}])
    artifacts = Artifacts(tmp_path / 'artifacts')
    ref = artifacts.put('alpha', 'run_history', {'run_id': 'run_history', 'scope_id': 'alpha',
        'goal': 'Old run', 'run_status': 'COMPLETED', 'outcome': 'NO_BUG_FOUND'}, label='收尾_修复报告数据')
    assert dispatch('runs.import', {})['imported'] == 1
    assert dispatch('runs.import', {})['imported'] == 0
    record = dispatch('runs', {})[0]
    assert record['mode'] == 'unknown' and record['timeSource'] == 'report_file'
    assert record['reportRef'] == ref
