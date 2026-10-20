"""人工测试文件授权：只在 TestSpec 冻结前由可信输入入口签发。"""
from __future__ import annotations

from tracefix.execution.platforms import safe_relative
from tracefix.execution.workspace import test_write_is_approved
from tracefix.runtime.contracts import Phase, RunStatus, digest


def _files(workspace, files):
    if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= 32:
        raise PermissionError('人工审批须指定 1–32 个精确测试文件')
    selected = sorted(set(files))
    for relative in selected:
        safe_relative(relative)
        if any(char in relative for char in '*?[]') or not test_write_is_approved(
                relative, selected, workspace.protected_paths):
            raise PermissionError('审批仅允许精确测试文件，不能覆盖 Oracle、CI、锁或保护路径')
    previous = workspace.approved_test_files
    try:
        workspace.approved_test_files = tuple(selected)
        for relative in selected:
            workspace.path(relative, write=True)
    finally:
        workspace.approved_test_files = previous
    return selected


def authorize_test_edits(store, artifacts, state, workspace, files, *, reviewer):
    """由 CLI 人工参数调用；不注册为模型工具或接受模型提出的审批。"""
    if (state.phase != Phase.PREPARE or state.run_status != RunStatus.RUNNING
            or state.test_spec_ref or state.test_spec_hash or state.patch_hash
            or state.test_edit_approval_ref or state.reproduction_plan_frozen
            or state.mode != 'repair' or not state.source_manifest or not state.repo_snapshot_ref):
        raise PermissionError('测试文件必须在修复 Run 的 TestSpec 冻结前审批')
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise PermissionError('人工审批缺少操作者身份')
    selected = _files(workspace, files)
    action = {'action': 'authorize_test_edits', 'version': 1, 'files': selected,
              'run_id': state.run_id, 'scope_id': state.scope_id,
              'source_manifest': state.source_manifest, 'repo_snapshot_ref': state.repo_snapshot_ref,
              'reviewer': reviewer, 'issued_before_spec': True}
    key = store.approval(state, action)
    store.decide_approval(key, state, 'approve')
    if not store.consume_approval(key, state, action):
        raise PermissionError('测试文件审批被拒绝')
    record = {'action': action, 'approval_id': key, 'action_hash': digest(action),
              'patch_hash_at_approval': None}
    ref = artifacts.put(state.scope_id, state.run_id, record, label='人工测试文件审批')
    state.test_edit_requested_files = selected
    state.approved_test_files = selected
    state.test_edit_approval_id = key
    state.test_edit_approval_ref = ref
    workspace.approved_test_files = tuple(selected)
    store.event(state, 'test_edit.approved', {'approval_id': key, 'approval_ref': ref,
                                            'action_hash': digest(action), 'files': selected})
    store.save(state)
    return state


def verified_test_edit_files(store, artifacts, state, workspace):
    """从持久化的审批和源码绑定恢复权限，裸 state 路径列表没有授权效力。"""
    fields = (state.test_edit_requested_files, state.approved_test_files,
              state.test_edit_approval_ref, state.test_edit_approval_id)
    if not any(fields):
        return ()
    if not all(fields):
        raise PermissionError('测试文件审批证据不完整')
    record = artifacts.json(state.scope_id, state.run_id, state.test_edit_approval_ref)
    action = record.get('action', {})
    selected = _files(workspace, action.get('files', []))
    expected = {'action': 'authorize_test_edits', 'version': 1, 'files': selected,
                'run_id': state.run_id, 'scope_id': state.scope_id,
                'source_manifest': state.source_manifest, 'repo_snapshot_ref': state.repo_snapshot_ref,
                'reviewer': action.get('reviewer'), 'issued_before_spec': True}
    if (action != expected or not isinstance(action.get('reviewer'), str) or not action['reviewer'].strip()
            or selected != state.test_edit_requested_files or selected != state.approved_test_files
            or record.get('approval_id') != state.test_edit_approval_id
            or record.get('action_hash') != digest(action)
            or record.get('patch_hash_at_approval', 'missing') is not None
            or not store.verify_consumed_approval(state.test_edit_approval_id, state, action, patch_hash=None)):
        raise PermissionError('测试文件人工审批已失效或绑定不匹配')
    return tuple(selected)


def restore_test_edit_approval(engine, state):
    engine.workspace.approved_test_files = verified_test_edit_files(
        engine.store, engine.artifacts, state, engine.workspace)
    return state


def ensure_test_edit_approval(engine, state):
    return restore_test_edit_approval(engine, state)
