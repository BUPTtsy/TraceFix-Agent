"""经验适用性和公开开发验证的窄公共接缝。"""
from __future__ import annotations

import json
import os

from tracefix.runtime.contracts import Validation, digest
from tracefix.runtime.verification import verify_artifacts


def cross_run_enabled(value=None):
    configured = os.getenv('TRACEFIX_CROSS_RUN_MEMORY', 'on').lower() not in {'0', 'false', 'off'}
    return configured and value is not False


def public_content(value):
    if isinstance(value, dict):
        if (value.get('final_scoring_only') or value.get('held_out') or value.get('heldout')
                or value.get('learnable') is False):
            return False
        for key in ('source', 'source_type', 'split', 'evaluation_split', 'purpose'):
            if str(value.get(key, '')).lower() in {
                    'held-out', 'held_out', 'heldout', 'final_oracle', 'final_scoring',
                    'oracle', 'final_scoring_only'}:
                return False
        return all(public_content(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(public_content(item) for item in value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return True
        return True if isinstance(decoded, str) else public_content(decoded)
    return True


def conditions(record):
    value = record.get('applicability') or {}
    return json.loads(value) if isinstance(value, str) else value


def applicable(record, state=None, phase=None):
    declared = conditions(record)
    phases = declared.get('phases') or []
    phase = str(phase or getattr(state, 'phase', '')).upper()
    if phases and phase not in {str(value).upper() for value in phases}:
        return False
    for field in ('environment_digest', 'test_spec_hash'):
        expected = declared.get(field)
        if expected and (state is None or getattr(state, field, None) != expected):
            return False
    return True


def source_applicability(record, source_manifest, state=None, *, artifact_exists=None, artifact_read=None):
    if record.get('source_revision') == source_manifest:
        return 'exact'
    compatibility = conditions(record).get('compatibility') or {}
    probe = compatibility.get(source_manifest) or {}
    if not probe.get('artifact_ref') or not probe.get('artifact_hash') or state is None:
        return 'stale'
    if all(probe.get(field) == getattr(state, field, None)
           for field in ('source_manifest', 'environment_digest', 'test_spec_hash')):
        if not callable(artifact_exists) or not callable(artifact_read):
            return 'stale'
        try:
            value = artifact_read(probe['artifact_ref']) if artifact_exists(probe['artifact_ref']) is True else None
            if (not isinstance(value, dict) or digest(value) != probe['artifact_hash'] or not public_content(value)
                    or value.get('revoked') or value.get('status') in {'revoked', 'superseded'}):
                return 'stale'
            current = checked_probe(state, probe['artifact_ref'], artifact_exists, artifact_read)
            return 'compatible' if current['artifact_hash'] == probe['artifact_hash'] else 'stale'
        except (OSError, ValueError, KeyError, PermissionError):
            return 'stale'
    return 'stale'


def checked_probe(state, probe_ref, artifact_exists, artifact_read):
    if artifact_exists(probe_ref) is not True:
        raise PermissionError('兼容探针原件缺失')
    probe = artifact_read(probe_ref)
    if (not isinstance(probe, dict) or not public_content(probe)
            or probe.get('type') != 'memory_compatibility_probe' or probe.get('passed') is not True):
        raise PermissionError('兼容性必须由当前公开探针支持')
    for field in ('scope_id', 'run_id', 'source_manifest', 'patch_hash',
                  'environment_digest', 'test_spec_hash'):
        if probe.get(field) != getattr(state, field, None):
            raise PermissionError('兼容探针绑定与当前 Run 不一致')
    refs = probe.get('evidence_refs') or []
    if not refs:
        raise PermissionError('兼容探针必须包含实际检查引用')
    for ref in refs:
        if artifact_exists(ref) is not True or not public_content(artifact_read(ref)):
            raise PermissionError('兼容探针证据缺失或不公开')
    return {'artifact_ref': probe_ref, 'artifact_hash': digest(probe),
            **{field: getattr(state, field, None) for field in (
                'source_manifest', 'environment_digest', 'test_spec_hash')}}


def verified_public_evidence(state, artifact_exists, artifact_read, artifact_read_bytes):
    refs = list(getattr(state, 'validation_refs', []) or [])
    if not refs or not all(callable(value) for value in (artifact_exists, artifact_read, artifact_read_bytes)):
        raise PermissionError('晋升需要可读取的公开开发验证原件')

    def public_read(ref):
        value = artifact_read(ref)
        if not public_content(value):
            raise PermissionError('最终 Oracle 或 held-out 证据不能用于经验晋升')
        return value

    validations = [Validation.model_validate(public_read(ref)) for ref in refs]
    if not verify_artifacts(state, validations, artifact_exists, public_read, artifact_read_bytes):
        raise PermissionError('公开开发验证未通过或与当前 patch/env/spec 不匹配')
    return refs


def experience_record(state, content, kind, revision, *, expires_at=None):
    if not public_content(content):
        raise PermissionError('最终 Oracle 或 held-out 内容不能写入经验')
    summary = content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            pass
    if isinstance(content, dict):
        summary = {key: content[key] for key in (
            'summary', 'result_summary', 'root_cause', 'failure_signature', 'failure_class',
            'negative_conditions', 'unknown', 'evidence_refs', 'failed_patch_hashes')
            if key in content}
        if not summary:
            summary = {'summary': str(content.get('goal') or getattr(state, 'goal', '')),
                       'unknown': '尚未提炼根因；原件通过来源引用回查'}
    refs = list(dict.fromkeys(getattr(state, 'evidence_refs', []) or []))
    summary = {'lesson': summary, 'source': 'public_development', 'kind': kind,
               'evidence_refs': refs, 'source_run_id': state.run_id,
               'source_manifest': state.source_manifest}
    key = digest([state.scope_id, state.source_manifest, kind, summary])
    return {'id': key, 'scope_id': state.scope_id, 'layer': 'M3', 'kind': kind,
            'revision': revision, 'source_revision': state.source_manifest,
            'source_run_id': state.run_id, 'content': summary, 'status': 'candidate',
            'patch_hash': getattr(state, 'patch_hash', None),
            'environment_digest': getattr(state, 'environment_digest', ''),
            'test_spec_hash': getattr(state, 'test_spec_hash', ''),
            'evidence_refs': refs, 'verification_refs': list(getattr(state, 'validation_refs', []) or []),
            'applicability': {'environment_digest': getattr(state, 'environment_digest', ''),
                              'test_spec_hash': getattr(state, 'test_spec_hash', '')},
            'expires_at': expires_at}
