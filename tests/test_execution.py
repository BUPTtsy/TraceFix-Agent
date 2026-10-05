import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tracefix.config import Profile
from tracefix.execution.browser import (MCPActionUnknown, MCPBrowser, MCPConnectionError,
                                        assertions, resolve_locator)
from tracefix.execution.platforms import bind_mount, container_user
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace, git, is_frozen_path
from tracefix.runtime.contracts import Assertion, BrowserAction, FileEdit, Locator, PatchProposal, Phase, RunState, TestSpec as Spec, digest
from tracefix.runtime.smoke import make_engine


def test_snapshot_exact_locator_and_stale_ref():
    snapshot='- button "Save" [ref=e2]\n- checkbox "Done" [ref=e3] [checked]'
    assert resolve_locator(snapshot,Locator(role='button',name='Save'))=='e2'
    assert assertions(snapshot,[Assertion(locator=Locator(role='checkbox',name='Done'),condition='checked')])['passed']
    with pytest.raises(ValueError): resolve_locator(snapshot+'\n- button "Save" [ref=e4]',Locator(role='button',name='Save'))
    policy=Policy(['http://app:3000'])
    s=RunState(scope_id='b',goal='save the task',url='http://app:3000',phase=Phase.EXPLORE)
    spec=Spec(goal=s.goal,assertions=[Assertion(locator=Locator(role='button',name='Save'))],regression_assertions=[Assertion(locator=Locator(role='button',name='Save'))])
    a=BrowserAction(kind='click',locator=Locator(role='button',name='Save'),observation_id='old',element_ref='e2')
    with pytest.raises(PermissionError): policy.browser(s,a,spec,{'id':'new','snapshot':snapshot})


def test_mcp_diagnostic_arguments_follow_discovered_required_schema():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.tools = {
        'browser_console_messages': {'type': 'object', 'properties': {
            'level': {'type': 'string', 'enum': ['error', 'warning', 'info', 'debug']}},
            'required': ['level'], 'additionalProperties': False},
        'browser_network_requests': {'type': 'object', 'properties': {
            'includeStatic': {'type': 'boolean'}}, 'required': ['includeStatic'],
            'additionalProperties': False},
    }
    assert browser.diagnostic_args('console') == {'level': 'info'}
    assert browser.diagnostic_args('network') == {'includeStatic': False}


def test_mcp_diagnostic_arguments_reject_unknown_required_schema():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.tools = {'browser_console_messages': {
        'type': 'object', 'properties': {'futureOption': {'type': 'string'}},
        'required': ['futureOption'], 'additionalProperties': False}}
    with pytest.raises(RuntimeError, match='futureOption'):
        browser.diagnostic_args('console')


@pytest.mark.parametrize('filler_length', [100, 40000])
async def test_mcp_snapshot_preserves_page_header_and_footer(filler_length):
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    header = 'Page URL: http://app:3000\n- button "Header" [ref=top]\n'
    footer = '\n- button "Footer" [ref=bottom]'
    snapshot = header + 'middle ' * filler_length + footer

    async def call(kind, args=None):
        if kind == 'snapshot':
            return [SimpleNamespace(type='text', text=snapshot)]
        assert kind == 'screenshot'
        return [SimpleNamespace(type='image', data=base64.b64encode(b'png').decode())]

    browser.call = call
    observation = await browser.observe()
    assert observation['snapshot'].startswith(header)
    assert observation['snapshot'].endswith(footer)
    assert observation['snapshot'] == snapshot
    metadata = observation['collection']['channels']['snapshot']
    assert metadata['status'] == 'available'
    assert metadata['collector_truncated'] is False
    assert metadata['provider_characters'] == len(snapshot)
    assert metadata['collector_characters'] == len(snapshot)
    assert resolve_locator(observation['snapshot'], Locator(role='button', name='Header')) == 'top'
    assert resolve_locator(observation['snapshot'], Locator(role='button', name='Footer')) == 'bottom'


