import json
import os
from types import SimpleNamespace

import pytest

from evals.isolation import AGENT_SURFACES, AgentExposure, DockerAgentBoundary, IsolationError, _mount_source


def container_record(identity, image, source):
    return {'Id': identity, 'Image': image, 'State': {'Running': True},
            'Config': {'User': '1000:1000'},
            'HostConfig': {'Privileged': False, 'ReadonlyRootfs': True,
                           'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true']},
            'Mounts': [{'Type': 'bind', 'Source': str(source)}]}


def test_real_inventory_rejects_private_mount_and_host_execution(tmp_path, monkeypatch):
    private = tmp_path / 'private'
    private.mkdir()
    script = private / 'oracle.py'
    script.write_text('private evaluator sentinel', encoding='utf-8')
    public = tmp_path / 'public'
    public.mkdir()
    identity, image = 'a' * 64, 'sha256:' + 'b' * 64
    exposure = AgentExposure(roots={surface: (public,) for surface in AGENT_SURFACES},
        container_ids=(identity,), expected_image_ids={identity: image},
        network_boundary_evidence='isolated-network-inspect',
        cache_snapshot_evidence='empty-cache', retrieval_snapshot_evidence='empty-retrieval')
    records = [container_record(identity, image, public)]
    monkeypatch.setattr('evals.isolation.subprocess.run', lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(records)))
    assert DockerAgentBoundary(exposure).verify(private, (script,))['real_isolation']
    records[0]['Mounts'][0]['Source'] = str(private)
    with pytest.raises(IsolationError, match='exposes evaluator'):
        DockerAgentBoundary(exposure).verify(private, (script,))
    records[0]['Mounts'][0]['Source'] = str(public)
    records[0]['HostConfig']['ReadonlyRootfs'] = False
    with pytest.raises(IsolationError, match='host access'):
        DockerAgentBoundary(exposure).verify(private, (script,))


@pytest.mark.skipif(os.name != 'nt', reason='Docker Desktop Windows mount translation')
def test_desktop_mount_translation_preserves_private_directory_detection(tmp_path):
    path = tmp_path.resolve()
    drive = path.drive.rstrip(':').lower()
    tail = path.as_posix().split(':', 1)[1]
    for prefix in ('/run/desktop/mnt/host/', '/host_mnt/'):
        assert _mount_source(prefix + drive + tail) == path
