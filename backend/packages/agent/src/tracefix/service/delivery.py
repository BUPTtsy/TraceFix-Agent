"""SDK Job 通过最终累计验收后的交付记录、人工审批与交付提交。"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from tracefix.config import Project
from tracefix.execution.harness_revision import (RevisionReceipt, cumulative_diff,
    tracked_files, validate_receipt)
from tracefix.execution.repository import canonical_remote, linked, plain_root, run_git
from tracefix.repository_registry import RepositoryError, registry_lock
from tracefix.runtime.contracts import digest, new_id
from tracefix.service.publication import publish_files


_IDENTITY = re.compile(r'[A-Za-z0-9_-]{1,80}')
_APPROVAL_ID = re.compile(r'dlv_[a-f0-9]{32}')
_APPROVAL_FIELDS = ('job_id', 'accepted_commit', 'patch_hash', 'publish_branch', 'diff_ref', 'requested_at')


class DeliveryError(RuntimeError):
    def __init__(self, code: str, message: str = ''):
        self.code = code
        super().__init__(message or code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def managed_config(config):
    return isinstance(config, dict) and type(config.get('version')) is int and config['version'] == 1


def _record_path(data_root, run_id):
    if not isinstance(run_id, str) or not _IDENTITY.fullmatch(run_id):
        raise DeliveryError('DELIVERY_INVALID', '交付 Run 标识无效')
    # plain_root 拒绝目录链中的链接，记录文件和临时文件另行检查。
    return plain_root(Path(data_root) / 'deliveries' / run_id) / 'record.json'


def _read(path):
    if linked(path) or linked(path.with_suffix('.pending')):
        raise PermissionError('交付记录不能经过链接')
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise DeliveryError('DELIVERY_RECORD_INVALID', '交付记录无法读取') from error
    if not isinstance(record, dict) or not isinstance(record.get('approval_id'), str):
        raise DeliveryError('DELIVERY_RECORD_INVALID', '交付记录格式无效')
    return record


def _write(path, record):
    temporary = path.with_suffix('.pending')
    if linked(path) or linked(temporary):
        raise PermissionError('交付记录不能经过链接')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stack = ExitStack()
    try:
        stack.enter_context(registry_lock(path))
    except RepositoryError:
        raise DeliveryError('DELIVERY_BUSY', '交付记录正在被其他请求处理') from None
    return stack


def delivery_record(data_root: Path, run_id: str) -> dict | None:
    return _read(_record_path(data_root, run_id))


def approval_view(record):
    """服务输出与控制面共用的审批子集。"""
    view = {'id': record['approval_id'], 'status': record['status'],
            **{key: record[key] for key in _APPROVAL_FIELDS}}
    if record['status'] != 'pending':
        view.update(decision=record['decision'], decided_by=record['decided_by'],
                    decided_at=record['decided_at'])
    return view


def delivery_fields(record, artifacts):
    """按交付记录塑形服务结果：待审批为非终态，已决定则携带交付结果。"""
    if record['status'] == 'pending':
        diff = artifacts.read(record['scope_id'], record['run_id'], record['diff_ref']).decode('utf-8')
        return {'run_status': 'WAITING_APPROVAL', 'terminal': False, 'outcome': 'FIX_VERIFIED',
                'coding_backend': 'claude_sdk', 'control_status': 'COMPLETED', 'can_resume': False,
                'publication': None, 'branch': None, 'commit': None,
                'approval': approval_view(record), 'delivery_diff': diff}
    publication = record['publication'] or {}
    return {'run_status': 'COMPLETED', 'terminal': True, 'publication': record['publication'],
            'branch': publication.get('branch'), 'commit': publication.get('commit'),
            'approval': approval_view(record) if record['require_approval'] else None}


def _require_identity(record, *, scope_id, job_id, run_id):
    if (record.get('scope_id'), record.get('job_id'), record.get('run_id')) != (scope_id, job_id, run_id):
        raise DeliveryError('APPROVAL_MISMATCH', '交付记录不属于当前 Job/Run')


def frozen_project(config):
    """与注册表装载一致的项目对象，其摘要即 RevisionReceipt.registered_config_hash。"""
    return Project(id=config['repo_id'], repo_id=config['owner'] + '/' + config['repo'],
        root=Path(config['root']), allowed_files=config['allowed_files'], repository_config=config)


def _bind(authority, artifacts, data_root, job, run_id, config):
    if (config.get('frozen_run_id') != run_id or config.get('repo_id') != job.scope_id
            or job.origin_base_commit != config['commit']
            or canonical_remote(job.repo_id) != canonical_remote(config['owner'] + '/' + config['repo'])):
        raise PermissionError('交付 Job 与冻结仓库配置不一致')
    if not any(session.kind == 'initializer' and session.run_id == run_id
               for session in authority.list_sessions(job.job_id, scope_id=job.scope_id)):
        raise PermissionError('交付 Job 不属于当前 Run')
    if not _IDENTITY.fullmatch(job.job_id):
        raise PermissionError('Harness Job 路径无效')
    path = plain_root(Path(data_root) / 'harness' / job.job_id) / 'host-state.json'
    if linked(path):
        raise PermissionError('Harness 宿主记录不能经过链接')
    host = json.loads(path.read_text(encoding='utf-8'))
    lineage = host['lineage']
    if host.get('origin_run_id') != run_id or not lineage or host.get('pending_accept'):
        raise PermissionError('宿主记录缺少完整的已接纳 lineage')
    entry = lineage[-1]
    receipt = RevisionReceipt.model_validate(artifacts.json(job.scope_id, entry['run_id'], entry['ref']))
    previous = (RevisionReceipt.model_validate(artifacts.json(job.scope_id, lineage[-2]['run_id'],
        lineage[-2]['ref'])) if len(lineage) > 1 else None)
    if entry['commit'] != job.accepted_commit or receipt.candidate_commit != job.accepted_commit:
        raise PermissionError('宿主 lineage 末项不是 Job 已接纳提交')
    # 回执中的注册配置摘要把这份冻结配置绑定到 Harness 实际使用的项目。
    validate_receipt(receipt, job=job, project=frozen_project(config), previous=previous)
    origin = host['origin_snapshot']
    origin_files = {path: bytes.fromhex(value) for path, value in host['origin_files'].items()}
    if (origin.get('commit') != job.origin_base_commit or origin.get('subdir') not in {'', '.'}
            or {path: digest(content) for path, content in origin_files.items()} != origin.get('files')):
        raise PermissionError('宿主原始内容与冻结源码快照不一致')
    candidate = plain_root(Path(receipt.candidate_repository))
    diff = cumulative_diff(origin_files, candidate, job.accepted_commit)
    if digest(diff.encode()) != job.final_patch_hash:
        raise DeliveryError('PATCH_HASH_MISMATCH', '累计差异与最终累计验收的补丁摘要不一致')
    # 改动集合 = 已接纳提交中相对原始内容变化或新增的文件；删除已由 cumulative_diff 拒绝。
    changes = {}
    for relative in tracked_files(candidate, job.accepted_commit):
        content = run_git(candidate, 'cat-file', 'blob', job.accepted_commit + ':' + relative)
        if origin_files.get(relative) != content:
            changes[relative] = content
    if not changes:
        raise ValueError('交付改动为空')
    return diff, changes


def _content(authority, artifacts, data_root, scope_id, job_id, run_id, config):
    """从 Harness 权威状态与宿主记录重建交付内容，并绑定最终累计验收。"""
    try:
        job = authority.get_job(job_id, scope_id=scope_id)
    except Exception as error:
        raise DeliveryError('DELIVERY_UNAVAILABLE', 'SDK Job 不存在或不属于当前作用域') from error
    if (job.status != 'COMPLETED' or job.quality != 'MERGE_READY'
            or not job.final_verification_ref or not job.final_patch_hash):
        raise DeliveryError('DELIVERY_UNAVAILABLE', 'SDK Job 尚未通过最终累计验收')
    try:
        diff, changes = _bind(authority, artifacts, data_root, job, run_id, config)
    except DeliveryError:
        raise
    except Exception as error:
        raise DeliveryError('DELIVERY_INVALID', '交付内容与最终累计验收的绑定无效') from error
    return job, diff, changes


def open_delivery(*, authority, artifacts, data_root: Path, scope_id: str, job_id: str, run_id: str,
                  repository_config: dict | None, resolve_credential) -> dict | None:
    """Job COMPLETED 后登记交付；需审批时只登记 pending，否则立即交付。记录已存在时幂等返回。"""
    if not managed_config(repository_config):
        return None
    path = _record_path(data_root, run_id)
    with _lock(path):
        record = _read(path)
        if record is not None:
            _require_identity(record, scope_id=scope_id, job_id=job_id, run_id=run_id)
            return record
        job, diff, changes = _content(authority, artifacts, data_root, scope_id, job_id, run_id,
                                      repository_config)
        record = {'approval_id': new_id('dlv'), 'status': 'pending', 'job_id': job_id, 'run_id': run_id,
            'scope_id': scope_id, 'accepted_commit': job.accepted_commit,
            'origin_base_commit': job.origin_base_commit, 'patch_hash': job.final_patch_hash,
            'diff_ref': artifacts.put(scope_id, run_id, diff.encode('utf-8'), 'diff', label='交付累计差异'),
            'publish_branch': repository_config.get('publish_branch', False) is True,
            'require_approval': repository_config.get('require_approval', False) is True,
            'requested_at': _now(), 'decision': None, 'decided_by': None, 'decided_at': None,
            'publication': None}
        if not record['require_approval']:
            publication = publish_files(repository_config, data_root, run_id=run_id, files=changes,
                                        resolve_credential=resolve_credential)
            record.update(status='approved', decision='approve', decided_by='auto',
                          decided_at=_now(), publication=publication)
        _write(path, record)
        return record


def decide_delivery(*, authority, artifacts, data_root: Path, scope_id: str, job_id: str, run_id: str,
                    repository_config: dict | None, approval_id: str, decision: str, decided_by: str,
                    resolve_credential) -> dict:
    """仅对匹配的 pending 记录生效；重复相同决定返回已有记录，冲突决定拒绝。"""
    if (decision not in {'approve', 'reject'} or not isinstance(approval_id, str)
            or not _APPROVAL_ID.fullmatch(approval_id) or not isinstance(decided_by, str)
            or not decided_by.strip() or len(decided_by) > 120):
        raise DeliveryError('APPROVAL_INVALID', '审批决定字段无效')
    if not managed_config(repository_config):
        raise DeliveryError('DELIVERY_UNAVAILABLE', '仓库配置不支持交付')
    path = _record_path(data_root, run_id)
    if not path.parent.is_dir():
        raise DeliveryError('APPROVAL_NOT_PENDING', '当前 Run 没有待审批交付')
    with _lock(path):
        record = _read(path)
        if record is None:
            raise DeliveryError('APPROVAL_NOT_PENDING', '当前 Run 没有待审批交付')
        _require_identity(record, scope_id=scope_id, job_id=job_id, run_id=run_id)
        if record['approval_id'] != approval_id:
            raise DeliveryError('APPROVAL_MISMATCH', '审批标识与待审批交付不一致')
        if record['status'] != 'pending':
            if record['decision'] == decision:
                return record
            raise DeliveryError('APPROVAL_DECIDED', '交付已有不同的审批决定')
        if (record['publish_branch'] is not (repository_config.get('publish_branch', False) is True)
                or record['require_approval'] is not (repository_config.get('require_approval', False) is True)):
            raise DeliveryError('APPROVAL_MISMATCH', '冻结仓库配置与待审批交付不一致')
        if decision == 'reject':
            publication = {'status': 'rejected', 'branch': None, 'commit': None, 'error': None}
        else:
            job, diff, changes = _content(authority, artifacts, data_root, scope_id, job_id, run_id,
                                          repository_config)
            if ((job.accepted_commit, job.origin_base_commit, job.final_patch_hash)
                    != (record['accepted_commit'], record['origin_base_commit'], record['patch_hash'])
                    or artifacts.read(scope_id, run_id, record['diff_ref']) != diff.encode('utf-8')):
                raise DeliveryError('APPROVAL_MISMATCH', '待审批内容与当前已验收内容不一致')
            publication = publish_files(repository_config, data_root, run_id=run_id, files=changes,
                                        resolve_credential=resolve_credential)
        record.update(status='approved' if decision == 'approve' else 'rejected', decision=decision,
                      decided_by=decided_by, decided_at=_now(), publication=publication)
        _write(path, record)
        return record
