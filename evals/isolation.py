"""Evaluator-owned checks for registered Agent execution surfaces."""

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol


AGENT_SURFACES = frozenset({'workspace', 'tools', 'memory', 'skills', 'retrieval', 'cache', 'reports'})


class IsolationError(ValueError):
    pass


class IsolationBoundary(Protocol):
    def verify(self, private_root: Path, private_files: tuple[Path, ...]) -> dict:
        ...


def _plain_path(path: Path) -> Path:
    path = Path(path).absolute()
    if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)):
        raise IsolationError('linked execution surface')
    return path.resolve()


def _overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


@dataclass(frozen=True)
class AgentExposure:
    """Complete inventory supplied by the trusted Agent launcher, never by a model."""

    roots: Mapping[str, tuple[Path, ...]]
    container_ids: tuple[str, ...] = ()
    host_process_ids: tuple[int, ...] = ()
    expected_image_ids: Mapping[str, str] = field(default_factory=dict)
    network_boundary_evidence: str = ''
    cache_snapshot_evidence: str = ''
    retrieval_snapshot_evidence: str = ''

    def check_paths(self, private_root: Path, private_files: tuple[Path, ...]) -> tuple[Path, ...]:
        if set(self.roots) != AGENT_SURFACES:
            raise IsolationError('incomplete Agent surface inventory')
        private_root = _plain_path(private_root)
        protected = (private_root, *(_plain_path(path) for path in private_files))
        roots = tuple(_plain_path(path) for paths in self.roots.values() for path in paths)
        for root in roots:
            if any(_overlaps(root, path) for path in protected):
                raise IsolationError('Agent surface exposes evaluator data')
            if not root.exists():
                raise IsolationError('Agent surface cannot be inspected')
        return roots


def _check_copies(roots: tuple[Path, ...], private_files: tuple[Path, ...]):
    fingerprints = {}
    for path in private_files:
        if path.is_file() and path.stat().st_size:
            fingerprints.setdefault(path.stat().st_size, set()).add(hashlib.sha256(path.read_bytes()).hexdigest())
    for root in set(roots):
        files = [root] if root.is_file() else []
        if root.is_dir():
            for directory, subdirectories, names in os.walk(root, followlinks=False):
                for name in (*subdirectories, *names):
                    path = Path(directory) / name
                    if path.is_symlink() or path.is_junction():
                        raise IsolationError('linked file in Agent surface')
                files.extend(Path(directory) / name for name in names)
        for path in files:
            hashes = fingerprints.get(path.stat().st_size, set())
            if hashes and hashlib.sha256(path.read_bytes()).hexdigest() in hashes:
                raise IsolationError('held-out copy in Agent surface')


@dataclass(frozen=True)
class HostAgentBoundary:
    def verify(self, private_root: Path, private_files: tuple[Path, ...]) -> dict:
        raise IsolationError('host Agent requires a separately enforced process identity or container boundary')


@dataclass(frozen=True)
class DockerAgentBoundary:
    """Inspect actual containers; host repair/model/tool processes remain unsupported."""

    exposure: AgentExposure
    docker_executable: str = 'docker'
    inspect_timeout: float = 30

    def verify(self, private_root: Path, private_files: tuple[Path, ...]) -> dict:
        roots = self.exposure.check_paths(private_root, private_files)
        if self.exposure.host_process_ids or not self.exposure.container_ids:
            raise IsolationError('Agent execution includes an unisolated host process')
        if len(set(self.exposure.container_ids)) != len(self.exposure.container_ids):
            raise IsolationError('duplicate container identity')
        if (set(self.exposure.expected_image_ids) != set(self.exposure.container_ids)
                or any(not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)
                       for image in self.exposure.expected_image_ids.values())):
            raise IsolationError('trusted clean image digest evidence is missing')
        if not all((self.exposure.network_boundary_evidence,
                    self.exposure.cache_snapshot_evidence,
                    self.exposure.retrieval_snapshot_evidence)):
            raise IsolationError('launcher network/cache/retrieval evidence is missing')
        result = subprocess.run(
            [self.docker_executable, 'container', 'inspect', *self.exposure.container_ids],
            capture_output=True, check=False, timeout=self.inspect_timeout,
        )
        if result.returncode:
            raise IsolationError('Agent containers unavailable')
        try:
            records = json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise IsolationError('container inspect is not JSON') from error
        if not isinstance(records, list) or len(records) != len(self.exposure.container_ids):
            raise IsolationError('container inventory mismatch')
        private_root = _plain_path(private_root)
        mounted = []
        for identity, record in zip(self.exposure.container_ids, records):
            if not isinstance(record, dict) or record.get('Id') != identity:
                raise IsolationError('container identity mismatch')
            if record.get('Image') != self.exposure.expected_image_ids[identity]:
                raise IsolationError('container image does not match registered clean digest')
            config = record.get('Config') or {}
            host = record.get('HostConfig') or {}
            state = record.get('State') or {}
            user = str(config.get('User', '')).split(':')[0]
            security = host.get('SecurityOpt') or []
            if (state.get('Running') is not True or user in {'', '0', 'root'}
                    or host.get('Privileged') is not False
                    or host.get('ReadonlyRootfs') is not True
                    or 'ALL' not in (host.get('CapDrop') or []) or host.get('CapAdd')
                    or not any(option in {'no-new-privileges', 'no-new-privileges:true'} for option in security)
                    or host.get('PidMode') not in {'', 'private', None}
                    or host.get('IpcMode') == 'host' or host.get('NetworkMode') == 'host'
                    or host.get('VolumesFrom') or host.get('Devices') or host.get('DeviceRequests')):
                raise IsolationError('Agent container permits host access')
            mounts = record.get('Mounts')
            if not isinstance(mounts, list):
                raise IsolationError('container mounts unavailable')
            for mount in mounts:
                if not isinstance(mount, dict):
                    raise IsolationError('invalid container mount')
                if mount.get('Type') == 'tmpfs':
                    continue
                if mount.get('Type') != 'bind' or not isinstance(mount.get('Source'), str):
                    raise IsolationError('uninspectable Agent mount')
                source = _plain_path(Path(mount['Source']))
                if (_overlaps(source, private_root)
                        or any(_overlaps(source, _plain_path(path)) for path in private_files)
                        or any(token in str(source).casefold() for token in ('docker.sock', 'docker_engine', '\\pipe\\'))
                        or str(source).replace('\\', '/') in {'/proc', '/sys', '/dev', '/var/run'}):
                    raise IsolationError('container mount exposes evaluator or host controls')
                if not source.exists():
                    raise IsolationError('container mount source unavailable')
                mounted.append(source)
        _check_copies((*roots, *mounted), private_files)
        fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        return {'kind': 'docker_container_inspect', 'real_isolation': True,
                'container_ids': list(self.exposure.container_ids),
                'inspect_hash': fingerprint, 'surfaces': sorted(self.exposure.roots),
                'mounted_sources': [str(path) for path in mounted],
                'network_boundary_evidence': self.exposure.network_boundary_evidence,
                'cache_snapshot_evidence': self.exposure.cache_snapshot_evidence,
                'retrieval_snapshot_evidence': self.exposure.retrieval_snapshot_evidence,
                'clean_image_ids': dict(self.exposure.expected_image_ids)}
