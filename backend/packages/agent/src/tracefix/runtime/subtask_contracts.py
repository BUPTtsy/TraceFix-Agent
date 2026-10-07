"""宿主只读调查的输入和结果；不携带执行器或持久化句柄。"""
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from tracefix.runtime.contracts import Contract
from tracefix.runtime.diagnosis import Hypothesis


class SubtaskResult(Contract):
    id: str = ''
    status: Literal['completed', 'partial', 'failed', 'timeout', 'rejected'] = 'completed'
    conclusion: str = Field(default='', max_length=1600)
    summary: str = Field(default='', max_length=1500)
    evidence_refs: list[str] = Field(max_length=10)
    files: list[str] = Field(max_length=10)
    suggested_experiments: list[str] = Field(max_length=5)
    unresolved: list[str] = Field(max_length=5)
    artifact_refs: list[str] = Field(default_factory=list, max_length=20)
    usage: dict = Field(default_factory=dict)
    worker_generation: int | None = None
    source_manifest: str = ''
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)
    support_refs: list[str] = Field(default_factory=list, max_length=20)
    counterevidence_refs: list[str] = Field(default_factory=list, max_length=20)
    gaps: list[str] = Field(default_factory=list, max_length=10)
    worker_id: str = ''
    error: dict | None = None

    @property
    def output_summary(self):
        return self.summary or self.conclusion


@dataclass(frozen=True)
class SubtaskSpec:
    goal: str
    role: str
    generation: int
    allowed_files: tuple[str, ...]
    allowed_artifacts: tuple[str, ...]
    depth: int = 1
