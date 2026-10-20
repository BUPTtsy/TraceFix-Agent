from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import AliasChoices, ConfigDict, Field, model_validator

from tracefix.connectors.github_mcp import GitHubReadRequest, validate_snapshot_binding
from tracefix.runtime.contracts import Contract, TestSpec
from tracefix.runtime.finding_acceptance import FindingAcceptance


_IDENTITY = r'^[A-Za-z0-9_-]{1,80}$'
_SECRET_KEYS = frozenset({'api_key', 'apikey', 'access_token', 'token', 'password', 'secret',
                          'authorization', 'credentials', 'client_secret', 'private_key', 'cookie'})
_TOKEN = re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}')


def reject_credentials(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold().replace('-', '_') in _SECRET_KEYS:
                raise ValueError('服务请求不接受明文凭据')
            reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            reject_credentials(item)
    elif isinstance(value, str) and _TOKEN.search(value):
        raise ValueError('服务请求不接受明文凭据')


class ServiceTestSpec(TestSpec):
    model_config = ConfigDict(extra='forbid')


class ExecutionRequest(Contract):
    mode: Literal['batch'] = Field(default='batch', validation_alias=AliasChoices('executionMode', 'mode'))
    url: str | None = Field(default=None, max_length=2048)
    spec: ServiceTestSpec | None = None
    repair_mode: Literal['hybrid', 'pipeline'] | None = Field(
        default=None, validation_alias=AliasChoices('repairMode', 'repair_mode'))

    @model_validator(mode='before')
    @classmethod
    def matching_aliases(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for public, internal in (('executionMode', 'mode'), ('repairMode', 'repair_mode')):
                if public in value and internal in value:
                    if value[public] != value[internal]:
                        raise ValueError(f'{public} 与 {internal} 不能矛盾')
                    value.pop(internal)
        return value


class GitHubRead(Contract):
    source_kind: Literal['file', 'commit', 'issue', 'pull_request']
    path: str | None = Field(default=None, max_length=1024)
    number: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode='after')
    def controlled_read(self):
        GitHubReadRequest(**self.model_dump())
        if self.source_kind != 'file' and self.path is not None:
            raise ValueError('只有 file 读取可以指定 path')
        return self


class RepositorySnapshot(Contract):
    repo_id: str = Field(pattern=_IDENTITY)
    projectId: str
    commit: str = Field(pattern=r'^[a-f0-9]{40}$|^[a-f0-9]{64}$')
    config_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    repository_config: dict[str, Any]
    projects_path: str
    profile_path: str
    snapshot: dict[str, Any] | None = None
    registry: str | None = None
    profile: str | None = None

    @model_validator(mode='after')
    def frozen_binding(self):
        validate_snapshot_binding(self.model_dump(exclude_none=True))
        if self.snapshot is not None and self.snapshot != self.repository_config:
            raise ValueError('冻结快照副本不一致')
        if self.registry is not None and self.registry != self.projects_path:
            raise ValueError('冻结注册表路径不一致')
        if self.profile is not None and self.profile != self.profile_path:
            raise ValueError('冻结 Profile 路径不一致')
        return self


class ServiceRequest(Contract):
    schema_version: Literal[1]
    task_id: str = Field(pattern=_IDENTITY)
    run_id: str = Field(pattern=_IDENTITY)
    repo_id: str = Field(pattern=_IDENTITY)
    mode: Literal['test', 'repair']
    goal: str = Field(min_length=5, max_length=16384)
    execution: ExecutionRequest
    additional_rule_ids: list[str] = Field(default_factory=list, max_length=256)
    repository_snapshot: RepositorySnapshot
    source_run_id: str | None = Field(default=None, pattern=_IDENTITY)
    source_finding_ids: list[str] = Field(default_factory=list, max_length=256)
    github_reads: list[GitHubRead] = Field(default_factory=list, max_length=64)
    finding_acceptance: FindingAcceptance | None = None

    @model_validator(mode='before')
    @classmethod
    def no_plaintext_credentials(cls, value):
        reject_credentials(value)
        if isinstance(value, dict) and type(value.get('schema_version')) is not int:
            raise ValueError('schema_version 必须为整数 1')
        return value

    @model_validator(mode='after')
    def consistent_identity(self):
        snapshot = self.repository_snapshot
        if (snapshot.repo_id != self.repo_id or snapshot.projectId != self.repo_id
                or snapshot.repository_config.get('frozen_run_id') != self.run_id):
            raise ValueError('请求仓库/Run 与冻结快照身份不一致')
        if bool(self.source_run_id) != bool(self.source_finding_ids):
            raise ValueError('source Run 与 Finding 集合必须一起指定')
        if self.source_run_id and self.mode != 'repair':
            raise ValueError('来源 Finding 只可用于独立 Repair')
        if self.finding_acceptance is not None and not self.source_run_id:
            raise ValueError('Finding 验收必须绑定来源 Run')
        if self.finding_acceptance is not None:
            accepted = self.finding_acceptance
            if (accepted.run_id != self.run_id or accepted.repo_id != self.repo_id
                    or accepted.commit != snapshot.commit
                    or accepted.source_run_id != self.source_run_id
                    or [item.finding_id for item in accepted.findings] != self.source_finding_ids):
                raise ValueError('Finding 验收与服务 Run、revision 或来源不一致')
            if self.execution.spec is not None and self.execution.spec.model_dump(mode='json') != accepted.spec.model_dump(mode='json'):
                raise ValueError('execution.spec 不能覆盖冻结验收规范')
        for values in (self.additional_rule_ids, self.source_finding_ids):
            if any(not item.strip() or len(item) > 160 for item in values) or len(values) != len(set(values)):
                raise ValueError('规则和 Finding 标识必须非空且唯一')
        return self
