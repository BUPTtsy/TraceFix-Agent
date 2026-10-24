"""SDK 源码副本的完整快照与冻结输入候选导出。"""

from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from tracefix.execution.platforms import is_link, is_windows, safe_relative
from tracefix.execution.repository import file_manifest, plain_root, repository_binding, run_git
from tracefix.execution.workspace import Workspace, git, is_frozen_path
from tracefix.runtime.contracts import FileEdit, PatchProposal, digest


class SDKCandidateRejected(PermissionError):
    def __init__(self, reason: str, path: str = ''):
        self.reason, self.path = reason, path
        super().__init__(f'{reason}: {path}' if path else reason)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    entries: dict[str, dict]
    content_hash: str

    def model_dump(self):
        return {'entries': self.entries, 'content_hash': self.content_hash}


def _snapshot(root: Path, *, skip=()) -> WorkspaceSnapshot:
    """完整快照 root；skip 为需整体跳过的顶层相对名（含子树），默认不跳过。"""
    if is_link(root) or not root.is_dir():
        raise SDKCandidateRejected('源码根目录不是普通目录')
    entries = {}
    skipped = frozenset(skip)

    def visit(directory: Path):
        with os.scandir(directory) as children:
            for child in sorted(children, key=lambda entry: entry.name):
                if skipped and directory == root and child.name in skipped:
                    continue
                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                metadata = path.lstat()
                kind = ('link' if is_link(path) else 'directory' if stat.S_ISDIR(metadata.st_mode)
                        else 'file' if stat.S_ISREG(metadata.st_mode) else 'special')
                entry = {'kind': kind, 'mode': stat.S_IMODE(metadata.st_mode),
                         'nlink': metadata.st_nlink, 'size': metadata.st_size if kind == 'file' else 0,
                         'digest': ''}
                if kind == 'file':
                    data = path.read_bytes()
                    after = path.lstat()
                    if (metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode,
                            metadata.st_nlink) != (after.st_ino, after.st_size, after.st_mtime_ns,
                                                  after.st_mode, after.st_nlink):
                        raise SDKCandidateRejected('扫描期间文件变化', relative)
                    entry['digest'] = digest(data)
                entries[relative] = entry
                if kind == 'directory':
                    visit(path)

    visit(root)
    return WorkspaceSnapshot(entries, digest(json.dumps(entries, sort_keys=True).encode()))


def _check_authority(authority: Workspace, frozen: dict):
    current = authority.require_repository_snapshot()
    identity = ('scope_id', 'repo_id', 'source_repository', 'source_tree', 'commit', 'files', 'subdir')
    if any(current.get(key) != frozen.get(key) for key in identity):
        raise SDKCandidateRejected('SDK 输入与当前 Run 的冻结源码身份不一致')