@pytest.mark.parametrize('entrypoint', ['call', 'action'])
async def test_mcp_undispatched_failure_reconnects_once_without_replaying(entrypoint):
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.connection_state = 'lost'
    browser.worker = asyncio.get_running_loop().create_future()
    browser.worker.set_result(None)
    browser.observation = {'id': 'old', 'snapshot': 'stale'}

    async def reconnect():
        await browser.close()
        browser.connection_state = 'ready'
        browser.tools = {'browser_navigate': {'type': 'object'}}

    browser.reconnect = AsyncMock(side_effect=reconnect)
    action = BrowserAction(kind='navigate', value='http://app:3000')
    browser._execute_action = AsyncMock(wraps=browser._execute_action)
    with pytest.raises(MCPConnectionError) as error:
        if entrypoint == 'call':
            await browser.call('navigate', {'url': action.value})
        else:
            await browser.action(action)

    browser.reconnect.assert_awaited_once_with()
    assert browser._execute_action.await_count == (1 if entrypoint == 'action' else 0)
    assert browser.queue.empty()
    assert browser.connection_state == 'ready'
    assert browser.observation is None
    assert error.value.details['dispatched'] is False
    assert error.value.details['reconnect_attempted'] is True
    assert error.value.details['reconnected'] is True
    assert error.value.details['requires_new_observation'] is True
    assert error.value.retryable is False
    if entrypoint == 'action':
        assert browser.last_action['status'] == 'WAITING_NETWORK'
        assert browser.last_action['details']['reconnected'] is True


async def test_mcp_action_direct_undispatched_failure_reconnects_without_replaying():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.worker = asyncio.get_running_loop().create_future()
    browser.worker.set_result(None)
    original_error = MCPConnectionError('unavailable', details={'dispatched': False})
    browser._execute_action = AsyncMock(side_effect=original_error)
    browser.reconnect = AsyncMock()
    action = BrowserAction(kind='navigate', value='http://app:3000')
    with pytest.raises(MCPConnectionError) as error:
        await browser.action(action)
    assert error.value is original_error
    browser._execute_action.assert_awaited_once_with(action)
    browser.reconnect.assert_awaited_once_with()


@pytest.mark.parametrize('dispatched', [False, True])
async def test_mcp_call_transport_failure_reconnects_only_before_dispatch(dispatched):
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.connection_state = 'ready'
    browser.tools = {'browser_navigate': {'type': 'object'}}
    calls = []

    async def worker_loop():
        request = await browser.queue.get()
        calls.append(request.name)
        request.dispatched = dispatched
        request.result.set_exception(ConnectionError('transport closed'))

    browser.worker = asyncio.create_task(worker_loop())
    browser.reconnect = AsyncMock()
    with pytest.raises(MCPConnectionError) as error:
        await browser.call('navigate', {'url': 'http://app:3000'})
    assert error.value.details['dispatched'] is dispatched
    assert calls == ['browser_navigate']
    assert browser.reconnect.await_count == (0 if dispatched else 1)
    await browser.close()


async def test_mcp_failed_reconnect_preserves_original_undispatched_failure():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.worker = asyncio.get_running_loop().create_future()
    browser.worker.set_result(None)
    original_error = MCPConnectionError('unavailable', details={'dispatched': False})
    browser._execute_action = AsyncMock(side_effect=original_error)
    browser.reconnect = AsyncMock(side_effect=OSError('startup unavailable'))
    with pytest.raises(MCPConnectionError) as error:
        await browser.action(BrowserAction(kind='navigate', value='http://app:3000'))
    assert error.value is original_error
    assert error.value.details['reconnect_attempted'] is True
    assert error.value.details['reconnected'] is False
    assert error.value.details['reconnect_error'] == 'OSError: startup unavailable'
    assert browser.connection_state == 'lost'
    assert browser.last_action['status'] == 'WAITING_NETWORK'
    browser.reconnect.assert_awaited_once_with()
    browser._execute_action.assert_awaited_once()


