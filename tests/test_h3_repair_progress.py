"""H3 脚本轨迹回放测试；不声称替代真实模型轨迹的离线阈值校准。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.runtime.contracts import CheckJudgement, FinishRepair, Phase, ReproductionPlan
from tracefix.runtime.recovery import RecoveryCause, RecoveryController
from tracefix.runtime.repair_progress import (
    WINDOW, advance_repair, assess_repair, calibrate_repair_traces, normalize_source, normalized_diff_fingerprint,
    observe_repair, repair_summary, replay_repair_events,
)
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import ToolPipeline, ToolRegistry, ToolSpec
from tracefix.tools.test_runner import RunTestsInput
from tracefix.tools.handlers import build_runtime_tools


FIXTURES = Path(__file__).parent / 'fixtures' / 'repair_progress'


def snapshot(data):
    return SimpleNamespace(repair_mode='hybrid', loop_evidence={'repair': data})


def test_payload(diff='A', failures=('a',), passed=1, **extra):
    return {'target': 'unit', 'normalized_diff_fingerprint': diff,
            'result': {'failure_signatures': list(failures),
                       'summary': {'passed': passed, 'unparsed': False}, **extra}}


# 辅助函数不是测试用例。
test_payload.__test__ = False


@pytest.mark.parametrize(('name', 'signal'), [
    ('diff_oscillation', 'repair_diff_oscillation'),
    ('signature_stagnation', 'repair_signature_stagnation'),
    ('candidate_repeated', 'repair_candidate_repeated'),
])
def test_scripted_repetitions_warn_before_signal_and_allow_one_correction(name, signal):
    rows = (FIXTURES / f'scripted_{name}.jsonl').read_text(encoding='utf-8').splitlines()
    replay = replay_repair_events(rows)
    observations = replay['assessments']
    warning = next(index for index, item in enumerate(observations)
                   if any(value['kind'] == signal for value in item['warnings']))
    hit = next(index for index, item in enumerate(observations)
               if any(value['kind'] == signal for value in item['signals']))
    assert warning < hit
    assert replay['incomplete'] == []
    # 模块返回信号但不终止；与引擎相同的 RecoveryController 仅允许一次纠偏。
    recovery = RecoveryController()
    decision = recovery.decide(RecoveryCause.REPEATED_NO_PROGRESS, [], episode_id='episode')
    assert decision.can_continue and decision.action.value == 'correct_strategy'
    episodes = recovery.record([], decision, status='continued')
    data, progressed = advance_repair(replay['repair'], 'correction')
    assert not progressed
    restored = json.loads(json.dumps(data))
    assert assess_repair(snapshot(restored))['signals']
    second = recovery.decide(RecoveryCause.REPEATED_NO_PROGRESS, episodes, episode_id='episode')
    assert not second.can_continue


def test_monotonic_improvement_and_new_information_reset_stagnation():
    data = {}
    for index in range(20):
        data, progressed = advance_repair(data, 'run_tests', **test_payload(str(index), passed=index))
        assert progressed
        assert assess_repair(snapshot(data))['signals'] == []
    assert len(data['history']) == WINDOW
    for _ in range(4):
        data, _ = advance_repair(data, 'run_tests', **test_payload('last', passed=19))
    assert assess_repair(snapshot(data))['signals']
    data, progressed = advance_repair(data, 'read', path='src/new.ts')
    assert progressed and not assess_repair(snapshot(data))['signals']
    data, progressed = advance_repair(data, 'read', path='src/new.ts')
    assert not progressed
    data, progressed = advance_repair(data, 'run_tests', **{**test_payload(), 'target': 'static'})
    assert progressed and not assess_repair(snapshot(data))['signals']


def test_true_subset_progress_and_regression_recovery_does_not_repeat_progress():
    data, _ = advance_repair({}, 'run_tests', **test_payload(failures=('a', 'b'), passed=0))
    data, progressed = advance_repair(data, 'run_tests', **test_payload(failures=('a',), passed=0))
    assert progressed and 'failure_subset' in data['last_progress']['reasons']
    data, progressed = advance_repair(data, 'run_tests', **test_payload(failures=('a', 'b'), passed=0))
    assert not progressed
    data, progressed = advance_repair(data, 'run_tests', **test_payload(failures=('a',), passed=0))
    assert not progressed


def test_verify_target_and_regression_passes_are_separate_progress():
    data, _ = advance_repair({}, 'verify', result={'kind': 'original', 'passed': False},
                             passed_counts={'target': 0, 'regression': 0})
    data, progressed = advance_repair(data, 'verify', result={'kind': 'original', 'passed': False},
                                      passed_counts={'target': 1, 'regression': 0})
    assert progressed
    data, progressed = advance_repair(data, 'verify', result={'kind': 'original', 'passed': False},
                                      passed_counts={'target': 1, 'regression': 1})
    assert progressed
    assert data['frontier']['verify:original']['passed_counts'] == {'target': 1, 'regression': 1}


def test_verify_partial_assertion_improvement_counts_before_overall_pass():
    data, _ = advance_repair({}, 'verify', result={'kind': 'original', 'passed': False,
        'assertions': [{'passed': False}, {'passed': False}]})
    data, progressed = advance_repair(data, 'verify', result={'kind': 'original', 'passed': False,
        'assertions': [{'passed': True}, {'passed': False}]})
    assert progressed
    assert data['frontier']['verify:original']['passed_counts'] == {'target': 1}


@pytest.mark.parametrize(('path', 'before', 'after'), [
    ('a.ts', 'const value = 1; // note\n', '/* changed note */ const value=1;'),
    ('a.py', 'def f():\n    return 1  # note\n', '# note\ndef f( ):\n  return 1\n'),
])
def test_lexical_normalization_ignores_comments_and_formatting(path, before, after):
    assert normalize_source(before, path) == normalize_source(after, path)


@pytest.mark.parametrize(('path', 'before', 'after'), [
    ('a.ts', 'const s = "a b // c";', 'const s = "ab // c";'),
    ('a.ts', 'const s = `hello /* string */`;', 'const s = `hello string`;'),
    ('a.ts', 'const s = `x${`a // string`}b`;', 'const s = `x${`a // stringb`}b`;'),
    ('a.ts', r'const s = /a\/\/ b/;', r'const s = /a\/\/b/;'),
    ('a.py', 's = "a # string b"\n', 's = "a # stringb"\n'),
    ('a.ts', 'const a = b + +c;', 'const a = b++c;'),
])
def test_lexical_normalization_preserves_string_regex_and_token_meaning(path, before, after):
    assert normalize_source(before, path) != normalize_source(after, path)


def test_normalized_diff_is_cumulative_and_survives_materialization(tmp_path):
    engine, state = make_engine(tmp_path)
    workspace = engine.workspace
    state.patch_base_commit = workspace.head()
    relative = next(path for path in workspace.require_repository_snapshot()['files'] if path.endswith('.ts'))
    original = workspace.path(relative).read_bytes()
    staged = original + b'\nconst repairMarker = "a b";\n'
    workspace._staged_files[relative] = staged
    fingerprint = normalized_diff_fingerprint(workspace, state)
    workspace.path(relative).write_bytes(staged)
    workspace._staged_files.clear()
    assert normalized_diff_fingerprint(workspace, state) == fingerprint
    workspace._staged_files[relative] = staged + b'// formatting only\n'
    assert normalized_diff_fingerprint(workspace, state) == fingerprint
    workspace._staged_files[relative] = staged.replace(b'"a b"', b'"ab"')
    assert normalized_diff_fingerprint(workspace, state) != fingerprint


def test_observe_persists_state_and_evidence_and_recovers_same_window(tmp_path):
    engine, state = make_engine(tmp_path)
    state.repair_mode = 'hybrid'
    state.phase = Phase.DIAGNOSE
    state.loop_evidence = {'repair_session': {'history_ref': 'history'}}
    engine.store.save(state)
    result = {'kind': 'repair_self_test', 'run_id': state.run_id, 'scope_id': state.scope_id,
              'source_manifest': state.source_manifest, 'result_ref': 'self-test',
              'failure_signatures': ['a'], 'summary': {'passed': 1, 'unparsed': False},
              'normalized_diff_fingerprint': 'A'}
    for _ in range(5):
        assessment = observe_repair(engine, state, 'run_tests', result=result, target='unit')
    assert assessment['signals']
    assert state.phase == Phase.DIAGNOSE and state.outcome is None
    restored = engine.store.load(state.run_id, state.scope_id)
    assert restored.loop_evidence['repair_session'] == {'history_ref': 'history'}
    assert restored.loop_evidence['repair']['self_test_refs'] == ['self-test']
    assert assess_repair(restored) == assessment
    observe_repair(engine, restored, 'correction')
    assert assess_repair(restored)['signals']
    observe_repair(engine, restored, 'read', path='src/new.ts')
    assert restored.loop_no_progress_steps == 0
    assert not assess_repair(restored)['signals']
    report = repair_summary(restored)
    assert report['run_tests_count'] == 5 and len(report['corrections']) == 1
    assert report['calibration']['real_trajectory_calibrated'] is False


def test_pipeline_does_not_observe_and_cross_run_refs_are_not_whitelisted(tmp_path):
    engine, state = make_engine(tmp_path)
    before = state.model_dump(mode='json')
    observe_repair(engine, state, 'read', path='src/new.ts')
    assert state.model_dump(mode='json') == before
    state.repair_mode = 'hybrid'
    engine.store.save(state)
    observe_repair(engine, state, 'run_tests', **test_payload(result_ref='foreign', run_id='another'))
    assert 'foreign' not in state.loop_evidence['repair'].get('self_test_refs', [])


@pytest.mark.parametrize(('schema', 'phase'), [
    (CheckJudgement, Phase.EXPLORE),
    (CheckJudgement, Phase.VERIFY),
    (CheckJudgement, Phase.DIAGNOSE),
    (ReproductionPlan, Phase.EXPLORE),
])
async def test_readonly_inspection_does_not_change_repair_progress(tmp_path, schema, phase):
    engine, state = make_engine(tmp_path)
    state.repair_mode, state.phase = 'hybrid', phase
    engine.store.save(state)
    tools = build_runtime_tools(engine, state, schema)
    pipeline = tools.pipeline()
    before = state.model_dump(mode='json')
    for index in range(engine.LOOP_NO_PROGRESS_LIMIT + 1):
        result = await pipeline.execute('Read', {
            'file_path': str(engine.workspace.root / 'src/value.ts')}, f'inspect-{index}')
        assert not result.is_error
    assert state.model_dump(mode='json') == before
    assert engine.store.load(state.run_id, state.scope_id).loop_evidence == state.loop_evidence


async def test_repair_session_reads_still_record_new_information_and_repetition(tmp_path):
    engine, state = make_engine(tmp_path)
    state.repair_mode, state.phase = 'hybrid', Phase.DIAGNOSE
    engine.store.save(state)
    pipeline = build_runtime_tools(engine, state, FinishRepair, {'repair_mode': 'hybrid'}).pipeline()
    for index in range(3):
        result = await pipeline.execute('Read', {
            'file_path': str(engine.workspace.root / 'src/value.ts')}, f'repair-{index}')
        assert not result.is_error
    repair = state.loop_evidence['repair']
    assert repair['read_files'] == ['src/value.ts']
    assert [item['semantic_progress'] for item in repair['history']] == [True, False, False]
    assert state.loop_no_progress_steps == 2


@pytest.mark.asyncio
async def test_tool_pipeline_audit_replay_matches_online_and_deduplicates(tmp_path):
    engine, state = make_engine(tmp_path)
    state.repair_mode = 'hybrid'
    engine.store.save(state)
    async def handler(arguments, call_id):
        result = {'target': arguments.target, 'failure_signatures': ['a'], 'summary': {'passed': 1},
                  'normalized_diff_fingerprint': 'A'}
        observe_repair(engine, state, 'run_tests', result=result, target=arguments.target)
        return result
    pipeline = ToolPipeline(ToolRegistry([ToolSpec('RunTests', '脚本化测试回执', RunTestsInput,
        parallel_safe=False)]), {'RunTests': handler}, Phase.DIAGNOSE,
        emit=lambda kind, payload: engine.event(state, kind, payload))
    for index in range(5):
        await pipeline.execute('RunTests', {'target': 'unit'}, str(index))
    records = engine.store.trace(state.run_id, state.scope_id)
    records.append(records[-1])
    replay = replay_repair_events(json.dumps(row) for row in records)
    assert replay['repair'] == state.loop_evidence['repair']
    assert replay['incomplete'] == []


def test_scripted_calibration_statistics_keep_real_trajectories_separate():
    traces = [{'source': 'scripted', 'expected_loop': True,
               'records': path.read_text(encoding='utf-8').splitlines()} for path in FIXTURES.glob('*.jsonl')]
    traces.append({'source': 'scripted', 'expected_loop': False, 'records': [
        {'event': 'run_tests', 'payload': test_payload(str(index), passed=index)} for index in range(8)]})
    counts = calibrate_repair_traces(traces)
    assert counts['true_positive'] == 3 and counts['true_negative'] == 1
    assert counts['false_positive_rate'] == counts['false_negative_rate'] == 0
    assert counts['sources'] == {'scripted': 4, 'real': 0}
    with pytest.raises(ValueError, match='source'):
        calibrate_repair_traces([{'records': [], 'expected_loop': False}])


def test_operation_id_submission_audit_and_missing_normalization_are_explicit():
    records = []
    for index in range(3):
        records.extend([
            {'type': 'tool.started', 'payload': {'operation_id': str(index),
                'intent': {'tool_name': 'finish_repair', 'arguments': {}}}},
            {'type': 'tool.completed', 'payload': {'operation_id': str(index),
                'receipt': {'name': 'finish_repair', 'call_id': f'call-{index}', 'isError': True,
                    'error': {'error_code': 'FAILED_CANDIDATE_REPEATED'},
                    'result': {'candidate_fingerprint': 'A'}}}},
        ])
    result = replay_repair_events(records)
    assert any(item['kind'] == 'repair_candidate_repeated' for item in result['assessments'][-1]['signals'])
    assert len(result['incomplete']) == 3
    assert not any(item['kind'] == 'repair_diff_oscillation' for item in result['assessments'][-1]['signals'])


def test_unparsed_outputs_do_not_fabricate_failure_subset_or_stagnation():
    data, _ = advance_repair({}, 'run_tests', **test_payload(failures=('a',), passed=0))
    for _ in range(8):
        data, progressed = advance_repair(data, 'run_tests', target='unit',
            result={'summary': {'passed': 0, 'unparsed': True}, 'normalized_diff_fingerprint': 'A'})
        assert not progressed
    assert not assess_repair(snapshot(data))['signals']
