"""阶段化 Skill 的小索引、正文快照和引用快照。

Skill 只是模型可读的指导。工具权限、路径范围和验证门仍由运行时决定。
"""
from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
from typing import Any

from tracefix.knowledge.context import SkillCatalog
from tracefix.storage.artifacts import redact


class SkillStore:
    """将实际使用的 Skill 正文和引用绑定到 Run，避免恢复时热更新漂移。"""

    SNAPSHOT_SCHEMA = 1
    SNAPSHOT_LABEL = 'Skill内容快照'

    def __init__(self, artifacts=None):
        self.artifacts = artifacts

    @staticmethod
    def _freeze_bytes(raw: bytes) -> dict[str, Any]:
        text = raw.decode('utf-8')
        safe = '\r'.join(redact(part) for part in text.split('\r')).encode('utf-8')
        return {'content_b64': base64.b64encode(safe).decode('ascii'),
                'source_hash': sha256(raw).hexdigest(),
                'frozen_source_hash': sha256(safe).hexdigest(),
                'source_redacted': safe != raw}

    @staticmethod
    def _identity(snapshot: dict[str, Any], phase: str) -> dict[str, str]:
        return {
            'name': snapshot['name'],
            'version': snapshot['version'],
            'content_hash': snapshot['content_hash'],
            'phase': SkillCatalog.phase_name(phase),
        }

    @staticmethod
    def _find_loaded(state, name: str) -> dict[str, Any] | None:
        for loaded in reversed(getattr(state, 'skills_loaded', []) or []):
            if loaded.get('name') == name and loaded.get('snapshot_ref'):
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
        source_bytes = base64.b64decode(snapshot['content_b64'], validate=True)
        if sha256(source_bytes).hexdigest() != snapshot.get('frozen_source_hash'):
            raise ValueError('Skill 源字节快照校验失败')
        for reference in snapshot.get('references', []):
            raw = reference.get('content', '').encode('utf-8')
            if sha256(raw).hexdigest() != reference.get('content_hash'):
                raise ValueError('Skill reference 快照校验失败')
            source_bytes = base64.b64decode(reference['content_b64'], validate=True)
            if sha256(source_bytes).hexdigest() != reference.get('frozen_source_hash'):
                raise ValueError('Skill reference 源字节快照校验失败')
        return snapshot

    def _read_snapshot(self, state, ref: str) -> dict[str, Any]:
        if self.artifacts is None:
            raise FileNotFoundError('Skill 快照存储未配置')
        snapshot = self._check_snapshot(self.artifacts.json(state.scope_id, state.run_id, ref))
        if snapshot.get('scope_id') != state.scope_id or snapshot.get('run_id') != state.run_id:
            raise PermissionError('Skill 快照不属于当前 Run')
        return snapshot

    def _write_snapshot(self, state, snapshot: dict[str, Any]) -> str | None:
        if self.artifacts is None:
            return None
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
            'references': [{key: value for key, value in reference.items() if key != 'content_b64'}
                           for reference in snapshot.get('references', [])],
            **({'snapshot_ref': snapshot_ref} if snapshot_ref else {}),
        }

    def load(self, state, catalog: SkillCatalog, name: str, phase: str,
             *, references: list[str] | None = None) -> dict[str, Any]:
        phase = SkillCatalog.phase_name(phase)
        loaded = self._find_loaded(state, name)
        if loaded and loaded.get('snapshot_ref'):
            snapshot = self._read_snapshot(state, loaded['snapshot_ref'])
            if snapshot['name'] != name or phase not in snapshot['metadata']['phases']:
                raise PermissionError('Skill 快照与当前阶段不匹配')
            if any(snapshot.get(key) != loaded.get(key) for key in ('version', 'content_hash')):
                raise ValueError('Skill 快照与已加载身份不匹配')
            return self._result(snapshot, loaded['snapshot_ref'])

        entry, content = catalog.load_document(name, phase)
        source_bytes = entry['_path'].read_bytes()
        if source_bytes.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n') != content:
            raise OSError('Skill 正文在加载时更新，请重新读取')
        actual_references = catalog.load_references(name, phase, references, entry=entry)
        for reference in actual_references:
            raw = base64.b64decode(reference['content_b64'], validate=True)
            reference.update(self._freeze_bytes(raw))
            reference['content'] = base64.b64decode(reference['content_b64']).decode('utf-8')
            reference['content_hash'] = sha256(reference['content'].encode('utf-8')).hexdigest()
            reference['content'] = redact(reference['content'])
            reference['content_hash'] = sha256(reference['content'].encode('utf-8')).hexdigest()
            reference['byte_count'] = len(reference['content'].encode('utf-8'))
        used_content = redact(content)
        metadata = deepcopy(entry)
        metadata.pop('_path', None)
        snapshot = {
            'schema_version': self.SNAPSHOT_SCHEMA,
            'scope_id': state.scope_id,
            'run_id': state.run_id,
            'name': entry['name'],
            'version': entry['version'],
            'phase': phase,
            'source': str(entry['_path']),
            'description': entry['description'],
            **self._freeze_bytes(source_bytes),
            'content_hash': sha256(used_content.encode('utf-8')).hexdigest(),
            'byte_count': len(used_content.encode('utf-8')),
            'content': used_content,
            'references': actual_references,
            'metadata': redact(metadata),
        }
        snapshot = redact(snapshot)
        snapshot_ref = self._write_snapshot(state, snapshot)
        identity = self._identity(snapshot, phase)
        if snapshot_ref:
            identity['snapshot_ref'] = snapshot_ref
        state.skills_loaded.append(identity)
        return self._result(snapshot, snapshot_ref)

    def select(self, catalog: SkillCatalog, phase: str, context: dict[str, Any] | None = None,
               *, explicit: list[str] | None = None, limit: int = 6,
               entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """以触发器具体度排序并限制正文候选；索引仍只包含摘要。"""
        phase = SkillCatalog.phase_name(phase)
        entries = catalog.details() if entries is None else entries
        applicable = [entry for entry in entries if catalog.matches(entry, phase, context or {})]
        explicit = list(dict.fromkeys(explicit or []))
        by_name = {entry['name']: entry for entry in entries if phase in entry['phases']}
        selected = [by_name[name] for name in explicit if name in by_name]
        remaining = [entry for entry in applicable if entry['name'] not in explicit]
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
        unavailable = []
        try:
            effective = {entry['name']: entry for entry in catalog.details()}
        except (OSError, ValueError) as error:
            effective = {}
            unavailable.append({'name': 'catalog', 'reason': str(error)})
        for loaded in getattr(state, 'skills_loaded', []) or []:
            if not loaded.get('snapshot_ref'):
                continue
            try:
                snapshot = self._read_snapshot(state, loaded['snapshot_ref'])
                effective[snapshot['name']] = snapshot['metadata']
            except (OSError, PermissionError, ValueError) as error:
                effective.pop(loaded['name'], None)
                unavailable.append({'name': loaded['name'], 'reason': str(error)})
        entries = self.select(catalog, phase, selection_context, explicit=explicit, limit=limit,
                              entries=list(effective.values()))
        for name in explicit or []:
            if name not in effective or SkillCatalog.phase_name(phase) not in effective[name]['phases']:
                unavailable.append({'name': name, 'reason': 'Skill 缺失或不适用于当前阶段'})
        skills = []
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
