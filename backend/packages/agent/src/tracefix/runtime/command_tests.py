"""冻结的非 GUI 目标、真实失败基线与独立命令执行。"""
from collections import Counter

from tracefix.execution.test_reports import normalize_failure_message, parse_test_report
from tracefix.runtime.contracts import Outcome, Phase, digest


TARGETS = ('static', 'unit', 'build')
KINDS = (*TARGETS, 'original', 'regression')


def make_plan(spec, profile):
    if spec.mode not in TARGETS:
        raise ValueError('非 GUI 规范必须选择 unit/static/build')
    commands = {target: list(profile.commands[target]) for target in TARGETS}
    return {'type': 'command_plan', 'target': spec.mode, 'commands': commands,
            'command_hashes': {target: digest(argv) for target, argv in commands.items()},
            'regression_targets': [target for target in TARGETS if target != spec.mode]}


def valid_plan(plan, spec):
    if type(plan) is not dict or spec.mode not in TARGETS:
        return False
    commands = plan.get('commands')
    if (set(plan) != {'type', 'target', 'commands', 'command_hashes', 'regression_targets'}
            or plan.get('type') != 'command_plan' or plan.get('target') != spec.mode
            or type(commands) is not dict or set(commands) != set(TARGETS)
            or any(type(argv) is not list or not argv
                   or any(type(arg) is not str or not arg for arg in argv) for argv in commands.values())):
        return False
    return (plan['command_hashes'] == {target: digest(argv) for target, argv in commands.items()}
            and plan['regression_targets'] == [target for target in TARGETS if target != spec.mode])


def frozen_plan(engine, state):
    spec = engine.spec(state)
    plan = engine.get(state, state.replay_plan_ref)
    if not state.reproduction_plan_frozen or not valid_plan(plan, spec) or plan != make_plan(spec, engine.profile):
        raise PermissionError('非 GUI 复现目标或冻结 Profile 命令发生变化')
    binding = engine.get(state, state.reproduction_binding_ref)
    expected = {'source_manifest': state.source_manifest, 'environment_digest': state.environment_digest,
                'test_spec_hash': state.test_spec_hash, 'scope_id': state.scope_id,
                'run_id': state.run_id, 'plan_hash': digest(plan)}
    if binding != expected:
        raise PermissionError('非 GUI 复现计划与冻结运行绑定不一致')
    return plan


async def freeze(engine, state):
    if state.reproduction_plan_frozen:
        frozen_plan(engine, state)
        return state
    if state.patch_hash or state.patch_ref:
        raise ValueError('非 GUI 失败基线必须在候选补丁之前冻结')
    plan = make_plan(engine.spec(state), engine.profile)
    ref = engine.put(state, plan, name='非GUI冻结命令计划')
    binding = {'source_manifest': state.source_manifest, 'environment_digest': state.environment_digest,
               'test_spec_hash': state.test_spec_hash, 'scope_id': state.scope_id,
               'run_id': state.run_id, 'plan_hash': digest(plan)}
    binding_ref = engine.put(state, binding, name='非GUI复现绑定')
    return engine.changed(state, replay_plan_ref=ref, reproduction_binding_ref=binding_ref,
                          reproduction_plan_frozen=True)


def command_body(result, plan, target, *, require_pass=None):
    if (type(result) is not dict or type(result.get('exit_code')) is not int
            or type(result.get('output')) is not str or type(result.get('passed')) is not bool
            or result.get('target') != target or result.get('executed_argv') != plan['commands'][target]
            or result.get('command_hash') != plan['command_hashes'][target]
            or result.get('execution_plan_hash') != digest(plan)
            or result['passed'] != (result['exit_code'] == 0)
            or result.get('error') not in (None, '')):
        return False
    if result['exit_code'] < 0 or result['exit_code'] >= 125:
        return False
    report = parse_test_report(result['output'])
    if result['passed'] and report['summary']['failed']:
        return False
    return require_pass is None or result['passed'] is require_pass


def signature(result, target):
    if result['passed']:
        return 'PASS'
    report = parse_test_report(result['output'])
    failures = report['failure_signatures']
    return digest({'target': target, 'exit_code': result['exit_code'],
                   'failure_signatures': failures or [normalize_failure_message(result['output'])]})


async def execute(engine, state, target, *, operation, trial=None):
    plan = frozen_plan(engine, state)
    engine.workspace.check_frozen(engine.source)
    intent = {'target': target, 'command_hash': plan['command_hashes'][target],
              'plan_hash': digest(plan), 'patch_hash': state.patch_hash, 'trial': trial,
              'source_manifest': state.source_manifest, 'environment_digest': state.environment_digest}
    async def run():
        return await engine.runner.command(target)
    raw = await engine.operation(state, operation, intent, run)
    result = {**raw, **engine.verification_context(state), 'type': 'command_execution',
              'target': target, 'executed_argv': plan['commands'][target],
              'command_hash': plan['command_hashes'][target], 'execution_plan_hash': digest(plan)}
    if not command_body(result, plan, target):
        raise RuntimeError('非 GUI 命令执行未返回有效进程结果，不能作为缺陷证据')
    if not result['passed'] and not result['output'].strip():
        raise RuntimeError('非 GUI 命令失败但无诊断输出，不能建立稳定失败基线')
    engine.workspace.check_frozen(engine.source)
    return result


