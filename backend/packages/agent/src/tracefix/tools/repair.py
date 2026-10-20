"""混合修复的结束信号与 overlay 撤销，不直接应用补丁或推进阶段。"""
from __future__ import annotations

from pydantic import Field, model_validator

from tracefix.model.contracts import ModelOutputError
from tracefix.runtime.contracts import Contract, FileEdit, FinishRepair, PatchProposal, Phase, digest
from tracefix.tools.core import ToolRejected, ToolResult
from tracefix.tools.local import LocalTools


class RevertOverlayInput(Contract):
    paths: list[str] = Field(default_factory=list, max_length=100)
    all: bool = False

    @model_validator(mode='after')
    def exactly_one_selection(self):
        if bool(self.paths) == self.all:
            raise ValueError('必须且只能指定非空 paths 或 all=true')
        if any(not path.strip() for path in self.paths):
            raise ValueError('撤销路径不能为空')
        return self


class RepairRejected(ToolRejected):
    def __init__(self, code, message, **details):
        self.details = {'error_code': code, 'written': False, 'disk_written': False,
                        'candidate_accepted': False, **details}
        super().__init__(message)


def _require_hybrid(state):
    if state.mode != 'repair' or state.phase != Phase.DIAGNOSE or state.repair_mode != 'hybrid':
        raise RepairRejected('REPAIR_PHASE_REQUIRED', '仅混合修复 DIAGNOSE 阶段可操作修复 overlay')


def build_repair_proposal(engine, state, finish):
    """仅从实际 overlay 构造完整候选，模型不能指定路径、内容或哈希。"""
    _require_hybrid(state)
    workspace = engine.workspace
    source = workspace.require_repository_snapshot()
    if source['scope_id'] != state.scope_id:
        raise RepairRejected('SOURCE_BINDING_MISMATCH', '工作区源码快照与当前 Run 不一致')
    workspace.check_frozen(source)
    edits = []
    for relative, content in sorted(workspace._staged_files.items()):
        path = workspace.path(relative, write=True)
        disk = workspace.baseline_bytes(relative)
        if (digest(disk) != workspace._staged_disk_hashes.get(relative)
                or path.exists() != workspace._staged_disk_exists.get(relative, True)):
            raise RepairRejected('DISK_DRIFT', '磁盘源码在编辑后发生变化，请重读并重建候选', path=relative)
        operation = workspace.edit_operation(relative)
        if content != disk or operation == 'create':
            edits.append(FileEdit(path=relative, before_hash=digest(disk),
                                  content=content.decode('utf-8'), operation=operation))
    if not edits:
        raise RepairRejected('NO_CHANGE', '当前 overlay 没有相对磁盘的实际字节变化')
    if len(edits) > 8:
        raise RepairRejected('TOO_MANY_FILES', '单次候选最多修改 8 个文件', paths=[item.path for item in edits])
    refs = list(dict.fromkeys(state.evidence_refs + state.initial_check_result_refs))
    for ref in refs:
        if not engine.bundle_exists(state, ref):
            raise RepairRejected('INVALID_EVIDENCE', '复现或初始检查证据缺失、损坏或不属于当前 Run', ref=ref)
    allowed = set(((state.loop_evidence or {}).get('repair') or {}).get('self_test_refs') or [])
    for ref in finish.self_test_refs:
        if ref not in allowed:
            raise RepairRejected('INVALID_SELF_TEST_REF', '自测引用未由当前 Run 的 RunTests 产生', ref=ref)
        try:
            result = engine.get(state, ref)
            if (not isinstance(result, dict) or result.get('kind') != 'repair_self_test'
                    or result.get('run_id') != state.run_id or result.get('scope_id') != state.scope_id
                    or result.get('source_manifest') != state.source_manifest
                    or result.get('target') not in {'unit', 'static', 'build'}
                    or type(result.get('passed')) is not bool or type(result.get('exit_code')) is not int
                    or type(result.get('overlay_revision')) is not int
                    or not isinstance(result.get('diff_fingerprint'), str)):
                raise ValueError('自测结果类型或绑定不匹配')
            engine.artifacts.read(state.scope_id, state.run_id, result['output_ref'])
        except (KeyError, OSError, ValueError, PermissionError, TypeError) as error:
            raise RepairRejected('INVALID_SELF_TEST_REF', '自测引用缺失、损坏或绑定不匹配', ref=ref) from error
        refs.append(ref)
    if not refs:
        raise RepairRejected('EVIDENCE_REQUIRED', '提交修复需要当前 Run 的复现、初始检查或自测证据')
    return PatchProposal(summary=finish.summary, evidence_refs=list(dict.fromkeys(refs)), edits=edits,
                         guidance_ack=finish.diagnosis.guidance_ack if finish.diagnosis else [])


def _observe(engine, state, action, **kwargs):
    from tracefix.runtime.repair_progress import observe_repair

    return observe_repair(engine, state, action, **kwargs)


