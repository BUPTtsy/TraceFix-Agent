"""四格实验的公开固定输入；候选补丁是每组运行输出。"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from tracefix.runtime.contracts import digest


@dataclass(frozen=True)
class AblationGroup:
    agent_mode: str
    cross_run_memory: bool

    @property
    def name(self):
        return f'{self.agent_mode}-memory-{"on" if self.cross_run_memory else "off"}'


GROUPS = tuple(AblationGroup(mode, memory) for mode in ('single', 'multi')
               for memory in (False, True))


@dataclass(frozen=True)
class EvaluationConfig:
    case_id: str
    family: str
    source_root: Path
    source_revision: str
    profile: Path
    spec: Path
    public_protocol: Path
    skills_root: Path
    initial_experience: Path
    goal: str
    model: str
    seed: int
    tools_hash: str
    recovery_rules_hash: str
    allowed_files: tuple[str, ...] = ('src/**', 'server/**')
    project: str = 'bugboard'
    effort: str = 'provider-default'
    vision_model: str = ''
    thinking: str = 'disabled'
    tool_mode: str = 'native'
    held_out: bool = True
    mutation_file: str | None = None
    mutation_before: str | None = None
    mutation_after: str | None = None
    timeout_seconds: int = 1800
    command: tuple[str, ...] = ()

    def __post_init__(self):
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', self.source_revision):
            raise ValueError('source_revision 必须是完整提交 SHA')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', self.project):
            raise ValueError('project 标识无效')
        if not all((self.case_id, self.family, self.goal, self.model,
                    self.tools_hash, self.recovery_rules_hash)):
            raise ValueError('固定输入字段不能为空')
        if type(self.seed) is not int or self.timeout_seconds <= 0:
            raise ValueError('seed 或 timeout 无效')
        if self.tool_mode not in {'native', 'json'} or self.thinking not in {'enabled', 'disabled'}:
            raise ValueError('工具模式或 thinking 无效')
        mutation = (self.mutation_file, self.mutation_before, self.mutation_after)
        if any(value is not None for value in mutation) and not all(value is not None for value in mutation):
            raise ValueError('缺陷注入必须提供 file/before/after')
        if self.mutation_file and (Path(self.mutation_file).is_absolute()
                                   or '..' in Path(self.mutation_file).parts):
            raise ValueError('缺陷注入路径必须位于源码内')
        if any(not isinstance(item, str) or not item for item in self.command):
            raise ValueError('command 必须是非空字符串数组')

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        values = json.loads(path.read_text(encoding='utf-8-sig'))
        for field in ('source_root', 'profile', 'spec', 'public_protocol',
                      'skills_root', 'initial_experience'):
            value = Path(values[field])
            values[field] = (path.parent / value).resolve()
        if 'allowed_files' in values:
            values['allowed_files'] = tuple(values['allowed_files'])
        if 'command' in values:
            values['command'] = tuple(values['command'])
        return cls(**values)

    def payload(self):
        return {key: str(value) if isinstance(value, Path) else value
                for key, value in asdict(self).items()}

    def fixed_inputs(self):
        skills = {path.relative_to(self.skills_root).as_posix(): digest(path.read_bytes())
                  for path in sorted(self.skills_root.rglob('*')) if path.is_file()}
        return {
            'case_id': self.case_id, 'family': self.family,
            'source_revision': self.source_revision, 'seed': self.seed,
            'profile_hash': digest(self.profile.read_bytes()),
            'spec_hash': digest(self.spec.read_bytes()),
            'protocol_hash': digest(self.public_protocol.read_bytes()),
            'skills_hash': digest(skills),
            'initial_experience_hash': digest(self.initial_experience.read_bytes()),
            'model': self.model, 'vision_model': self.vision_model,
            'effort': self.effort, 'thinking': self.thinking,
            'tool_mode': self.tool_mode, 'tools_hash': self.tools_hash,
            'recovery_rules_hash': self.recovery_rules_hash,
            'environment_digest': digest({
                'profile_hash': digest(self.profile.read_bytes()),
                'spec_hash': digest(self.spec.read_bytes()),
                'tools_hash': self.tools_hash, 'skills_hash': digest(skills),
                'model': self.model, 'vision_model': self.vision_model, 'effort': self.effort,
                'thinking': self.thinking, 'tool_mode': self.tool_mode,
            }),
            'mutation_hash': digest([self.mutation_file, self.mutation_before, self.mutation_after]),
            'allowed_files': list(self.allowed_files), 'held_out': self.held_out,
        }
