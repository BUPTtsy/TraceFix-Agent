"""评测 Docker broker 的组隔离、恢复生命周期与超时；不调用真实 Docker。"""
import json
import subprocess
import sys
import time

import pytest

from evals.docker_broker import BrokerRejected, DockerBroker
from evals.docker_adapter import public_worker_config
from evals.config import EvaluationConfig


IMAGE = 'sha256:' + '1' * 64


@pytest.fixture
def broker(tmp_path):
    return DockerBroker(tmp_path, {'app': IMAGE}, resource_prefix='tf-testgroup')


def run_arguments(name='tf-testgroup-app'):
    return ['run', '--rm', '--name', name, '--network=none', '--user', '1000:1000',
            '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', 'app', 'node']


def execute(broker, arguments, monkeypatch, *, program='print("ok")'):
    directory = broker.channel / ('%032x' % (len(list(broker.channel.iterdir())) + 1))
    directory.mkdir()
    (directory / 'request.json').write_text(json.dumps({'arguments': arguments, 'stream': False}))
    (directory / 'stdin-closed').touch()
    original = subprocess.Popen
    monkeypatch.setattr('evals.docker_broker.subprocess.Popen',
                        lambda *_args, **kwargs: original([sys.executable, '-c', program], **kwargs))
    broker._execute(directory)
    return directory


@pytest.mark.parametrize('arguments', [run_arguments('tf-othergroup-app'),
    ['network', 'create', '--internal', '--label', 'tracefix.run=other', 'tf-othergroup-net'],
    ['volume', 'create', '--label', 'tracefix.run=other', 'tf-othergroup-deps'],
    ['rm', '-f', 'tf-othergroup-app'], ['exec', 'tf-othergroup-app', 'cat', '/etc/passwd']])
def test_foreign_group_resources_are_rejected(broker, arguments):
    with pytest.raises(BrokerRejected):
        broker._control(arguments)
    assert not broker.containers and not broker.networks and not broker.volumes


def test_auto_removed_container_can_be_reused_after_nonzero_exit(broker, monkeypatch):
    first = execute(broker, run_arguments(), monkeypatch, program='raise SystemExit(1)')
    assert json.loads((first / 'done.json').read_text())['returncode'] == 1
    assert not broker.containers
    monkeypatch.undo()
    second = execute(broker, run_arguments(), monkeypatch)
    assert json.loads((second / 'done.json').read_text())['returncode'] == 0
    assert not broker.containers


def test_dependency_store_recreation_is_fresh(broker, monkeypatch):
    create = ['volume', 'create', '--label', 'tracefix.run=testgroup', 'tf-testgroup-deps']
    execute(broker, create, monkeypatch)
    old = broker.volumes['tf-testgroup-deps']
    (old / 'stale').write_text('previous execution')
    assert not broker.containers
    broker._control(['volume', 'rm', 'tf-testgroup-deps'])
    monkeypatch.undo()
    execute(broker, create, monkeypatch)
    assert broker.volumes['tf-testgroup-deps'] != old
    assert not (broker.volumes['tf-testgroup-deps'] / 'stale').exists()


def test_timeout_is_enforced_after_stdin_closes(broker, monkeypatch):
    broker.timeout = 0.2
    started = time.monotonic()
    directory = execute(broker, run_arguments(), monkeypatch, program='import time;time.sleep(30)')
    assert time.monotonic() - started < 5
    assert json.loads((directory / 'done.json').read_text())['returncode'] != 0


def test_close_only_removes_registered_resources_in_this_group(broker, monkeypatch):
    broker.validate(run_arguments())
    broker._control(['network', 'create', '--internal', '--label', 'tracefix.run=testgroup', 'tf-testgroup-net'])
    calls = []
    monkeypatch.setattr('evals.docker_broker.subprocess.run', lambda arguments, **kwargs: calls.append(arguments))
    broker.close()
    assert calls == [['docker', 'rm', '-f', 'tf-testgroup-app'], ['docker', 'network', 'rm', 'tf-testgroup-net']]


def test_broker_rejects_new_launch_after_close(broker, monkeypatch):
    broker.close()
    directory = execute(broker, run_arguments(), monkeypatch)
    assert (directory / 'error.json').is_file()
    assert not broker.containers


def test_unregistered_mount_and_privileged_launch_are_rejected(broker):
    for option in ('--privileged', '--pid=host', '--volume', '--device'):
        with pytest.raises(BrokerRejected):
            broker.validate(['run', option, *run_arguments()[1:]])
    with pytest.raises(BrokerRejected):
        broker._mount('type=bind,src=/agent-run/private,dst=/app')


def test_runtime_equal_resource_flags_still_validate_values(broker):
    translated = broker.validate([*run_arguments()[:-2], '--pids-limit=128', '--memory=1g',
                                  '--cpus=2', *run_arguments()[-2:]])
    assert translated[translated.index('--pids-limit') + 1] == '128'
    assert translated[translated.index('--cpus') + 1] == '2'
    broker.containers.clear()
    with pytest.raises(BrokerRejected):
        broker.validate([*run_arguments()[:-2], '--memory=32g', *run_arguments()[-2:]])


def test_worker_configuration_never_contains_private_mutation_recipe(tmp_path):
    config = EvaluationConfig(case_id='private-case', family='private-family',
        source_root=tmp_path, source_revision='a' * 40, profile=tmp_path / 'profile',
        spec=tmp_path / 'spec', public_protocol=tmp_path / 'protocol', skills_root=tmp_path,
        initial_experience=tmp_path / 'memory', goal='公开用户操作期望', model='fixture',
        seed=0, tools_hash='tools', recovery_rules_hash='recovery', known_bug=True,
        mutations=({'file': 'src/private.ts', 'before': 'private-before-marker',
                    'after': 'private-after-marker'},))
    public = public_worker_config(config, {'source_root': tmp_path / 'public'})
    payload = json.dumps(public.payload())
    for private in ('private-case', 'private-family', 'private-before-marker',
                    'private-after-marker', 'src/private.ts', 'a' * 40):
        assert private not in payload
    assert public.case_mutations() == ()
    assert public.known_bug is None and public.goal == config.goal