@dataclass
class SDKWorkspace:
    execution_dir: Path
    source: Path
    baseline: Path
    input_snapshot: WorkspaceSnapshot
    authority_snapshot: dict
    allowed_files: list[str]
    protected_paths: list[str]
    exclusions: tuple[str, ...] = ()
    config_dir: Path | None = None

    @property
    def root(self):
        return self.source

    @classmethod
    def create(cls, workspace: Workspace, execution_dir: Path, exclusions=()):
        snapshot = workspace.require_repository_snapshot()
        workspace.check_frozen(snapshot)
        destination = plain_root(Path(execution_dir))
        if destination.exists():
            raise FileExistsError('SDK execution 目录必须全新')
        if destination.is_relative_to(workspace.root):
            raise SDKCandidateRejected('SDK 副本必须位于权威源码之外')
        for pattern in exclusions:
            safe_relative(pattern)
            if any(fnmatchcase(relative, pattern) for relative in snapshot['files']):
                raise SDKCandidateRejected('排除规则不能隐藏冻结输入', pattern)
        source, baseline = destination / 'source', destination / 'input'
        source.mkdir(parents=True)
        baseline.mkdir()
        for name in ('sdk-config', 'transcript', 'control', 'state'):
            (destination / name).mkdir()
        for relative, expected in snapshot['files'].items():
            original = workspace.path(relative)
            metadata = original.lstat()
            if is_link(original) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise SDKCandidateRejected('输入包含链接或特殊文件', relative)
            data = original.read_bytes()
            if digest(data) != expected:
                raise SDKCandidateRejected('冻结输入内容已漂移', relative)
            for target in (source / relative, baseline / relative):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(stat.S_IMODE(metadata.st_mode))
        initial = _snapshot(source)
        metadata = {'input_snapshot': initial.model_dump(), 'authority_snapshot': snapshot,
                    'allowed_files': workspace.allowed_files, 'protected_paths': workspace.protected_paths,
                    'exclusions': list(exclusions)}
        (destination / 'input_manifest.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        return cls(destination, source, baseline, initial, deepcopy(snapshot),
                   list(workspace.allowed_files), list(workspace.protected_paths), tuple(exclusions))

    @classmethod
    def load(cls, execution_dir: Path, authority: Workspace):
        """从宿主持久化清单恢复副本；不信任 worker 可写入的 state。"""
        root = plain_root(Path(execution_dir))
        manifest_path = root / 'input_manifest.json'
        if not manifest_path.is_file() or is_link(manifest_path):
            raise SDKCandidateRejected('SDK 输入清单缺失或为链接')
        metadata = json.loads(manifest_path.read_text(encoding='utf-8'))
        snapshot = authority.require_repository_snapshot(metadata.get('authority_snapshot'))
        _check_authority(authority, snapshot)
        if metadata.get('allowed_files') != authority.allowed_files or metadata.get('protected_paths') != authority.protected_paths:
            raise SDKCandidateRejected('SDK 授权清单与当前权威工作区不一致')
        session_root = plain_root(Path(metadata.get('session_root', root)))
        if not root.is_relative_to(session_root):
            raise SDKCandidateRejected('SDK execution 不属于冻结 Session 目录')
        source = plain_root(Path(metadata.get('source_root', root / 'source')))
        baseline = plain_root(Path(metadata.get('baseline_root', root / 'input')))
        config_dir = plain_root(Path(metadata.get('config_dir', root / 'sdk-config')))
        if any(not path.is_relative_to(session_root) for path in (source, baseline, config_dir)):
            raise SDKCandidateRejected('SDK 持久化目录逃逸')
        if any(not p.is_dir() or is_link(p) for p in (source, baseline)):
            raise SDKCandidateRejected('SDK 源码副本目录缺失或为链接')
        loaded = cls(root, source, baseline, WorkspaceSnapshot(**metadata['input_snapshot']),
                     deepcopy(snapshot), list(metadata['allowed_files']), list(metadata['protected_paths']),
                     tuple(metadata.get('exclusions', ())), config_dir)
        authority.check_frozen(snapshot)
        loaded.check_input()
        return loaded

    def fork_execution(self, execution_id: str):
        safe_relative(execution_id)
        # 新 invocation 与首个 execution 平级（create 固定 <execution>/source 布局），目录深度不随反馈轮次增长；
        # 嵌套布局在 Windows MAX_PATH 下第二轮即失败。session_root 放宽为同一 Session 的 executions 目录。
        session_root = self.source.parent.parent
        target = session_root / execution_id
        if target.exists():
            raise SDKCandidateRejected('该 SDK execution 已存在', execution_id)
        target.mkdir(parents=True)
        for name in ('sdk-config', 'transcript', 'control', 'state'):
            (target / name).mkdir()
        metadata = {'input_snapshot': self.input_snapshot.model_dump(), 'authority_snapshot': self.authority_snapshot,
                    'allowed_files': self.allowed_files, 'protected_paths': self.protected_paths,
                    'exclusions': list(self.exclusions), 'session_root': str(session_root),
                    'source_root': str(self.source), 'baseline_root': str(self.baseline),
                    'config_dir': str(self.config_dir or self.execution_dir / 'sdk-config')}
        (target / 'input_manifest.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        return SDKWorkspace(target, self.source, self.baseline, self.input_snapshot,
                            deepcopy(self.authority_snapshot), self.allowed_files, self.protected_paths,
                            self.exclusions, self.config_dir or self.execution_dir / 'sdk-config')

    def freeze(self):
        snapshot = _snapshot(self.source)
        (self.execution_dir / 'output_manifest.json').write_text(
            json.dumps(snapshot.model_dump(), ensure_ascii=False), encoding='utf-8')
        return snapshot

    def check_input(self):
        baseline = _snapshot(self.baseline)
        if baseline != self.input_snapshot:
            raise SDKCandidateRejected('持久化冻结输入已损坏')

    def check_linux_boundary(self):
        start_path = self.execution_dir / 'linux_input_manifest.json'
        end_path = self.execution_dir / 'linux_output_manifest.json'
        if not start_path.exists() and not end_path.exists():
            return
        if not start_path.is_file() or not end_path.is_file():
            raise SDKCandidateRejected('Linux 执行边界清单不完整')
        start = json.loads(start_path.read_text(encoding='utf-8'))['entries']
        end = json.loads(end_path.read_text(encoding='utf-8'))['entries']
        for relative in sorted(start.keys() | end.keys()):
            original, current = start.get(relative), end.get(relative)
            if current is None:
                raise SDKCandidateRejected('Linux 副本存在删除', relative)
            if current['kind'] in {'link', 'special'} or (current['kind'] == 'file' and current['nlink'] != 1):
                raise SDKCandidateRejected('Linux 副本存在链接或特殊文件', relative)
            if original and any(original[key] != current[key] for key in ('kind', 'mode')):
                raise SDKCandidateRejected('Linux 副本存在类型或模式变化', relative)
            if not original and current['kind'] == 'file' and current['mode'] & 0o111 and not is_windows():
                raise SDKCandidateRejected('Linux 副本新增可执行文件', relative)


