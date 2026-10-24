"""冻结评测用例的固定输入；由 evals.freeze 写入与校验。"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePath, PureWindowsPath

from tracefix.runtime.contracts import digest


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
    held_out: bool = True
    mutation_file: str | None = None
    mutation_before: str | None = None
    mutation_after: str | None = None
    timeout_seconds: int = 1800
    command: tuple[str, ...] = ()
    track: str = 'compare'
    category: str | None = None
    known_bug: bool | None = None
    mutations: tuple[dict, ...] = ()

    def __post_init__(self):
        if self.track not in {'compare', 'capability'}:
            raise ValueError('track 必须为 compare 或 capability')
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', self.source_revision):
            raise ValueError('source_revision 必须是完整提交 SHA')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', self.project):
            raise ValueError('project 标识无效')
        if not all((self.case_id, self.family, self.goal, self.model,
                    self.tools_hash, self.recovery_rules_hash)):
            raise ValueError('固定输入字段不能为空')
        if type(self.seed) is not int or self.timeout_seconds <= 0:
            raise ValueError('seed 或 timeout 无效')
        if self.thinking not in {'enabled', 'disabled'}:
            raise ValueError('thinking 无效')
        mutation = (self.mutation_file, self.mutation_before, self.mutation_after)
        if any(value is not None for value in mutation) and not all(value is not None for value in mutation):
            raise ValueError('缺陷注入必须提供 file/before/after')
        if self.mutation_file and (Path(self.mutation_file).is_absolute()
                                   or '..' in Path(self.mutation_file).parts):
            raise ValueError('缺陷注入路径必须位于源码内')
        if self.mutations and any(value is not None for value in mutation):
            raise ValueError('不可同时提供旧单处与 mutations 注入')
        for item in self.case_mutations():
            if (set(item) != {'file', 'before', 'after'}
                    or any(not isinstance(item[field], str) for field in item)
                    or not item['before'] or item['before'] == item['after']):
                raise ValueError('mutation 必须包含 file/before/after 且确实改变源码')
            path = item['file'].replace('\\', '/')
            if (not path or path.startswith('/') or Path(path).is_absolute() or PureWindowsPath(path).drive
                    or '..' in Path(path).parts or '.git' in Path(path).parts):
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
        if 'mutations' in values:
            values['mutations'] = tuple(values['mutations'])
        return cls(**values)

    def payload(self):
        return {key: str(value) if isinstance(value, PurePath) else value
                for key, value in asdict(self).items()}

    def case_mutations(self):
        if self.mutations:
            return tuple(dict(item) for item in self.mutations)
        if self.mutation_file is not None:
            return ({'file': self.mutation_file, 'before': self.mutation_before,
                     'after': self.mutation_after},)
        return ()

    def mutation_hash(self):
        mutations = self.case_mutations()
        if len(mutations) <= 1:
            return digest([mutations[0][name] for name in ('file', 'before', 'after')]
                          if mutations else [None, None, None])
        return digest(list(mutations))

    def agent_payload(self):
        private = {'mutation_file', 'mutation_before', 'mutation_after', 'mutations', 'source_root',
                   'source_revision', 'case_id', 'family', 'category', 'known_bug', 'track'}
        return {key: value for key, value in self.payload().items() if key not in private}

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
            'tools_hash': self.tools_hash,
            'recovery_rules_hash': self.recovery_rules_hash,
            'environment_digest': digest({
                'profile_hash': digest(self.profile.read_bytes()),
                'spec_hash': digest(self.spec.read_bytes()),
                'tools_hash': self.tools_hash, 'skills_hash': digest(skills),
                'model': self.model, 'vision_model': self.vision_model, 'effort': self.effort,
                'thinking': self.thinking,
            }),
            'mutation_hash': self.mutation_hash(),
            'allowed_files': list(self.allowed_files), 'held_out': self.held_out,
            'track': self.track, 'category': self.category or self.family, 'known_bug': self.known_bug,
        }