def _assessment_fields(assessment, fingerprint=None):
    fields = {'candidate_fingerprint': fingerprint}
    history = (assessment.get('evidence') or {}).get('history') or []
    if history:
        fields['normalized_diff_fingerprint'] = history[-1].get('diff_fingerprint')
    warnings = assessment.get('warnings', []) + assessment.get('signals', [])
    if warnings:
        fields['loop_warning'] = warnings
    return fields


def register_repair_tools(engine, state, context, bind, *, schema, submissions, validate_output=None):
    # 子 Agent 没有结束父修复段或撤销共享 overlay 的能力。
    if (context.get('worker_depth') == 1 or state.mode != 'repair'
            or state.repair_mode != 'hybrid' or state.phase != Phase.DIAGNOSE):
        return
    local = LocalTools(engine, context, state)

    async def revert(arguments, call_id):
        try:
            _require_hybrid(state)
            local.require_managed()
            with local.mutex.workspace_write():
                paths = sorted(engine.workspace._staged_files) if arguments.all else arguments.paths
                removed = engine.workspace.unstage_many(paths)
        except (ValueError, PermissionError, OSError) as error:
            raise ToolRejected(str(error)) from error
        # overlay 已经改变后，持久化失败必须保留 UNKNOWN，不能误称为确定未执行。
        if removed:
            refs = [item for item in state.staged_candidate_refs if item['path'] not in removed]
            updated = engine.changed(state, staged_candidate_refs=refs,
                                     loop_no_progress_steps=state.loop_no_progress_steps)
            for field in type(state).model_fields:
                setattr(state, field, getattr(updated, field))
        result = {'reverted_paths': removed, 'overlay_revision': engine.workspace.overlay_revision,
                  'disk_written': False}
        assessment = _observe(engine, state, 'revert_overlay', result=result)
        result.update(_assessment_fields(assessment))
        return result

    bind('RevertOverlay', '撤销指定单个或多个相对路径的暂存修改，或 all=true 撤销全部 overlay；'
         '只恢复到当前磁盘源码，不改磁盘或 Git 提交。', RevertOverlayInput, revert,
         phases={Phase.DIAGNOSE}, side_effect='write', parallel_safe=False,
         idempotency_key=lambda arguments: digest([arguments.model_dump(mode='json'),
                                                   engine.workspace.overlay_revision]))
    if schema is not FinishRepair:
        return

    async def finish(arguments, call_id):
        fingerprint = None
        normalized = None
        try:
            local.require_managed()
            with local.mutex.workspace_write():
                proposal = build_repair_proposal(engine, state, arguments)
                fingerprint = engine.candidate_fingerprint(proposal)
                from tracefix.runtime.repair_progress import normalized_diff_fingerprint
                normalized = normalized_diff_fingerprint(engine.workspace, state)
                engine.validate_patch_candidate(proposal, state=state)
                if validate_output:
                    validate_output(proposal)
        except (ValueError, PermissionError, OSError, ModelOutputError) as error:
            if getattr(error, 'status', 'FAILED') != 'FAILED':
                raise
            details = dict(getattr(error, 'details', {}))
            details.setdefault('error_code', getattr(error, 'error_code', 'REPAIR_REJECTED'))
            details.update(written=False, disk_written=False, candidate_accepted=False)
            assessment = _observe(engine, state, 'finish_repair', candidate_fingerprint=fingerprint,
                                  normalized_diff_fingerprint=normalized,
                                  accepted=False, error_code=details['error_code'])
            details.update(_assessment_fields(assessment, fingerprint))
            return ToolResult(call_id=call_id, name='finish_repair', isError=True, result=details,
                              error={**details, 'message': str(error), 'executed': False})
        audit = engine.put(state, {'kind': 'finish_repair', 'run_id': state.run_id,
            'scope_id': state.scope_id, 'input': arguments.model_dump(mode='json'),
            'candidate_fingerprint': fingerprint,
            'normalized_diff_fingerprint': normalized}, name='混合修复结束审计')
        assessment = _observe(engine, state, 'finish_repair', candidate_fingerprint=fingerprint,
                              normalized_diff_fingerprint=normalized, accepted=True, ref=audit)
        submissions['finish_repair'] = proposal
        result = {'accepted': True, 'submission': 'finish_repair',
                  'proposal': proposal.model_dump(mode='json'), 'audit_ref': audit}
        result.update(_assessment_fields(assessment, fingerprint))
        return result

    bind('finish_repair', '结束本轮内层修复。仅提交摘要、可选诊断和 RunTests 证据引用；'
         '运行时从当前 overlay 生成并校验候选补丁，失败后可继续修正。', FinishRepair, finish,
         phases={Phase.DIAGNOSE}, side_effect='write', parallel_safe=False,
         submission=True, output_limit_tokens=2000)