def export_candidate(sdk_workspace: SDKWorkspace, authority: Workspace, *, summary: str,
                     evidence_refs: list[str], rule_refs=(), forbidden_values=()) -> PatchProposal:
    # 凭据兜底：过短的值误报率高且无保护意义，直接忽略。
    secrets = tuple(value for value in forbidden_values if isinstance(value, str) and len(value) >= 8)
    sdk_workspace.check_input()
    sdk_workspace.check_linux_boundary()
    _check_authority(authority, sdk_workspace.authority_snapshot)
    if (authority.allowed_files != sdk_workspace.allowed_files
            or authority.protected_paths != sdk_workspace.protected_paths):
        raise SDKCandidateRejected('候选授权与冻结输入不一致')
    before, after = sdk_workspace.input_snapshot, sdk_workspace.freeze()
    edits = []
    for relative in sorted(before.entries.keys() | after.entries.keys()):
        old, current = before.entries.get(relative), after.entries.get(relative)
        if current is None:
            raise SDKCandidateRejected('不允许删除或重命名', relative)
        if current['kind'] in {'link', 'special'} or (current['kind'] == 'file' and current['nlink'] != 1):
            raise SDKCandidateRejected('不允许链接或特殊文件', relative)
        if old and any(old[key] != current[key] for key in ('kind', 'mode')):
            raise SDKCandidateRejected('不允许类型或模式变化', relative)
        if old == current:
            continue
        safe_relative(relative)
        if any(part.startswith('.') for part in relative.split('/')):
            raise SDKCandidateRejected('不允许隐藏路径变化', relative)
        if is_frozen_path(relative, authority.protected_paths):
            raise SDKCandidateRejected('不允许冻结测试或保护文件变化', relative)
        if current['kind'] == 'directory':
            if not any(path.startswith(relative + '/') and entry['kind'] == 'file'
                       for path, entry in after.entries.items()):
                raise SDKCandidateRejected('不允许新增空目录', relative)
            continue
        if not old and current['mode'] & 0o111:
            raise SDKCandidateRejected('不允许新建可执行文件', relative)
        if any(fnmatchcase(relative, pattern) for pattern in sdk_workspace.exclusions):
            continue
        authority.path(relative, write=True)
        data = (sdk_workspace.source / relative).read_bytes()
        if digest(data) != current['digest']:
            raise SDKCandidateRejected('导出期间候选变化', relative)
        if b'\0' in data:
            raise SDKCandidateRejected('不允许二进制内容', relative)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as error:
            raise SDKCandidateRejected('只允许 UTF-8 普通文本', relative) from error
        if any(secret in text for secret in secrets):
            # 异常信息只含路径，不得回显凭据值。
            raise SDKCandidateRejected('候选包含受保护凭据', relative)
        if not text.strip():
            raise SDKCandidateRejected('现有补丁契约不允许空内容', relative)
        if old:
            original = (sdk_workspace.baseline / relative).read_bytes()
            try:
                original.decode('utf-8')
            except UnicodeDecodeError as error:
                raise SDKCandidateRejected('不允许修改二进制输入', relative) from error
            if b'\0' in original:
                raise SDKCandidateRejected('不允许修改二进制输入', relative)
        edits.append(FileEdit(path=relative, before_hash=old['digest'] if old else digest(b''),
                              content=text, operation='modify' if old else 'create'))
    if _snapshot(sdk_workspace.source) != after:
        raise SDKCandidateRejected('导出期间完整源码发生变化')
    return PatchProposal(summary=summary, evidence_refs=evidence_refs, rule_refs=list(rule_refs), edits=edits)