async def test_mcp_side_effect_is_unknown_after_transport_loss_and_never_retried():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.observation = {'id': 'obs-1', 'snapshot': '- button "Save" [ref=e1]'}
    calls = []

    async def execute(action):
        calls.append(action.kind)
        raise MCPConnectionError('connection closed after dispatch')

    browser._execute_action = execute
    action = BrowserAction(kind='click', locator=Locator(role='button', name='Save'),
                           observation_id='obs-1', element_ref='e1')

    with pytest.raises(MCPActionUnknown) as error:
        await browser.action(action)

    assert calls == ['click']
    assert browser.observation is None
    assert browser.connection_state == 'lost'
    assert browser.last_action['status'] == 'UNKNOWN'
    assert error.value.kind == 'click'
    assert error.value.status == 'UNKNOWN_OPERATION'
    assert error.value.category == 'browser_action_unknown'
    assert error.value.retryable is False
    assert error.value.details['requires_manual_review'] is True
    assert error.value.details['action_args_digest'] == browser.last_action['args_digest']
    assert error.value.details['observation_id'] == 'obs-1'


async def test_mcp_action_not_sent_remains_waiting_network_instead_of_unknown():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.reconnect = AsyncMock()

    async def unavailable(action):
        raise MCPConnectionError('connection unavailable before dispatch', details={'dispatched': False})

    browser._execute_action = unavailable
    action = BrowserAction(kind='navigate', value='http://app:3000')
    with pytest.raises(MCPConnectionError) as error:
        await browser.action(action)
    assert not isinstance(error.value, MCPActionUnknown)
    assert error.value.status == 'WAITING_NETWORK'
    assert error.value.details['dispatched'] is False
    assert browser.last_action['status'] == 'WAITING_NETWORK'
    browser.reconnect.assert_not_awaited()


async def test_mcp_observation_is_invalidated_when_read_only_call_loses_connection():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.observation = {'id': 'obs-1', 'snapshot': 'old'}

    async def disconnected(*args, **kwargs):
        raise MCPConnectionError('transport closed')

    browser.call = disconnected
    with pytest.raises(MCPConnectionError):
        await browser.observe()
    assert browser.observation is None


async def test_mcp_rejects_element_action_after_reconnect_without_new_observation():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    action = BrowserAction(kind='click', locator=Locator(role='button', name='Save'),
                           observation_id='old', element_ref='e1')
    with pytest.raises(PermissionError, match='页面观测已过期'):
        await browser.action(action)


async def test_mcp_navigation_remains_available_after_observation_reset():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.observation = None
    browser.connection_state = 'ready'
    called = []

    async def navigate(action):
        called.append(action.kind)
        return {'id': 'fresh'}

    browser._execute_action = navigate
    result = await browser.action(BrowserAction(kind='navigate', value='http://app:3000'))
    assert result == {'id': 'fresh'}
    assert called == ['navigate']


async def test_mcp_press_requires_current_observation_after_reconnect():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    with pytest.raises(PermissionError, match='页面观测已过期'):
        await browser.action(BrowserAction(kind='press', value='Enter', observation_id='old'))


def test_mcp_connection_error_codes_are_classified_without_retrying_tools():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    error = SimpleNamespace(error=SimpleNamespace(code=-32000))
    assert browser._is_connection_error(error)
    assert browser._is_connection_error(TimeoutError('read timeout'))
    assert not browser._is_connection_error(ValueError('invalid schema'))


def test_mcp_connection_error_exposes_structured_recovery_details():
    error = MCPConnectionError('read timeout', details={'tool': 'browser_snapshot', 'dispatched': True})
    assert error.status == 'WAITING_NETWORK'
    assert error.category == 'browser_transport_error'
    assert error.retryable is False
    assert error.details == {
        'status': 'WAITING_NETWORK',
        'operation_status': 'WAITING_NETWORK',
        'category': 'browser_transport_error',
        'retryable': False,
        'requires_manual_review': True,
        'tool': 'browser_snapshot',
        'dispatched': True,
    }


