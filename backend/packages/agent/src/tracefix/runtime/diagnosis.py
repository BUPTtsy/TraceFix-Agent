"""Narrow, evidence-bound diagnosis contracts used before patch proposals."""
from __future__ import annotations

import time
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from tracefix.runtime.contracts import GuidanceAck, digest


class EvidenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str | None = None
    observation_ref: str
    route: str
    window_start: float | None = None
    window_end: float | None = None
    source_manifest: str
    environment_digest: str
    page_generation: int | None = None
    dom_assertions: list[dict] = Field(default_factory=list)
    console: dict = Field(default_factory=lambda: {"status": "unavailable"})
    network: dict = Field(default_factory=lambda: {"status": "unavailable"})
    source_map: dict = Field(default_factory=lambda: {"status": "unavailable"})
    initiator: dict = Field(default_factory=lambda: {"status": "unavailable"})
    observed_at: float = Field(default_factory=time.time)


class CandidatePath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    symbol: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    content_version: str
    status: Literal["current", "stale", "unresolved"] = "current"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    support_refs: list[str] = Field(default_factory=list, max_length=20)
    counterevidence_refs: list[str] = Field(default_factory=list, max_length=20)
    candidate_paths: list[CandidatePath] = Field(default_factory=list, max_length=8)
    prediction: str = Field(default="", max_length=800)
    minimal_probe: str = Field(default="", max_length=800)
    status: Literal["supported", "plausible", "refuted", "unresolved"] = "unresolved"


class DiagnosisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)
    unresolved: list[str] = Field(default_factory=list, max_length=10)
    guidance_ack: list[GuidanceAck] = Field(default_factory=list)


class DiagnosisReport(DiagnosisDraft):
    binding: EvidenceBinding
    source_version: str
    generated_by: Literal["model", "deterministic_unavailable"] = "model"


def binding_from_observation(state, observation: dict, *, action_id: str | None = None,
                             dom_assertions=(), window_start=None, window_end=None):
    """Build the immutable symptom identity from runtime evidence."""
    route = observation.get("url") or state.url
    def channel(name):
        value = observation.get(name)
        coverage = (observation.get("collection") or {}).get("channels", {}).get(name)
        status = coverage.get("status") if isinstance(coverage, dict) else coverage
        status = status or ("available" if isinstance(value, str) else "unavailable")
        result = {"status": status, "ref": (observation.get("raw_refs") or {}).get(name)}
        if isinstance(value, str):
            result.update(content_hash=digest(value), length=len(value))
        return result
    return EvidenceBinding(
        action_id=action_id,
        observation_ref=state.observation_ref or "",
        route=route,
        source_manifest=state.source_manifest,
        environment_digest=state.environment_digest,
        page_generation=observation.get("page_generation"),
        window_start=window_start, window_end=window_end,
        dom_assertions=[item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                        for item in dom_assertions],
        console=channel("console"), network=channel("network"),
        source_map={"status": "unavailable"}, initiator={"status": "unavailable"},
        observed_at=observation.get("observed_at", time.time()),
    )


def symptom_query(goal, assertions, observation):
    terms = [goal]
    terms.extend(item.locator.name for item in assertions)
    network = observation.get("network", "")
    if isinstance(network, str):
        for endpoint in re.findall(r'\[?(?:GET|POST|PUT|PATCH|DELETE)\]?\s+(\S+)', network)[:10]:
            path = urlsplit(endpoint).path
            terms.append(path)
            terms.append(re.sub(r'/\d+(?=/|$)', '', path))
    return "\n".join(dict.fromkeys(term for term in terms if term))


def validate_report(report: DiagnosisReport, *, evidence_refs, allowed_files,
                    source_manifest, environment_digest, binding=None, content_versions=None):
    """Reject invented evidence, paths, and stale source claims before patching."""
    if report.binding.source_manifest != source_manifest:
        raise ValueError("诊断来源版本与当前 source manifest 不一致")
    if report.binding.environment_digest != environment_digest:
        raise ValueError("诊断环境摘要与当前运行不一致")
    if binding is not None and report.binding != binding:
        raise ValueError("诊断症状绑定与当前 observation、route 或时间窗不一致")
    if report.source_version != source_manifest:
        raise ValueError("诊断报告 source version 与当前源码不一致")
    allowed = set(allowed_files)
    known = set(evidence_refs)
    for hypothesis in report.hypotheses:
        invalid = (set(hypothesis.support_refs) | set(hypothesis.counterevidence_refs)) - known
        if invalid:
            raise ValueError("诊断引用了不存在的证据：" + "、".join(sorted(invalid)))
        if hypothesis.status == "supported" and (not hypothesis.support_refs or not hypothesis.candidate_paths or not hypothesis.prediction
                                                   or not hypothesis.minimal_probe):
            raise ValueError("受支持诊断必须包含原始支持证据、区分性预测和最小探针")
        for candidate in hypothesis.candidate_paths:
            if candidate.path not in allowed:
                raise ValueError("诊断候选路径未获授权：" + candidate.path)
            if candidate.line_start is not None and candidate.line_end is not None and candidate.line_end < candidate.line_start:
                raise ValueError("诊断候选行范围无效")
            expected = (content_versions or {}).get(candidate.path, source_manifest)
            if candidate.content_version != expected:
                candidate.status = "stale"
                if hypothesis.status == "supported":
                    hypothesis.status = "unresolved"
    return report
