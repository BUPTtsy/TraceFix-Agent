import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tracefix.runtime.contracts import Contract, digest


SDK_VERSION = '0.2.168'
CLI_VERSION = '2.1.301'


class ClaudeSdkConfig(Contract):
    backend: Literal['legacy', 'claude_sdk'] = 'legacy'
    model_id: str = ''
    credential_ref: str = ''
    worker_image: str = ''
    egress_allowlist: list[str] = Field(default_factory=list)
    sdk_version: Literal['0.2.168'] = SDK_VERSION
    cli_version: Literal['2.1.301'] = CLI_VERSION
    stop_grace_seconds: float = Field(default=15, gt=0, le=120)
    session_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def validate_backend(self):
        if self.backend == 'legacy':
            return self
        if not self.model_id or self.model_id in {'latest', 'default', 'sonnet', 'opus', 'haiku'}:
            raise ValueError('SDK 后端必须冻结明确的 Claude 模型 ID')
        if not re.fullmatch(r'env:[A-Z][A-Z0-9_]*', self.credential_ref):
            raise ValueError('SDK 凭据必须是 env:NAME 引用，不得传入密钥正文')
        if (not self.worker_image or self.worker_image.endswith(':latest')
                or (':' not in self.worker_image and '@sha256:' not in self.worker_image)):
            raise ValueError('SDK 后端必须配置固定版本的专属 worker 镜像')
        if not self.egress_allowlist:
            raise ValueError('SDK 后端必须显式配置受控出口')
        for origin in self.egress_allowlist:
            parsed = urlsplit(origin)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                    or '*' in origin):
                raise ValueError('SDK 出口必须是精确的 HTTPS origin')
        return self

    @property
    def config_hash(self):
        return digest(self)

    def resolve_credential(self, environment=None):
        source = os.environ if environment is None else environment
        secret = source.get(self.credential_ref.removeprefix('env:'), '')
        if not secret:
            raise ValueError('SDK 凭据引用未解析，拒绝启动')
        return secret


def load_claude_sdk_config(environment=None):
    source = os.environ if environment is None else environment
    return ClaudeSdkConfig(backend=source.get('TRACEFIX_CODING_BACKEND', 'legacy'),
        model_id=source.get('TRACEFIX_CLAUDE_MODEL', ''),
        credential_ref=source.get('TRACEFIX_CLAUDE_CREDENTIAL_REF', ''),
        worker_image=source.get('TRACEFIX_SDK_WORKER_IMAGE', ''),
        egress_allowlist=[item.strip() for item in source.get('TRACEFIX_SDK_EGRESS_ALLOWLIST', '').split(',')
                          if item.strip()],
        stop_grace_seconds=source.get('TRACEFIX_SDK_STOP_GRACE_SECONDS', 15),
        session_timeout_seconds=source.get('TRACEFIX_SDK_SESSION_TIMEOUT_SECONDS') or None)