async def test_mcp_close_discards_queued_request_before_reconnect():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.queue.put_nowait(object())
    worker = asyncio.create_task(asyncio.sleep(60))
    browser.worker = worker
    await browser.close()
    assert browser.queue.empty()
    assert worker.done()


async def test_mcp_dispatched_action_stays_unknown_if_followup_observe_disconnects():
    browser = MCPBrowser(['unused'], Policy(['http://app:3000']))
    browser.reconnect = AsyncMock()
    browser.connection_state = 'ready'
    browser.tools = {'browser_snapshot': {'type': 'object'},
        'browser_click': {'type': 'object', 'properties': {
        'element': {'type': 'string'}, 'ref': {'type': 'string'}},
        'required': ['element', 'ref']}}
    browser.observation = {'id': 'obs-1', 'snapshot': '- button "Save" [ref=e1]'}

    requests = []

    async def worker_loop():
        for content in ([SimpleNamespace(type='text',
            text='Page URL: http://app:3000\n- button "Save" [ref=e2]')], []):
            request = await browser.queue.get()
            requests.append(request)
            request.dispatched = True
            request.result.set_result(SimpleNamespace(isError=False, content=content))

    browser.worker = asyncio.create_task(worker_loop())

    async def disconnected_observe():
        raise MCPConnectionError('snapshot connection closed', details={'dispatched': False})

    browser._observe = disconnected_observe
    action = BrowserAction(kind='click', locator=Locator(role='button', name='Save'),
                           observation_id='obs-1', element_ref='e1')
    with pytest.raises(MCPActionUnknown) as error:
        await browser.action(action)
    assert error.value.status == 'UNKNOWN_OPERATION'
    assert error.value.details['dispatched'] is True
    assert [request.name for request in requests] == ['browser_snapshot', 'browser_click']
    assert requests[1].args['ref'] == 'e2'
    assert browser.last_action['status'] == 'UNKNOWN'
    assert browser.observation is None
    browser.reconnect.assert_not_awaited()
    await browser.close()


@pytest.mark.parametrize('url',['http://169.254.169.254/','file:///etc/passwd','http://app.evil:3000','http://app:3001','http://user:pass@app:3000'])
def test_url_allowlist(url):
    with pytest.raises(PermissionError): Policy(['http://app:3000']).url(url)


def test_patch_preflights_all_files_and_frozen_tests(tmp_path):
    (tmp_path/'src').mkdir();(tmp_path/'src/a.ts').write_text('before');(tmp_path/'src/b.ts').write_text('before')
    (tmp_path/'src/a.test.ts').write_text('assert');(tmp_path/'src/tests').mkdir();(tmp_path/'src/tests/a.ts').write_text('assert')
    git(tmp_path,'init','-q');git(tmp_path,'add','.');git(tmp_path,'-c','user.name=CI','-c','user.email=ci@local','commit','-qm','init')
    w=Workspace(tmp_path,['src/**'])
    patch=PatchProposal(summary='change',evidence_refs=['e'],edits=[FileEdit(path='src/a.ts',before_hash=digest(b'before'),content='after'),FileEdit(path='src/b.ts',before_hash='wrong',content='after')])
    with pytest.raises(ValueError): w.apply(patch)
    assert w.read('src/a.ts')=='before'
    for path in ['src/a.test.ts','src/tests/a.ts','../secret','.git/config']:
        with pytest.raises(PermissionError): w.path(path,True)
    patch.edits=patch.edits[:1]
    receipt=w.apply(patch)
    assert w.reconcile(patch)==receipt
    assert '+after' in w.diff()


