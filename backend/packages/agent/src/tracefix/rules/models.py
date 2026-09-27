"""Stable contracts for detection rules.

Rules are user-authored policy data. They are validated and bounded before they
are persisted or put into a model context; they never grant actions or file
access.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from tracefix.runtime.contracts import Contract, digest, new_id


RuleStatus = Literal["draft", "enabled", "disabled", "archived"]
RuleCategory = Literal[
    "functional", "a11y", "console", "network", "visual", "performance",
    "security", "i18n", "code-pattern",
]
RuleSeverity = Literal["blocker", "critical", "major", "minor"]
DetectionType = Literal["oracle", "static", "guided"]
FindingSource = Literal["oracle", "static", "guided", "user"]
FindingStatus = Literal[
    "suspected", "reproduced", "not_reproducible", "fixed", "wont_fix", "false_positive",
]
OracleKind = Literal[
    "console_no_error", "network_status", "dom_assertion", "dom_after_action",
    "a11y_axe", "visual_threshold",
]


class RuleScope(Contract):
    level: Literal["org", "project", "job", "run"] = "project"
    project_ids: list[str] = Field(default_factory=list, max_length=100)
    job_ids: list[str] = Field(default_factory=list, max_length=100)
    run_ids: list[str] = Field(default_factory=list, max_length=100)
    url_patterns: list[str] = Field(default_factory=list, max_length=100)
    path_globs: list[str] = Field(default_factory=list, max_length=100)
    frameworks: list[str] = Field(default_factory=list, max_length=30)


class RuleDetection(Contract):
    type: DetectionType
    oracle: dict[str, Any] | None = None
    static: dict[str, Any] | None = None
    guided: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        payload = getattr(self, self.type)
        if self.type == "oracle" and not payload:
            raise ValueError("oracle 规则必须提供 detection.oracle")
        if self.type == "static" and not payload:
            raise ValueError("static 规则必须提供 detection.static")
        if self.type == "guided" and not payload:
            raise ValueError("guided 规则必须提供 detection.guided")
        if self.type == "oracle":
            kind = payload.get("kind")
            if kind not in {"console_no_error", "network_status", "dom_assertion", "dom_after_action", "a11y_axe", "visual_threshold"}:
                raise ValueError("不支持的 oracle kind")
        return self


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _walk_strings(item)


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk)-[a-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:https?|postgres(?:ql)?|mysql)://[^\s/@]+:[^\s/@]+@"),
)


def contains_sensitive_text(value: Any) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS for text in _walk_strings(value))


class Rule(Contract):
    id: str = Field(default_factory=lambda: new_id("rule"), min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    version: int = Field(default=1, ge=1)
    status: RuleStatus = "draft"
    category: RuleCategory = "functional"
    severity: RuleSeverity = "major"
    priority: int = Field(default=50, ge=1, le=100)
    pinned: bool = False
    scope: RuleScope = Field(default_factory=RuleScope)
    phases: list[str] = Field(default_factory=lambda: ["EXPLORE", "DIAGNOSE", "VERIFY"], max_length=10)
    detection: RuleDetection
    fix_guidance: str = Field(default="", max_length=4096)
    examples: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=30)
    owner: str = Field(default="", max_length=160)
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", value):
            raise ValueError("规则 id 只能包含字母、数字、_、.、:、-")
        return value

    @field_validator("phases", "tags")
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError("规则列表项不能为空")
        return list(dict.fromkeys(item.strip() for item in value))

    @model_validator(mode="after")
    def validate_body(self):
        if contains_sensitive_text(self.model_dump(exclude={"created_at", "updated_at"})):
            raise ValueError("规则内容疑似包含密钥、凭据或带认证信息的地址")
        body_size = len(self.model_dump_json().encode("utf-8"))
        if body_size > 4096:
            raise ValueError("单条规则正文不得超过 4 KiB")
        if self.status == "enabled" and not self.phases:
            raise ValueError("启用规则至少需要一个适用阶段")
        return self

    @property
    def source(self) -> FindingSource:
        return self.detection.type

    @property
    def body_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    def summary(self) -> dict[str, Any]:
        check = self.detection.model_dump(mode="json")
        return {
            "id": self.id, "version": self.version, "severity": self.severity,
            "name": self.name, "check": check, "fix_guidance": self.fix_guidance,
        }


class RuleRef(Contract):
    id: str
    version: int = Field(ge=1)

    def key(self) -> str:
        return f"{self.id}@{self.version}"


class RuleSnapshot(Contract):
    run_id: str
    parent_run_id: str | None = None
    refs: list[RuleRef] = Field(default_factory=list)
    hash: str = ""
    created_at: str | None = None

    @model_validator(mode="after")
    def compute_hash(self):
        expected = digest([ref.model_dump(mode="json") for ref in self.refs])
        if self.hash and self.hash != expected:
            raise ValueError("rule snapshot hash 与规则版本集合不一致")
        self.hash = expected
        return self


class Finding(Contract):
    id: str = Field(default_factory=lambda: new_id("finding"))
    job_id: str
    run_id: str | None = None
    rule_id: str | None = None
    rule_version: int | None = Field(default=None, ge=1)
    source: FindingSource
    severity: RuleSeverity
    title: str = Field(min_length=1, max_length=240)
    location: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    status: FindingStatus = "suspected"
    fingerprint: str
    details: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_failure(cls, *, job_id: str, run_id: str | None, rule: Rule | None,
                     title: str, location: dict[str, Any], evidence_refs: list[str] | None = None,
                     details: dict[str, Any] | None = None, failure_signature: Any = None):
        rule_id = rule.id if rule else None
        rule_version = rule.version if rule else None
        fingerprint = digest([rule_id, location, failure_signature or title])
        return cls(job_id=job_id, run_id=run_id, rule_id=rule_id, rule_version=rule_version,
                   source=rule.source if rule else "user", severity=rule.severity if rule else "minor",
                   title=title, location=location, evidence_refs=evidence_refs or [],
                   fingerprint=fingerprint, details=details or {})
