"""Opt-in read-only delegation; same gateway, shared parent usage callbacks."""
import asyncio
from dataclasses import dataclass

from pydantic import Field

from tracefix.runtime.contracts import Contract


class SubtaskResult(Contract):
    conclusion: str = Field(max_length=1600)
    evidence_refs: list[str] = Field(max_length=10)
    files: list[str] = Field(max_length=10)
    suggested_experiments: list[str] = Field(max_length=5)
    unresolved: list[str] = Field(max_length=5)


@dataclass(frozen=True)
class SubtaskSpec:
    goal: str
    role: str
    generation: int
    allowed_files: tuple[str, ...]
    allowed_artifacts: tuple[str, ...]
    depth: int = 1


class ReadOnlyWorker:
    def __init__(self, engine):
        self.engine = engine
        self.semaphore = asyncio.Semaphore(2)

    async def run(self, state, spec: SubtaskSpec):
        if spec.depth != 1 or spec.role not in {'code_investigator', 'evidence_reviewer'}:
            raise PermissionError('worker 能力或深度被拒绝')
        if state.phase != 'DIAGNOSE':
            raise PermissionError('worker 仅在诊断阶段可用')
        state.budget = state.budget.charge('subtasks')
        self.engine.store.save(state)
        if not set(spec.allowed_artifacts) <= set(state.evidence_refs):
            raise PermissionError('非本作用域证据')
        cards = [{'path': p, 'content': self.engine.workspace.read(p)} for p in spec.allowed_files]
        # No browser, shell, patch, approval, memory writer or recursive delegation
        # object is exposed to the model; it only gets this schema and frozen input.
        async with self.semaphore:
            result = await self.engine.model_call(state, SubtaskResult,
                {'goal':spec.goal,'role':spec.role,'scope':state.scope_id,'files':cards,
                 'evidence':[self.engine.get(state,r) for r in spec.allowed_artifacts]})
        if spec.generation != state.revision:
            raise ValueError('worker 版本已过期')
        if not set(result.evidence_refs) <= set(spec.allowed_artifacts) or not set(result.files) <= set(spec.allowed_files):
            raise PermissionError('worker 返回了未被授权委派的引用')
        return result

    async def group(self, state, specs):
        async with asyncio.TaskGroup() as group:
            tasks=[group.create_task(self.run(state,s)) for s in specs]
        return [t.result() for t in tasks]
