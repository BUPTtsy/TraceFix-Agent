import importlib.util
import os
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('isolation_check', Path(__file__).parents[1] / 'isolation_check.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_browser_state_requires_empty_start_and_refreshed_marker():
    initial = {'cookie': '', 'storage': {}, 'tasks': [], 'source': {'source_manifest': 'source'}}
    refreshed = {'cookie': 'tracefix_isolation=current', 'storage': {'tracefix_isolation': 'current'},
                 'tasks': [{'title': 'current'}], 'source': {'source_manifest': 'source'}}
    checker.validate_browser_state(initial, refreshed, 'current', ['previous'], 'source')
    with pytest.raises(AssertionError, match='cookie'):
        checker.validate_browser_state({**initial, 'cookie': 'previous'}, refreshed, 'current', ['previous'], 'source')
    with pytest.raises(AssertionError, match='测试数据'):
        checker.validate_browser_state(initial, {**refreshed, 'tasks': []}, 'current', ['previous'], 'source')
    with pytest.raises(AssertionError, match='前次 marker'):
        checker.validate_browser_state(initial, {**refreshed, 'tasks': [{'title': 'current'}, {'title': 'previous'}]}, 'current', ['previous'], 'source')


def test_sequence_rejects_shared_resources_and_source_drift():
    records = [{key: f'{key}-{index}' for key in ('run_id', 'workspace', 'network', 'volume', 'frozen_registry', 'marker')}
               | {'repo_id': repo, 'commit': repo, 'source_files_hash': repo, 'config_digest': repo,
                  'credential_ref': repo, 'status': 'pass'} for index, repo in enumerate(('alpha', 'beta', 'alpha'))]
    checker.validate_sequence(records)
    records[2]['volume'] = records[0]['volume']
    with pytest.raises(AssertionError, match='volume'):
        checker.validate_sequence(records)


def test_strict_docker_mount_identity_and_readonly(tmp_path):
    container = {'HostConfig': {'Mounts': [{'Type': 'bind', 'Source': str(tmp_path),
                  'Target': '/app', 'ReadOnly': True}]}, 'Mounts': [
        {'Type': 'bind', 'Source': str(tmp_path), 'Destination': '/app', 'RW': False},
        {'Type': 'volume', 'Name': 'current-volume', 'Destination': '/app/node_modules', 'RW': False}]}
    checker.validate_mounts(container, tmp_path, 'current-volume')
    container['Mounts'][1]['Name'] = 'foreign-volume'
    with pytest.raises(AssertionError, match='挂载身份'):
        checker.validate_mounts(container, tmp_path, 'current-volume')
    container['Mounts'][1]['Name'] = 'current-volume'
    container['HostConfig']['Mounts'][0]['ReadOnly'] = False
    with pytest.raises(AssertionError, match='挂载身份'):
        checker.validate_mounts(container, tmp_path, 'current-volume')


@pytest.mark.skipif(os.name != 'nt', reason='Docker Desktop Windows host mapping')
def test_docker_desktop_precise_mapping_rejects_foreign_drive(tmp_path):
    drive = tmp_path.drive[0].lower()
    tail = tmp_path.as_posix()[3:]
    for prefix in ('/host_mnt/', '/run/desktop/mnt/host/'):
        assert checker.mount_source_path(prefix + drive + '/' + tail) == tmp_path.resolve()
        assert checker.mount_source_path(prefix + 'z/' + tail) != tmp_path.resolve()
        assert checker.mount_source_path(prefix + drive + '/' + tail + '-foreign') != tmp_path.resolve()
