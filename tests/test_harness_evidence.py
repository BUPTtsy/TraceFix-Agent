from types import SimpleNamespace

import pytest

from tracefix.runtime.harness import HarnessRuntime


class FakeEngine:
    def __init__(self, artifacts, commands=None):
        self.artifacts = artifacts
        self.profile = SimpleNamespace(commands=commands or {})
        self.saved = {}
        self.next_ref = 0

    def get(self, _state, ref):
        return self.artifacts[ref]

    def put(self, _state, value, ext='json', *, name='数据记录'):
        self.next_ref += 1
        ref = f'generated-{self.next_ref}.{ext}'
        self.saved[ref] = value
        return ref

    def verification_context(self, _state):
        return {'plan_hash': 'plan'}


def make_runtime(engine):
    runtime = object.__new__(HarnessRuntime)
    runtime.session = lambda _state: SimpleNamespace(session_id='session', input_commit='input')
    runtime._identity = lambda _state: {
        'scope_id': 'scope', 'job_id': 'job', 'session_id': 'session', 'run_id': 'run'}
    return runtime


def make_state(refs):
    return SimpleNamespace(
        scope_id='scope', harness_job_id='job', harness_session_id='session', run_id='run',
        source_manifest='source', patch_hash='patch', environment_digest='env',
        test_spec_hash='spec', check_plan_hash='plan', validation_refs=refs, evidence_refs=[])


def test_original_receipt_uses_executed_argv():
    engine = FakeEngine({
        'validation': {'kind': 'original', 'artifact_ref': 'result'},
        'result': {'exit_code': 0, 'executed_argv': ['pytest', '-q']},
    })
    record = make_runtime(engine).verification_record(
        engine, make_state(['validation']), candidate_commit='candidate', passed=True,
        acceptance_refs=['original'])
    assert [item.command for item in record.commands] == [['pytest', '-q']]
    evidence = engine.saved[record.commands[0].evidence_ref]
    assert evidence['command'] == ['pytest', '-q']
    assert evidence['receipt_refs'] == ['validation', 'result']


def test_regression_receipt_expands_actual_subcommands():
    engine = FakeEngine({
        'validation': {'kind': 'regression', 'artifact_ref': 'result'},
        'result': {'exit_code': 0, 'command_results': [
            {'exit_code': 0, 'executed_argv': ['pytest', 'unit']},
            {'exit_code': 1, 'executed_argv': ['pytest', 'integration']},
        ]},
    })
    record = make_runtime(engine).verification_record(
        engine, make_state(['validation']), candidate_commit='candidate', passed=False,
        acceptance_refs=['original'])
    assert [item.command for item in record.commands] == [
        ['pytest', 'unit'], ['pytest', 'integration']]
    assert [item.exit_code for item in record.commands] == [0, 1]
    for item in record.commands:
        evidence = engine.saved[item.evidence_ref]
        assert evidence['receipt_refs'][:2] == ['validation', 'result']
        raw = engine.saved[evidence['receipt_refs'][2]]
        assert raw['executed_argv'] == item.command
        assert raw['exit_code'] == item.exit_code


def test_original_without_executed_argv_is_rejected():
    engine = FakeEngine({
        'validation': {'kind': 'original', 'artifact_ref': 'result'},
        'result': {'exit_code': 0},
    })
    with pytest.raises(PermissionError, match='实际执行命令参数'):
        make_runtime(engine).verification_record(
            engine, make_state(['validation']), candidate_commit='candidate', passed=True,
            acceptance_refs=['original'])