@pytest.mark.parametrize('path',['src/latest-run/status.ts','src/blockchain/tx.ts','src/components/Testimonial.tsx',
                                 'src/protest/handler.ts','src/specification.md','src/unblocked.ts','src/Blocker.tsx',
                                 # 锁文件策略只认文件名形态；lock 不是通用词元，业务代码不该被拦。
                                 'src/useLock.ts','src/LockService.ts','src/lock/manager.ts','src/locks/registry.ts'])
def test_frozen_policy_allows_paths_that_merely_contain_the_words(tmp_path, path):
    assert not is_frozen_path(path)
    (tmp_path/path).parent.mkdir(parents=True, exist_ok=True);(tmp_path/path).write_text('source')
    assert Workspace(tmp_path,['src/**']).path(path,True).exists()


@pytest.mark.parametrize('path',['src/foo.test.ts','src/foo_test.py','src/test_foo.py','src/foo.spec.js',
                                 'src/tests/helper.py','src/test/helper.py','src/__tests__/helper.ts','src/spec/helper.rb',
                                 'src/oracle/expected.json','src/tests/fixtures/oracle.json','src/Foo.Test.TS','src/test2.py',
                                 # camelCase/PascalCase 边界也必须切词，否则 JUnit 式命名会漏网。
                                 'src/UserServiceTest.java','src/FooTest.java','src/userTest.ts','src/MyComponentSpec.js',
                                 'src/fooSpec.rb','src/conftest.py',
                                 'src/package-lock.json','src/poetry.lock','src/yarn.lock','src/Cargo.lock',
                                 'src/pnpm-lock.yaml','src/npm-shrinkwrap.json'])
def test_frozen_policy_still_blocks_real_tests_oracles_and_locks(tmp_path, path):
    assert is_frozen_path(path)
    (tmp_path/path).parent.mkdir(parents=True, exist_ok=True);(tmp_path/path).write_text('assert')
    with pytest.raises(PermissionError): Workspace(tmp_path,['src/**']).path(path,True)


