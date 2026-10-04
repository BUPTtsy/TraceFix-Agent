"""阶段化 Skill 的小索引、正文快照和引用快照。

Skill 只是模型可读的指导。工具权限、路径范围和验证门仍由运行时决定。
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any

from tracefix.knowledge.context import SkillCatalog


class SkillStore:
    """将实际使用的 Skill 正文和引用绑定到 Run，避免恢复时热更新漂移。"""

    SNAPSHOT_SCHEMA = 1
    SNAPSHOT_LABEL = 'Skill内容快照'

    def __init__(self, artifacts=None):
        self.artifacts = artifacts

    @staticmethod
    def _identity(entry: dict[str, Any], phase: str) -> dict[str, str]:
        return {
            'name': entry['name'],
            'version': entry['version'],
            'content_hash': entry['content_hash'],
            'phase': phase.upper(),
        }

    @staticmethod
    def _find_loaded(state, name: str, phase: str) -> dict[str, Any] | None:
        phase = phase.upper()
        for loaded in reversed(getattr(state, 'skills_loaded', []) or []):
            if loaded.get('name') == name and str(loaded.get('phase', phase)).upper() == phase:
                return loaded
        return None

    @staticmethod
    def _check_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get('schema_version') != SkillStore.SNAPSHOT_SCHEMA:
            raise ValueError('Skill 快照版本不受支持')
        content = snapshot.get('content')
        if not isinstance(content, str):
            raise ValueError('Skill 快照缺少正文')
        if sha256(content.encode('utf-8')).hexdigest() != snapshot.get('content_hash'):
            raise ValueError('Skill 正文快照校验失败')
        for reference in snapshot.get('references', []):
            raw = reference.get('content', '').encode('utf-8')
            if sha256(raw).hexdigest() != reference.get('content_hash'):
                raise ValueError('Skill reference 快照校验失败')
        return snapshot

    def _read_snapshot(self, state, ref: str) -> dict[str, Any]:
        if self.artifacts is None:
            raise FileNotFoundError('Skill 快照存储未配置')
        return self._check_snapshot(self.artifacts.json(state.scope_id, state.run_id, ref))

    def _write_snapshot(self, state, entry: dict[str, Any], content: str,
                        references: list[dict[str, Any]], phase: str) -> str | None:
        if self.artifacts is None:
            return None
        snapshot = {
            'schema_version': self.SNAPSHOT_SCHEMA,
            'name': entry['name'],
            'version': entry['version'],
            'phase': phase.upper(),
            'source': str(entry['_path']),
            'content_hash': sha256(content.encode('utf-8')).hexdigest(),
            'content': content,
            'references': deepcopy(references),
        }
        return self.artifacts.put(state.scope_id, state.run_id, snapshot,
                                  label=self.SNAPSHOT_LABEL)

    @staticmethod
    def _result(snapshot: dict[str, Any], snapshot_ref: str | None) -> dict[str, Any]:
        return {
            'name': snapshot['name'],
            'version': snapshot['version'],
            'content_hash': snapshot['content_hash'],
            'description': snapshot.get('description', ''),
            'source': snapshot.get('source', ''),
            'content': snapshot['content'],
            'references': deepcopy(snapshot.get('references', [])),
            **({'snapshot_ref': snapshot_ref} if snapshot_ref else {}),
        }

    def load(self, state, catalog: SkillCatalog, name: str, phase: str,
             *, references: list[str] | None = None) -> dict[str, Any]:
        phase = phase.upper()
        loaded = self._find_loaded(state, name, phase)
        if loaded and loaded.get('snapshot_ref'):
            snapshot = self._read_snapshot(state, loaded['snapshot_ref'])
            if snapshot['name'] != name or snapshot['phase'] != phase:
                raise ValueError('Skill 快照与当前阶段不匹配')
            return self._result(snapshot, loaded['snapshot_ref'])

        entry, content = catalog.load_document(name, phase)
        actual_references = catalog.load_references(name, phase, references)
        snapshot_ref = self._write_snapshot(state, entry, content, actual_references, phase)
        snapshot = {
            'name': entry['name'],
            'version': entry['version'],
            'phase': phase,
            'source': str(entry['_path']),
            'description': entry['description'],
            'content_hash': entry['content_hash'],
            'content': content,
            'references': actual_references,
        }
        identity = self._identity(entry, phase)
        if snapshot_ref:
            identity['snapshot_ref'] = snapshot_ref
        if not any(item.get('name') == name and str(item.get('phase', phase)).upper() == phase
                   for item in getattr(state, 'skills_loaded', []) or []):
            state.skills_loaded.append(identity)
        return self._result(snapshot, snapshot_ref)

    def select(self, catalog: SkillCatalog, phase: str, context: dict[str, Any] | None = None,
               *, explicit: list[str] | None = None, limit: int = 6) -> list[dict[str, Any]]:
        """以触发器具体度排序并限制正文候选；索引仍只包含摘要。"""
        phase = phase.upper()
        entries = catalog.select(phase, context or {})
        explicit = list(dict.fromkeys(explicit or []))
        by_name = {entry['name']: entry for entry in entries}
        selected = [by_name[name] for name in explicit if name in by_name]
        remaining = [entry for entry in entries if entry['name'] not in explicit]
        remaining.sort(key=lambda entry: (
            -sum(bool(values) for values in entry.get('triggers', {}).values()),
            -sum(len(values) for values in entry.get('triggers', {}).values()),
            entry['name'],
        ))
        for entry in remaining:
            if len(selected) >= max(0, limit):
                break
            selected.append(entry)
        return selected[:max(0, limit)]

    def context(self, state, catalog: SkillCatalog, phase: str,
                selection_context: dict[str, Any] | None = None, *, explicit: list[str] | None = None,
                limit: int = 6) -> dict[str, Any]:
        """生成少量索引和正文；单个可选 Skill 不可用时返回普通工具降级信息。"""
        entries = self.select(catalog, phase, selection_context, explicit=explicit, limit=limit)
        skills, unavailable = [], []
        for entry in entries:
            try:
                skills.append(self.load(state, catalog, entry['name'], phase))
            except (FileNotFoundError, OSError, PermissionError, ValueError) as error:
                unavailable.append({'name': entry['name'], 'reason': str(error)})
        return {
            'skill_index': [{'name': entry['name'], 'description': entry['description']} for entry in entries],
            'skills': skills,
            'skill_unavailable': unavailable,
        }