async def record_baseline(engine, state, *, trial=0):
    if state.patch_hash or state.patch_ref:
        raise PermissionError('非 GUI 复现不能读取候选补丁后的命令结果')
    target = engine.spec(state).mode
    result = await execute(engine, state, target, operation='baseline.command.' + str(trial), trial=trial)
    result.update(engine.verification_context(state), type='command_baseline', kind='baseline_' + target,
                  trial=trial, failure_signature=signature(result, target))
    ref = engine.put(state, result, name='非GUI失败基线')
    return engine.changed(state, baseline_validation_refs=state.baseline_validation_refs + [ref],
                          evidence_refs=list(dict.fromkeys(state.evidence_refs + [ref]))), result


def baseline_reproduced(state, plan, exists, read):
    from tracefix.runtime.verification import verification_binding
    expected = {**verification_binding(state, digest(plan)), 'patch_hash': None}
    trials = {}
    for ref in state.baseline_validation_refs:
        if type(ref) is not str or not exists(ref):
            return False
        result = read(ref)
        if (type(result) is not dict or result.get('type') != 'command_baseline'
                or result.get('kind') != 'baseline_' + plan['target']
                or any(result.get(key) != value for key, value in expected.items())
                or not command_body(result, plan, plan['target'])
                or result.get('failure_signature') != signature(result, plan['target'])
                or type(result.get('trial')) is not int or result['trial'] not in {0, 1, 2, 3}
                or result['trial'] in trials):
            return False
        trials[result['trial']] = result
    failures = Counter(result['failure_signature'] for trial, result in trials.items()
                       if trial in {1, 2, 3} and result['passed'] is False)
    return {1, 2, 3} <= trials.keys() and max(failures.values(), default=0) >= 2


async def explore(engine, state):
    frozen_plan(engine, state)
    state = await engine.ensure_check_plan(state)
    state = engine.changed(state, check_suite_stage='explore', check_suite_completed=False,
                           check_suite_patch_hash=None, check_result_refs=[])
    return engine.output(state, 'check_suite')


async def reproduce(engine, state):
    plan = frozen_plan(engine, state)
    state, result = await record_baseline(engine, state, trial=state.trial + 1)
    trial = state.trial + 1
    state = engine.changed(state, trial=trial,
                           failure_signatures=state.failure_signatures + [result['failure_signature']])
    if trial < 3:
        return engine.output(state, 'prelude')
    stable = baseline_reproduced(state, plan,
        lambda ref: engine.artifacts.exists(state.scope_id, state.run_id, ref),
        lambda ref: engine.get(state, ref))
    if stable and state.mode == 'repair':
        if state.repair_source and (not engine.source_acceptance_ready(state)
                                   or not engine.source_rule_trials_passed(state)):
            return engine.output(engine.changed(state, phase=Phase.FINALIZE, outcome=Outcome.INCONCLUSIVE,
                error='非 GUI 来源问题尚缺独立来源验收证据'), 'prelude')
        return engine.output(engine.changed(state, phase=Phase.DIAGNOSE, reproduced=True), 'prelude')
    outcome = Outcome.BUG_CONFIRMED if stable else Outcome.INCONCLUSIVE
    return engine.output(engine.changed(state, phase=Phase.FINALIZE, reproduced=stable, outcome=outcome,
        error=None if stable else '命令故障无法在三次试验中的至少两次稳定复现'), 'prelude')


async def verification_result(engine, state, kind):
    plan = frozen_plan(engine, state)
    if kind != 'regression':
        target = plan['target'] if kind == 'original' else kind
        return await execute(engine, state, target, operation='verify.command.' + kind)
    results = [await execute(engine, state, target, operation='verify.command.regression.' + target)
               for target in plan['regression_targets']]
    return {'passed': all(result['passed'] for result in results),
            'exit_code': 0 if all(result['passed'] for result in results) else 1,
            'output': '\n'.join(result['output'] for result in results), 'command_results': results,
            'execution_plan_hash': digest(plan)}


def goal_result(engine, state, refs):
    if state.check_suite_stage == 'verify':
        records = [engine.get(state, ref) for ref in state.validation_refs]
        records = [item for item in records if item.get('kind') == 'original'
                   and item.get('patch_hash') == state.patch_hash]
        if not records:
            raise ValueError('非 GUI 用户目标缺少当前补丁的独立验证结果')
        ref = records[-1]['artifact_ref']
    else:
        if not state.baseline_validation_refs:
            raise ValueError('非 GUI 用户目标缺少真实失败基线')
        ref = state.baseline_validation_refs[0]
    result = engine.get(state, ref)
    plan = frozen_plan(engine, state)
    if not command_body(result, plan, plan['target']):
        raise ValueError('非 GUI 用户目标命令证据无效')
    refs.append(ref)
    actual = result['output'] or '冻结命令退出码为 0'
    return 'pass' if result['passed'] else 'fail', actual, list(dict.fromkeys(refs))
