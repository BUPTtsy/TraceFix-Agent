from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tracefix.config import load_projects
from tracefix.repository_registry import RepositoryRegistry
from tracefix.service.results import ServiceError


OFFICIAL_GITHUB_VERSION = 'v2.0.2'
OFFICIAL_GITHUB_IMAGE = ('ghcr.io/github/github-mcp-server@sha256:'
                         'ffced0d76e77428532a2ced185516992d77fd146d69eec2f244e6237d1a97796')


@dataclass(frozen=True)
class ServiceSettings:
    data_root: Path
    console_db: Path
    database_url: str = field(default='', repr=False)
    credential_env: dict[str, str] = field(default_factory=dict, repr=False)
    github_timeout: float = 30
    github_max_bytes: int = 1_048_576

    @classmethod
    def from_environment(cls):
        data_root = os.getenv('TRACEFIX_SERVICE_DATA_ROOT')
        console_db = os.getenv('TRACEFIX_SERVICE_CONSOLE_DB')
        if not data_root or not console_db:
            raise ServiceError('configuration_missing', '服务需要显式 data root 和 console DB')
        try:
            mapping = json.loads(os.getenv('TRACEFIX_SERVICE_GITHUB_CREDENTIAL_MAP', '{}'))
            if not isinstance(mapping, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                   for key, value in mapping.items()):
                raise ValueError
        except (ValueError, TypeError):
            raise ServiceError('configuration_invalid', '部署 credential 映射无效') from None
        return cls(Path(data_root).resolve(), Path(console_db).resolve(),
                   os.getenv('TRACEFIX_SERVICE_DATABASE_URL', ''), mapping)

    def resolve_credential(self, reference):
        key = self.credential_env.get(reference)
        value = os.getenv(key) if key and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) else None
        if not value:
            raise ServiceError('credential_unavailable', '冻结 credential_ref 未配置部署凭据')
        return {'GITHUB_PERSONAL_ACCESS_TOKEN': value}


def validate_snapshot(request, settings):
    root = settings.data_root.resolve()
    registry = root / 'frozen-repositories' / request.run_id / 'projects.yaml'
    if not registry.resolve().is_relative_to(root):
        raise ServiceError('snapshot_invalid', '冻结注册表路径越界')
    try:
        projects = load_projects(registry)
        if len(projects) != 1 or request.repo_id not in projects:
            raise ValueError
        config = projects[request.repo_id].repository_config
        if not config or config.get('frozen_run_id') != request.run_id or not config.get('enabled'):
            raise ValueError
        authoritative = RepositoryRegistry._frozen_result(config, registry)
        supplied = request.repository_snapshot.model_dump(exclude_none=True)
        if any(authoritative.get(key) != value for key, value in supplied.items()):
            raise ValueError
        return authoritative
    except (OSError, ValueError, TypeError, KeyError):
        raise ServiceError('snapshot_invalid', '请求快照与部署侧冻结配置不一致') from None
