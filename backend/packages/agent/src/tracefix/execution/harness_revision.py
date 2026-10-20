from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from tracefix.execution.repository import plain_root, repository_binding, run_git
from tracefix.execution.workspace import Workspace, file_manifest, validate_protected_paths
from tracefix.runtime.contracts import Contract, digest


class RevisionReceipt(Contract):
    kind: str = 'harness_revision'
    scope_id: str
    job_id: str
    session_id: str
    run_id: str
    origin_base_commit: str
    input_commit: str
    candidate_commit: str
    candidate_repository: str
    export_base_commit: str
    input_tree: str
    candidate_tree: str
    input_files: dict[str, str]
    candidate_files: dict[str, str]
    patch_hash: str
    verification_ref: str
    previous_receipt_ref: str | None = None
    previous_run_id: str | None = None
    registered_config_hash: str


def tracked_files(root: Path, commit: str) -> dict[str, str]:
    result = {}
    for record in run_git(root, 'ls-tree', '-rz', commit).split(b'\0'):
        if not record:
            continue
        metadata, name = record.split(b'\t', 1)
        mode, kind, blob = metadata.decode().split()
        if mode != '100644' or kind != 'blob':
            raise PermissionError('派生 revision 包含不支持的文件种类或模式')
        result[name.decode('utf-8')] = digest(run_git(root, 'cat-file', 'blob', blob))
    return result


def validate_receipt(receipt: RevisionReceipt, *, job, project, previous=None):
    if (receipt.scope_id != job.scope_id or receipt.job_id != job.job_id
            or receipt.origin_base_commit != job.origin_base_commit
            or receipt.registered_config_hash != digest(project.model_dump(mode='json'))):
        raise PermissionError('派生 revision 与冻结 Job/注册配置不一致')
    root = plain_root(Path(receipt.candidate_repository))
    binding = repository_binding(root)
    if binding['head'] != receipt.candidate_commit or binding['tree'] != receipt.candidate_tree:
        raise PermissionError('已接纳候选仓库发生漂移')
    parents = run_git(root, 'rev-list', '--parents', '-n', '1', receipt.candidate_commit).decode().split()
    if parents != [receipt.candidate_commit, receipt.export_base_commit]:
        raise PermissionError('候选提交必须直接继承冻结输入提交')
    if run_git(root, 'rev-parse', receipt.export_base_commit + '^{tree}').decode().strip() != receipt.input_tree:
        raise PermissionError('派生 revision 输入树不匹配')
    if (tracked_files(root, receipt.export_base_commit) != receipt.input_files
            or tracked_files(root, receipt.candidate_commit) != receipt.candidate_files):
        raise PermissionError('派生 revision 文件内容不匹配')
    if not set(receipt.input_files) <= set(receipt.candidate_files):
        raise PermissionError('派生 revision 不允许删除文件')
    authority = Workspace(root, project.allowed_files,
        protected_paths=(project.repository_config or {}).get('protected_paths', []))
    for path in set(receipt.input_files) | set(receipt.candidate_files):
        if receipt.input_files.get(path) != receipt.candidate_files.get(path):
            authority.path(path, write=True)
    actual_diff = run_git(root, 'diff', '--no-color', receipt.export_base_commit,
                          receipt.candidate_commit, '--').decode('utf-8')
    if digest(actual_diff.encode()) != receipt.patch_hash:
        raise PermissionError('派生 revision 补丁与提交回执不一致')
    if previous is None:
        if receipt.input_commit != job.origin_base_commit or receipt.previous_receipt_ref is not None:
            raise PermissionError('派生 revision 缺少原始基准 lineage')
    elif (receipt.input_commit != previous.candidate_commit
            or receipt.input_files != previous.candidate_files
            or receipt.previous_receipt_ref is None):
        raise PermissionError('派生 revision 没有继承上一已接纳候选')
    return receipt


