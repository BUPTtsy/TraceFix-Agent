"""M4 runner 控制协议回归，不依赖本机 Docker daemon。"""

import json
from types import SimpleNamespace

import pytest

from tracefix.agents.claude_sdk_adapter import ExecutionResult, SdkExecutionRequest
from tracefix.agents.claude_sdk_config import ClaudeSdkConfig
from tracefix.execution.agent_runner import AgentExecutionRunner


def runner(tmp_path):
    value = object.__new__(AgentExecutionRunner)
    value.workspace = SimpleNamespace(execution_dir=tmp_path, source=tmp_path / 'source')
    return value


@pytest.mark.asyncio
async def test_interrupt_waits_for_container_writer_to_stop(tmp_path):
    value = runner(tmp_path)
    records = iter([
        {'Config': {'Labels': {'tracefix.execution': 'exec-1'}}, 'State': {'Running': True}},
        {'Config': {'Labels': {'tracefix.execution': 'exec-1'}}, 'State': {'Running': False}},
    ])
    commands = []

    async def inspect(_execution):
        return next(records)

    async def docker(*args, **kwargs):
        commands.append(args)
        return ''

    value._inspect_container = inspect
    value._docker = docker
    receipt = await value.interrupt('exec-1', 'stop-1', grace_seconds=1)
    assert receipt.interrupt_sent and receipt.writers_stopped
    assert receipt.status == 'STOPPED'
    assert commands == [('kill', '--signal', 'SIGUSR1', 'tf-sdk-exec-1')]


@pytest.mark.asyncio
async def test_interrupt_of_already_exited_container_reports_stopped(tmp_path):
    value = runner(tmp_path)

    async def inspect(_execution):
        return {'Config': {'Labels': {'tracefix.execution': 'exec-1'}}, 'State': {'Running': False}}

    value._inspect_container = inspect
    receipt = await value.interrupt('exec-1', 'stop-1', grace_seconds=1)
    assert not receipt.interrupt_sent and receipt.writers_stopped and receipt.status == 'STOPPED'


@pytest.mark.asyncio
async def test_inspect_rejects_container_with_wrong_execution_label(tmp_path):
    value = runner(tmp_path)

    async def docker(*args, **kwargs):
        return json.dumps([{'Config': {'Labels': {'tracefix.execution': 'other'}},
                            'State': {'Running': False}}])

    value._docker = docker
    with pytest.raises(PermissionError, match='execution 标签'):
        await value.inspect('exec-1')


@pytest.mark.asyncio
async def test_resume_reuses_persisted_session_and_preserves_request(tmp_path):
    execution_dir = tmp_path
    (execution_dir / 'control').mkdir()
    (execution_dir / 'state').mkdir()
    request = SdkExecutionRequest.model_construct(
        execution_id='exec-1', session_id='session-1', input_hash='input', role='coder',
        prompt='continue', workspace='/workspace', config=ClaudeSdkConfig.model_construct(
            backend='claude_sdk', model_id='claude-test', credential_ref='env:KEY',
            worker_image='sha256:' + '0' * 64, egress_allowlist=['https://example.test']),
        tools=['Read'])
    (execution_dir / 'control' / 'request.json').write_text(
        request.model_dump_json(), encoding='utf-8')
    prior = ExecutionResult(execution_id='exec-1', status='STOPPED', sdk_session_id='sdk-session-1')
    (execution_dir / 'state' / 'result.json').write_text(prior.model_dump_json(), encoding='utf-8')
    value = runner(execution_dir)
    captured = {}

    async def start(request, emit, *, _resume=False):
        captured.update(request=request, resume=_resume)
        return prior

    value.start = start
    async def inspect(_execution):
        return SimpleNamespace(active=False, status='STOPPED')

    value.inspect = inspect
    result = await value.resume('exec-1')
    assert result is prior
    assert captured['resume'] is True
    assert captured['request'].sdk_session_id == 'sdk-session-1'
    assert (execution_dir / 'control' / 'request.json').is_file()

    await value.resume('exec-1', rebuild=True)
    assert captured['request'].sdk_session_id is None
    assert '保留的工作区' in captured['request'].prompt


@pytest.mark.asyncio
async def test_resume_archives_attempt_outputs_and_preserves_input_manifest(tmp_path):
    value = runner(tmp_path)
    (tmp_path / 'state').mkdir()
    (tmp_path / 'control').mkdir()
    for name in ('state/result.json', 'linux_output_manifest.json', 'output_manifest.json',
                 'control/stop_receipt.json', 'linux_input_manifest.json'):
        (tmp_path / name).write_text(name, encoding='utf-8')
    await value._archive_resume_outputs()
    assert (tmp_path / 'linux_input_manifest.json').read_text() == 'linux_input_manifest.json'
    assert not (tmp_path / 'state' / 'result.json').exists()
    assert (tmp_path / 'state' / 'result.json.attempt-1').is_file()
    assert (tmp_path / 'control' / 'stop_receipt.json.attempt-1').is_file()


@pytest.mark.asyncio
async def test_inspect_rejects_newer_owner_epoch(tmp_path):
    value = runner(tmp_path)
    value.owner_epoch = 2

    async def docker(*args, **kwargs):
        return json.dumps([{'Config': {'Labels': {'tracefix.execution': 'exec-1',
            'tracefix.owner_epoch': '3'}}, 'State': {'Running': True}}])

    value._docker = docker
    with pytest.raises(PermissionError, match='owner epoch'):
        await value.inspect('exec-1')