async def test_runner_prepares_nested_mountpoints_before_readonly_bind(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'engine')
    workspace = engine.workspace
    snapshot = workspace.require_repository_snapshot()
    assert state.source_manifest == digest(snapshot)
    for name in ('dist', 'node_modules'):
        assert (workspace.root / name).is_dir()
        assert not any((workspace.root / name).iterdir())
        entry = snapshot['entries'][name]
        assert entry['kind'] == 'directory' and entry['tracked'] is False and entry['content'] == ''
    profile = Profile(project='b', source_commit='HEAD', image='tracefix-bugboard:1.0',
        browser_image='tracefix-browser:1.0', commands={
            'start': ['node', 'scripts/serve.mjs'],
            **{name: ['node', name] for name in ('reset', 'static', 'unit', 'build')}})
    runner = DockerRunner(profile, workspace, 'run_mounts')
    app_image_id, browser_image_id = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
    browser_command = runner.browser_command()
    browser_image_index = browser_command.index('--headless') - 1
    assert browser_command[browser_image_index] == ''
    assert profile.browser_image not in browser_command
    calls = []
    image_call = ('image', 'inspect', profile.image, profile.browser_image)
    network_call = ('network', 'create', '--internal', '--label',
                    'tracefix.run=' + runner.run_id, runner.network)
    start_call = ('run', '-d', '--name', runner.name, '--network', runner.network,
        '--network-alias', 'app', '--label', 'tracefix.run=' + runner.run_id,
        '--user', container_user(), '--cap-drop=ALL', '--security-opt=no-new-privileges',
        '--pids-limit=128', '--memory=1g', '--cpus=2', '--read-only',
        '--tmpfs', '/tmp:rw,nosuid,size=128m,mode=1777',
        '--tmpfs', '/app/dist:rw,nosuid,size=128m,mode=1777',
        '--mount', bind_mount(workspace.root),
        '--mount', 'type=volume,dst=/app/node_modules,readonly',
        '-e', 'NODE_PATH=/deps/node_modules', '-e', 'TRACEFIX_SOURCE=' + state.source_manifest,
        app_image_id, *profile.commands['start'])
    health_code = "let ok=false;for(let i=0;i<30;i++){try{const r=await fetch(process.argv[1]);if(r.ok){ok=true;break}}catch{}await new Promise(r=>setTimeout(r,500))}if(!ok)process.exit(1);console.log('健康检查通过')"
    health_call = ('exec', runner.name, 'node', '--input-type=module', '-e', health_code,
                   f'http://127.0.0.1:{profile.port}{profile.health_path}')
    version_call = ('exec', runner.name, 'node', '--input-type=module', '-e',
        "console.log(await (await fetch(process.argv[1])).text())",
        f'http://127.0.0.1:{profile.port}{profile.version_path}')
    runtime_call = ('container', 'inspect', runner.name, runner.name + '-browser')
    outputs = {
        image_call: json.dumps([{'Id': app_image_id}, {'Id': browser_image_id}]),
        network_call: 'c' * 64,
        start_call: 'd' * 64,
        health_call: '健康检查通过\n',
        version_call: json.dumps({'source_manifest': state.source_manifest}),
        runtime_call: json.dumps([
            {'Name': '/' + name, 'Id': container_id, 'Image': image_id,
             'State': {'Running': True},
             'Config': {'Image': image_id, 'Labels': {'tracefix.run': runner.run_id}},
             'NetworkSettings': {'Networks': {runner.network: {'NetworkID': 'c' * 64}}},
             'HostConfig': {'NetworkMode': runner.network}}
            for name, container_id, image_id in (
                (runner.name, 'd' * 64, app_image_id),
                (runner.name + '-browser', 'e' * 64, browser_image_id))]),
    }

    async def docker(*args, **kwargs):
        workspace.check_frozen(snapshot)
        calls.append(args)
        # 仅接受完整的已批准参数，未知命令或脚本不得被成功 mock 掩盖。
        assert args in outputs, f'非预期 Docker 调用：{args!r}'
        assert kwargs == ({'check': False} if args == health_call else {})
        return {'passed': True, 'exit_code': 0, 'output': outputs[args]}

    monkeypatch.setattr(runner, 'docker', docker)
    # 先解析镜像，再创建隔离网络；预先持有的浏览器命令原位绑定固定 ID。
    environment_digest = await runner.inspect_images()
    assert environment_digest == digest({
        'profile': profile.model_dump(), 'image_ids': [app_image_id, browser_image_id]})
    assert calls == [image_call]
    assert runner.resolved_image_ids == {'app': app_image_id, 'browser': browser_image_id}
    assert runner.browser_command() is browser_command
    assert browser_command[browser_image_index] == browser_image_id
    assert profile.browser_image not in browser_command
    assert (await runner.start(state.source_manifest))['passed']
    assert len(calls) == 4
    assert calls[0] == image_call
    assert calls[1][:2] == ('network', 'create')
    assert calls[2][0] == 'run'
    assert '--read-only' in calls[2]
    assert '/app/dist:rw,nosuid,size=128m,mode=1777' in calls[2]
    assert 'type=volume,dst=/app/node_modules,readonly' in calls[2]
    assert 'TRACEFIX_SOURCE=' + state.source_manifest in calls[2]
    assert calls[2][-3:] == (app_image_id, *profile.commands['start'])
    assert profile.image not in calls[2]
    assert calls[3][:2] == ('exec', runner.name)
    assert await runner.version() == state.source_manifest
    assert await runner.inspect_images(runtime=True) == environment_digest
    assert calls == [image_call, network_call, start_call, health_call, version_call, runtime_call]


def test_runner_rejects_non_directory_mountpoint(tmp_path):
    (tmp_path / 'dist').write_text('not a directory', encoding='utf-8')
    runner = DockerRunner(SimpleNamespace(), Workspace(tmp_path, ['src/**']), 'run_mounts')
    with pytest.raises(PermissionError, match='dist'):
        runner.prepare_mountpoints()
