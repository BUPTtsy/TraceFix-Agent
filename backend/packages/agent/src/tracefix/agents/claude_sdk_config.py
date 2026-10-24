import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tracefix.runtime.contracts import Contract, digest
from tracefix.connectors.context7_mcp import CONTEXT7_ORIGIN, context7_enabled


SDK_VERSION = '0.2.168'
CLI_VERSION = '2.1.301'
ANTHROPIC_ORIGIN = 'https://api.anthropic.com'


def _origin(url):
    parsed = urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc}'.lower()


class ClaudeSdkConfig(Contract):
    # 字段保留在序列化与摘要中，已冻结 SDK Job 的 config_hash 保持不变；编码只有 SDK 一条路径。
    backend: Literal['claude_sdk'] = 'claude_sdk'
    model_id: str = ''
    credential_ref: str = ''
    # 空值不进入序列化与摘要，已冻结的旧 SDK Job 摘要保持不变。
    base_url: str = Field(default='', exclude_if=lambda value: not value)
    worker_image: str = ''
    egress_allowlist: list[str] = Field(default_factory=list)
    sdk_version: Literal['0.2.168'] = SDK_VERSION
    cli_version: Literal['2.1.301'] = CLI_VERSION
    stop_grace_seconds: float = Field(default=15, gt=0, le=120)
    session_timeout_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def validate_backend(self):
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
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or '*' in self.base_url):
                raise ValueError('SDK base_url 必须是不含凭据的 HTTPS 地址')
            if _origin(self.base_url) not in {_origin(origin) for origin in self.egress_allowlist}:
                raise ValueError('SDK base_url 必须位于冻结出口白名单内')
        return self

    @property
    def context7_enabled(self):
        return CONTEXT7_ORIGIN in self.egress_allowlist

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
    """读取 .env（由 launch.py / console-api / CLI 入口载入进程环境）中的 SDK 配置。"""
    source = os.environ if environment is None else environment

    def setting(name):
        return (source.get(name) or '').strip()

    base_url = setting('TRACEFIX_CLAUDE_BASE_URL').rstrip('/')
    legacy_url = setting('TRACEFIX_BASE_URL')
    # 仅当网关与现有 TRACEFIX_BASE_URL 同源时复用其密钥和模型，避免把原服务密钥发往其他主机。
    reuse = bool(base_url and legacy_url and _origin(base_url) == _origin(legacy_url))
    credential_ref = setting('TRACEFIX_CLAUDE_CREDENTIAL_REF')
    if not credential_ref and setting('TRACEFIX_CLAUDE_API_KEY'):
        credential_ref = 'env:TRACEFIX_CLAUDE_API_KEY'
    if not credential_ref and reuse and setting('TRACEFIX_API_KEY'):
        credential_ref = 'env:TRACEFIX_API_KEY'
    allowlist = [item.strip() for item in setting('TRACEFIX_SDK_EGRESS_ALLOWLIST').split(',') if item.strip()]
    enabled = context7_enabled(source)
    if not allowlist:
        allowlist = [_origin(base_url) if base_url else ANTHROPIC_ORIGIN]
        if enabled:
            allowlist.append(CONTEXT7_ORIGIN)
    elif enabled and CONTEXT7_ORIGIN not in allowlist:
        raise ValueError('Context7 MCP 需要在 TRACEFIX_SDK_EGRESS_ALLOWLIST 中放行 ' + CONTEXT7_ORIGIN)
    if not enabled:
        allowlist = [origin for origin in allowlist if origin != CONTEXT7_ORIGIN]
    return ClaudeSdkConfig(model_id=setting('TRACEFIX_CLAUDE_MODEL') or (setting('TRACEFIX_TEXT_MODEL') if reuse else ''),
        credential_ref=credential_ref, base_url=base_url,
        worker_image=setting('TRACEFIX_SDK_WORKER_IMAGE'),
        # 未显式配置白名单时只放行实际模型接口的 origin。
        egress_allowlist=allowlist,
        stop_grace_seconds=setting('TRACEFIX_SDK_STOP_GRACE_SECONDS') or 15,
        session_timeout_seconds=setting('TRACEFIX_SDK_SESSION_TIMEOUT_SECONDS') or None)