def materialize_candidate(sdk_workspace: SDKWorkspace, proposal: PatchProposal, target: Path) -> Workspace:
    sdk_workspace.check_input()
    destination = plain_root(Path(target))
    if destination.exists() or destination.is_relative_to(sdk_workspace.execution_dir):
        raise SDKCandidateRejected('验证副本必须使用源码副本之外的全新目录')
    frozen = sdk_workspace.authority_snapshot
    source = Path(frozen['workspace_root'])
    run_git(None, 'clone', '--no-hardlinks', '--', str(source), str(destination), local_clone=True)
    git(destination, 'reset', '--hard', frozen['workspace_repository']['head'])
    git(destination, 'remote', 'remove', 'origin')
    if frozen['workspace_repository']['origin']:
        git(destination, 'remote', 'add', 'origin', frozen['workspace_repository']['origin'])
    branch = frozen['workspace_repository']['branch']
    if branch:
        git(destination, 'checkout', '-B', branch, frozen['workspace_repository']['head'])
    files = {}
    for relative, entry in sdk_workspace.input_snapshot.entries.items():
        if entry['kind'] != 'file':
            continue
        data = (sdk_workspace.baseline / relative).read_bytes()
        path = destination / relative
        if is_link(path) or not path.is_file() or path.read_bytes() != data:
            raise SDKCandidateRejected('验证 clone 与冻结输入不一致', relative)
        files[relative] = digest(data)
    tracked = {name.decode() for name in git(destination, 'ls-files', '-z').split(b'\0') if name}
    if tracked != files.keys():
        raise SDKCandidateRejected('验证 clone 文件集合与冻结输入不一致')
    workspace = Workspace(destination, list(sdk_workspace.allowed_files),
                          protected_paths=sdk_workspace.protected_paths)
    workspace.prepare_mountpoints(create=True)
    snapshot = deepcopy(sdk_workspace.authority_snapshot)
    snapshot.update(workspace_root=str(destination), workspace_repository=repository_binding(destination),
                    files=files, entries=file_manifest(destination))
    workspace.repository_snapshot = snapshot
    workspace.require_repository_snapshot()
    for edit in proposal.edits:
        workspace.path(edit.path, write=True)
    return workspace