def validate_workspace_revision(workspace: Workspace, snapshot: dict, *, job, project,
                                original_snapshot: dict, receipts=(), base_commit=None,
                                patch_hash=None, branch=None):
    """验证已接纳候选仓库物化后的 Workspace，不调用注册仓库的普通基线校验。

    派生候选的 commit 只存在于受控候选仓库；registered source 仍通过
    ``original_snapshot`` 绑定。该入口只负责身份、lineage 和完整文件内容，
    不会改变 legacy ``Workspace.validate_repository`` 的规则。
    """
    if not isinstance(snapshot, dict) or not isinstance(original_snapshot, dict):
        raise PermissionError('派生 Workspace 快照必须是对象')
    protected_paths = validate_protected_paths((project.repository_config or {}).get('protected_paths', []))
    if workspace.allowed_files != list(project.allowed_files) or workspace.protected_paths != protected_paths:
        raise PermissionError('派生 Workspace 授权范围与冻结项目不一致')
    original = Workspace(Path(original_snapshot['workspace_root']), project.allowed_files,
        protected_paths=(project.repository_config or {}).get('protected_paths', [])).require_repository_snapshot(original_snapshot)
    candidate = workspace.require_repository_snapshot(snapshot)
    if job.repo_id != project.repo_id or (original.get('scope_id'), original.get('repo_id')) != (job.scope_id, project.repo_id):
        raise PermissionError('原始源码快照与 Job/注册仓库身份不一致')
    if original.get('commit') != job.origin_base_commit:
        raise PermissionError('原始源码快照不是 Job 冻结基准')
    if original.get('repository_config') != project.repository_config:
        raise PermissionError('原始源码快照与当前注册配置不一致')
    if project.repository_config:
        from tracefix.repository_registry import validate_stored_config
        validate_stored_config(project)
        if project.repository_config['commit'] != original['commit']:
            raise PermissionError('注册配置基准与原始快照不一致')
    if plain_root(project.root) != Path(original['source_root']).resolve():
        raise PermissionError('注册源码路径与原始快照不一致')
    source = Path(original['source_repository']['root'])
    top = Path(run_git(project.root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
    if source.resolve() != top:
        raise PermissionError('注册源码顶层仓库与原始快照不一致')
    current_source = repository_binding(source, project.repo_id, original['commit'], require_clean=False)
    if current_source != original['source_repository']:
        raise PermissionError('注册仓库 origin/HEAD/branch/commit 身份已漂移')
    if run_git(source, 'rev-parse', f"{original['commit']}^{{tree}}").decode().strip() != original['source_tree']:
        raise PermissionError('注册仓库基准树已漂移')
    chain = list(receipts)
    if not chain:
        raise PermissionError('派生 Workspace 缺少完整 revision lineage')
    previous = None
    for receipt in chain:
        receipt = RevisionReceipt.model_validate(receipt)
        validate_receipt(receipt, job=job, project=project, previous=previous)
        if previous is None and receipt.input_files != original['files']:
            raise PermissionError('首个 revision 未继承原始导出内容')
        if previous is not None and receipt.previous_run_id != previous.run_id:
            raise PermissionError('revision lineage 的 Run 引用不连续')
        previous = receipt
    accepted = RevisionReceipt.model_validate(chain[-1])
    if accepted.candidate_commit != job.accepted_commit:
        raise PermissionError('Workspace 不是 Job 当前已接纳候选')
    if candidate.get('commit') != accepted.candidate_commit or candidate.get('files') != accepted.candidate_files:
        raise PermissionError('Workspace 快照与已接纳候选回执不一致')
    if candidate.get('scope_id') != job.scope_id or candidate.get('repo_id') != project.repo_id:
        raise PermissionError('派生 Workspace 与 Job/注册仓库身份不一致')
    if candidate.get('source_repository') != original.get('source_repository'):
        raise PermissionError('派生 Workspace 改写了注册仓库身份')
    for field in ('source_root', 'source_tree', 'subdir', 'repository_config'):
        if candidate.get(field) != original.get(field):
            raise PermissionError('派生 Workspace 改写了原始仓库绑定')
    if candidate.get('harness_revision') != accepted.model_dump(mode='json'):
        raise PermissionError('派生 Workspace 回执不匹配')
    expected_base = base_commit or candidate['workspace_repository']['head']
    if expected_base != accepted.candidate_commit:
        raise PermissionError('Workspace baseline 与 revision 回执不一致')
    current_binding = repository_binding(workspace.root, require_clean=False)
    expected_binding = candidate['workspace_repository']
    if (current_binding['origin'] != expected_binding['origin']
            or current_binding['branch'] not in {branch, expected_binding['branch']}
            or expected_binding['tree'] != accepted.candidate_tree
            or expected_binding['head'] != accepted.candidate_commit):
        raise PermissionError('物化 Workspace origin/branch/tree 与候选回执不一致')
    if current_binding['head'] != expected_base:
        if not branch or current_binding['branch'] != branch:
            raise PermissionError('Workspace HEAD 未绑定本次候选分支')
        run_git(workspace.root, 'merge-base', '--is-ancestor', expected_base, current_binding['head'])
        committed = tracked_files(workspace.root, current_binding['head'])
        if not set(accepted.candidate_files) <= set(committed):
            raise PermissionError('本次候选删除了已接纳文件')
        for path, checksum in committed.items():
            if checksum != accepted.candidate_files.get(path):
                workspace.path(path, write=True)
    if workspace.root != Path(candidate['workspace_root']).resolve():
        raise PermissionError('物化 Workspace 路径与快照不一致')
    actual_files = tracked_files(workspace.root, accepted.candidate_commit)
    if actual_files != accepted.candidate_files or actual_files != candidate['files']:
        raise PermissionError('物化 Workspace 文件内容与候选回执不一致')
    actual_diff = workspace.diff(base=expected_base)
    if (patch_hash and digest(actual_diff.encode()) != patch_hash) or (not patch_hash and actual_diff):
        raise PermissionError('本次候选 Workspace.diff 与冻结 patch hash 不一致')
    previous_entries = workspace._created_entries
    try:
        if patch_hash:
            workspace._created_entries = workspace._new_entries(candidate, file_manifest(workspace.root))
        workspace.check_frozen(candidate)
    except BaseException:
        workspace._created_entries = previous_entries
        raise
    return candidate


def materialize_revision(receipt: RevisionReceipt, target: Path, *, job, project,
                         original_snapshot: dict, previous=None, receipts=None):
    validate_receipt(receipt, job=job, project=project, previous=previous)
    if receipt.candidate_commit != job.accepted_commit:
        raise PermissionError('仅能从 Job 当前已接纳 revision 创建 Run')
    target = plain_root(target)
    if target.exists():
        raise FileExistsError('派生 revision 必须物化到新目录')
    run_git(None, 'clone', '--no-hardlinks', '--', receipt.candidate_repository,
            str(target), local_clone=True)
    run_git(target, 'config', '--unset-all', 'remote.origin.url', check=False)
    run_git(target, 'checkout', '-B', 'tracefix/export', receipt.candidate_commit)
    workspace = Workspace(target, list(project.allowed_files),
        protected_paths=(project.repository_config or {}).get('protected_paths', []))
    workspace.prepare_mountpoints(create=True)
    snapshot = deepcopy(original_snapshot)
    snapshot.update(commit=receipt.candidate_commit, files=receipt.candidate_files,
        workspace_root=str(target), workspace_repository=repository_binding(target),
        entries=file_manifest(target), harness_revision=receipt.model_dump(mode='json'))
    workspace.repository_snapshot = snapshot
    validate_workspace_revision(workspace, snapshot, job=job, project=project,
        original_snapshot=original_snapshot, receipts=receipts or ([previous, receipt] if previous else [receipt]))
    return workspace, snapshot


def cumulative_diff(original_files: dict[str, bytes], final_root: Path,
                    final_commit: str) -> str:
    import difflib

    final_files = tracked_files(final_root, final_commit)
    if not set(original_files) <= set(final_files):
        raise PermissionError('累计候选删除了原始文件')
    output = []
    for path in sorted(set(original_files) | set(final_files)):
        before = original_files.get(path, b'')
        after = run_git(final_root, 'show', final_commit + ':' + path)
        if before == after:
            continue
        output.extend(difflib.unified_diff(before.decode('utf-8').splitlines(keepends=True),
            after.decode('utf-8').splitlines(keepends=True),
            fromfile='a/' + path if path in original_files else '/dev/null',
            tofile='b/' + path))
    return ''.join(output)
