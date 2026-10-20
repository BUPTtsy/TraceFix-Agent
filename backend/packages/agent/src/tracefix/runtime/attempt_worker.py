"""可写 Worker 的独立源码导出；候选只作为父模型可检查的诊断产物。"""
from __future__ import annotations

import copy
import tempfile
from contextlib import contextmanager
from pathlib import Path

from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import Phase, digest
from tracefix.tools.core import ToolRejected
from tracefix.tools.effects import apply_effect_receipt
from tracefix.tools.fingerprints import diff_fingerprint


def parent_view(engine, state):
    workspace = engine.workspace
    return (id(workspace), workspace.overlay_revision, workspace.head(), state.source_manifest,
            state.revision, diff_fingerprint(workspace, state))


@contextmanager
def attempt_workspace(engine, state, child, task):
    """每次调用拥有独立磁盘、overlay、源码绑定；不替换父 engine 的任何对象。"""
    if state.mode != 'repair' or state.phase not in {Phase.DIAGNOSE, Phase.PATCH}:
        raise ToolRejected('可写尝试只允许修复阶段')
    snapshot = engine.workspace.require_repository_snapshot()
    parent = engine.workspace
    view = parent_view(engine, state)
    # 先固定父视图，后续版本变化时由 delegate 拒绝结果。
    baseline = {}
    for relative in task.allowed_files:
        path = parent.path(relative)
        staged = parent.staged_content(relative)
        baseline[relative] = staged if staged is not None else (path.read_bytes() if path.exists() else None)
    with tempfile.TemporaryDirectory(prefix='tracefix-attempt-') as temporary:
        workspace, source = Workspace.export(engine.scopes, engine.context, snapshot['commit'],
                                              Path(temporary) / 'workspace')
        workspace.protected_paths = list(parent.protected_paths)
        workspace.approved_test_files = tuple(parent.approved_test_files)
        workspace.guidance_constraints = copy.deepcopy(parent.guidance_constraints)
        # 运行测试需要完整冻结源码；读写工具仍由 Worker 的精确白名单收窄。
        staged = {}
        created = {relative for relative, entry in parent._created_entries.items()
                   if entry['kind'] == 'file'}
        for relative in set(snapshot['files']) | set(parent._staged_files) | created:
            path = parent.path(relative)
            data = parent.staged_content(relative)
            if data is None:
                data = path.read_bytes() if path.exists() else None
            if data is not None and (data != workspace.baseline_bytes(relative)
                                     or workspace.edit_operation(relative) == 'create'):
                staged[relative] = data.decode('utf-8')
        if staged:
            with apply_effect_receipt():
                workspace.stage_many(staged)
        isolated = copy.copy(engine)
        isolated.workspace, isolated.source = workspace, source
        isolated.subagent_depth = 1
        isolated.worker_attempt = True
        isolated.worker_write_enabled = True
        isolated.worker_allowed_files = list(task.allowed_files)
        isolated.worker_writable_files = list(task.writable_files)
        isolated.worker_allowed_tools = list(task.allowed_tools)
        isolated.worker_shell_mode = task.shell_mode
        isolated.memory = None
        isolated.runtime_services = None
        # Fake/本地模型可持有 workspace；也必须绑定子工作区。
        for name in ('model', 'worker_model'):
            model = getattr(isolated, name, None)
            if model is not None and hasattr(model, 'workspace'):
                model = copy.copy(model)
                model.workspace = workspace
                setattr(isolated, name, model)
        child.phase = Phase.DIAGNOSE
        child.source_manifest = digest(source)
        child.repo_snapshot_ref = isolated.put(child, source, name='独立尝试源码快照')
        child.patch_base_commit = source['workspace_repository']['head']
        child.patch_hash = None
        child.overlay_snapshot_ref = None
        child.staged_candidate_refs = []
        child.loop_evidence = {}
        child.validation_index = child.replay_index = 0
        child.reproduced = False
        isolated.store.save(child)
        if parent_view(engine, state) != view:
            raise ToolRejected('导出 Worker 时父工作区已变化，拒绝不一致的尝试基线')
        yield isolated, {'files': baseline, 'view': view}


def attempt_candidate(engine, state, child_engine, child, task, baseline):
    """从实际子 overlay 生成建议，不信任 Worker 自报的补丁或成功标记。"""
    if parent_view(engine, state) != baseline['view']:
        raise ToolRejected('Worker 执行期间父工作区已变化，拒绝过期候选')
    baseline = baseline['files']
    changes = []
    for relative in task.writable_files:
        current = child_engine.workspace.staged_content(relative)
        if current is None or current == baseline[relative]:
            continue
        changes.append({'path': relative, 'before_hash': digest(baseline[relative] or b''),
                        'operation': 'create' if baseline[relative] is None else 'modify',
                        'content': current.decode('utf-8')})
    for relative, previous in baseline.items():
        path = engine.workspace.path(relative)
        current = engine.workspace.staged_content(relative)
        if current is None:
            current = path.read_bytes() if path.exists() else None
        if current != previous:
            raise ToolRejected('Worker 执行期间父 overlay 或磁盘已变化，拒绝过期候选')
    tests = []
    for reference in ((child.loop_evidence or {}).get('repair') or {}).get('self_test_refs', []):
        result = dict(child_engine.get(child, reference))
        if (result.get('kind') != 'repair_self_test' or result.get('run_id') != child.run_id
                or result.get('scope_id') != child.scope_id
                or result.get('source_manifest') != child.source_manifest):
            raise ToolRejected('Worker 自测结果与独立尝试身份不一致')
        output_ref = result.get('output_ref')
        output = child_engine.artifacts.read(child.scope_id, child.run_id, output_ref)
        result['child_output_ref'] = output_ref
        result['output_ref'] = engine.artifacts.put(state.scope_id, state.run_id, output,
                                                   'txt', label='Worker尝试自测输出')
        tests.append(result)
    ref = engine.put(state, {'kind': 'worker_attempt_candidate', 'child_run_id': child.run_id,
        'parent_run_id': state.run_id, 'source_revision': task.source_revision,
        'source_manifest': state.source_manifest, 'edits': changes,
        'self_tests': tests,
        'applied_to_parent': False, 'verification_evidence': False,
        'requires_parent_edit_and_verify': True}, name='独立Worker尝试候选')
    return {'candidate_ref': ref, 'files': [item['path'] for item in changes],
            'applied_to_parent': False, 'requires_parent_edit_and_verify': True}
