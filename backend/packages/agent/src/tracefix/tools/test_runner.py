"""诊断期执行 Profile 审计过的测试命令，不产生验证结论。"""
from __future__ import annotations

from typing import Literal

from tracefix.execution.runner import DockerRunner
from tracefix.execution.test_reports import parse_test_report
from tracefix.runtime.contracts import Contract, Phase
from tracefix.tools.core import ToolRejected
from tracefix.tools.fingerprints import record_tool_action
from tracefix.tools.local import LocalTools


class RunTestsInput(Contract):
    target: Literal['unit', 'static', 'build'] = 'unit'


def register_test_runner(engine, state, context, bind):
    profile = getattr(engine, 'profile', None)
    if profile is None:
        return
    tools = LocalTools(engine, context, state)

    async def run_tests(arguments, call_id):
        tools.require_tool('RunTests')
        runner = getattr(engine, 'runner', None)
        if not isinstance(runner, DockerRunner) or not runner._started:
            raise ToolRejected('RunTests 需要已启动的 Docker sandbox；请先完成 PREPARE，不能在宿主机执行')
        tools.require_managed()
        argv = profile.commands.get(arguments.target)
        if not isinstance(argv, list) or not argv or any(not isinstance(value, str) or not value for value in argv):
            raise ToolRejected('RunTests target 未配置有效的 Profile argv')
        try:
            options = {'complete_source': True} if getattr(engine, 'worker_attempt', False) else {}
            result = await tools.sandbox_run(argv, writable=False, timeout=profile.timeout_seconds,
                                             output_limit=None, **options)
        except FileNotFoundError as error:
            raise ToolRejected('RunTests Docker sandbox 不可用；未找到 Docker CLI，不能在宿主机执行') from error
        if result['exit_code'] == 125:
            raise ToolRejected('RunTests Docker sandbox 启动失败：' + result.get('output', '')[-1500:])
        output = result.get('output', '')
        output_ref = engine.artifacts.put(state.scope_id, state.run_id, output, 'txt', label='修复自测完整输出')
        parsed = parse_test_report(output)
        feedback = {'target': arguments.target, 'exit_code': result['exit_code'],
                    'passed': result['passed'], **parsed, 'overlay_revision': result['overlay_revision'],
                    'diff_fingerprint': result['diff_fingerprint'], 'output_ref': output_ref}
        if 'normalized_diff_fingerprint' in result:
            feedback['normalized_diff_fingerprint'] = result['normalized_diff_fingerprint']
        if parsed['summary']['unparsed']:
            feedback['output_tail'] = output[-4000:]
        feedback.update(run_id=state.run_id, scope_id=state.scope_id,
                        kind='repair_self_test', source_manifest=state.source_manifest)
        feedback['result_ref'] = engine.put(state, feedback, name='修复自测结果')
        from tracefix.runtime.repair_progress import observe_repair
        assessment = observe_repair(engine, state, 'run_tests', result=feedback,
                                    target=arguments.target)
        if assessment.get('warnings') or assessment.get('signals'):
            feedback['loop_warning'] = assessment.get('warnings', []) + assessment.get('signals', [])
        if state.repair_mode != 'hybrid':
            repair = dict((state.loop_evidence or {}).get('repair') or {})
            repair['self_test_refs'] = list(dict.fromkeys(
                repair.get('self_test_refs', []) + [feedback['result_ref']]))
            updated = engine.changed(state, loop_evidence={**(state.loop_evidence or {}), 'repair': repair},
                                     loop_no_progress_steps=state.loop_no_progress_steps)
            for field in type(state).model_fields:
                setattr(state, field, getattr(updated, field))
        record_tool_action(engine, state, 'RunTests', arguments, feedback)
        return feedback

    bind('RunTests', '在独立只读 Docker sandbox 中执行 Profile 已审计的 unit、static 或 build argv；'
         '使用当前 overlay 和预构建依赖，禁止自定义命令或参数。返回测试用例、失败签名与完整输出引用；'
         '结果仅供诊断，最终修复结论由 VERIFY 决定。',
         RunTestsInput, run_tests, phases={Phase.DIAGNOSE}, side_effect='read',
         parallel_safe=False, timeout_s=profile.timeout_seconds + 30)
