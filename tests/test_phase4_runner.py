import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from evals.config import EvaluationConfig, GROUPS
from evals.oracle_bridge import OracleBridge
from evals.runner import CommandAdapter, FourCellRunner, SessionAdapter, instrument_memory_calls
from tracefix.knowledge.memory import MemoryLibrary
from tracefix.runtime.contracts import RunState
from tracefix.storage.store import MemoryStore
from tracefix.storage.artifacts import Artifacts


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def _source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'src').mkdir()
    (source / 'src' / 'app.ts').write_text('export const ok = true;\n', encoding='utf-8')
    _git(source, 'init', '-q')
    _git(source, 'add', '.')
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=fixture', '-c',
                    'user.email=fixture@localhost', 'commit', '-qm', 'base'], check=True)
    return source


def _config(tmp_path, source):
    source_revision = _git(source, 'rev-parse', 'HEAD')
    skills = tmp_path / 'skills'
    skills.mkdir(exist_ok=True)
    (skills / 'skill.txt').write_text('public', encoding='utf-8')
    spec = tmp_path / 'spec.json'
    spec.write_text('{"goal":"public"}', encoding='utf-8')
    protocol = tmp_path / 'protocol.json'
    protocol.write_text('{"reset":true}', encoding='utf-8')
    experience = tmp_path / 'experience.json'
    experience.write_text('{"items":[]}', encoding='utf-8')
    profile = tmp_path / 'profile.json'
    profile.write_text('{"project":"demo"}', encoding='utf-8')
    return EvaluationConfig(case_id='B01', family='mutation', source_root=source,
                            source_revision=source_revision, profile=profile, spec=spec,
                            public_protocol=protocol, skills_root=skills,
                            initial_experience=experience, goal='repair', model='fixture-model',
                            seed=7, tools_hash='tools', recovery_rules_hash='recovery',
                            held_out=True)


def test_group_directories_are_fresh_and_switches_reach_adapter(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)

    def adapter(**kwargs):
        group = kwargs['group']
        env = kwargs['env']
        assert env['TRACEFIX_AGENT_MODE'] == group.agent_mode
        assert env['TRACEFIX_CROSS_RUN_MEMORY'] == ('on' if group.cross_run_memory else 'off')
        return {'status': 'completed', 'outcome': 'INCONCLUSIVE',
                'trace': [{'event': 'model_call', 'model_agent_id': 'one'}],
                'usage': {'prompt_tokens': 2}}

    rows = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_all()
    assert len(rows) == 4
    assert {row['configuration'] for row in rows} == {group.name for group in GROUPS}
    assert all(row['evidence_kind'] == 'inconclusive' for row in rows)
    for row in rows:
        group = tmp_path / 'runs' / 'B01' / row['configuration']
        assert all((group / name).is_dir() for name in ('data', 'artifacts', 'cache', 'browser', 'session'))
        assert row['binding']['source_revision'] == config.source_revision


def test_existing_group_is_never_reused(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)
    runner = FourCellRunner(config, tmp_path / 'runs', adapter=lambda **_: {})
    runner.run_group(GROUPS[0])
    with pytest.raises(FileExistsError):
        runner.run_group(GROUPS[0])


def test_single_trace_with_delegation_is_inconclusive(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)

    def adapter(**_):
        return {'trace': [{'event': 'model_call', 'model_agent_id': 'one'},
                          {'event': 'delegation', 'model_agent_id': 'two'}]}

    row = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_group(GROUPS[0])
    assert row['evidence_kind'] == 'inconclusive'
    assert row['trace_summary']['trace_proven'] is False


def test_off_switch_reaches_existing_memory_methods_and_keeps_l1(tmp_path, monkeypatch):
    memory = MemoryLibrary(tmp_path / 'memory.sqlite3')
    trace = []
    instrument_memory_calls(memory, trace)
    monkeypatch.setenv('TRACEFIX_CROSS_RUN_MEMORY', 'off')
    memory.note('scope', 'run', {'kind': 'finding', 'text': 'current observation'})
    memory.save_job_memory('scope', 'job', {'text': 'must not persist'},
                           source_run_id='run', source_manifest='source')
    assert memory.job_memory('scope', 'job', 'source') == []
    assert [item['event'] for item in trace] == ['memory.note', 'memory.save_job_memory', 'memory.job_memory']
    assert memory.working_memory('scope', 'run')['finding'][0]['text'] == 'current observation'


def test_runner_single_off_sets_environment_used_by_real_memory_store(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)
    calls = []

    def adapter(**kwargs):
        memory = MemoryLibrary(Path(kwargs['root']) / 'data' / 'memory.sqlite3')
        instrument_memory_calls(memory, calls)
        memory.note('scope', 'run', {'kind': 'finding', 'text': 'L1'})
        memory.save_job_memory('scope', 'job', {'text': 'L2'}, source_run_id='old', source_manifest='source')
        assert memory.job_memory('scope', 'job', 'source') == []
        return {'status': 'completed', 'outcome': 'INCONCLUSIVE',
                'trace': [{'event': 'model_call', 'model_agent_id': 'one', 'response_audited': True}, *calls],
                'trace_origin': 'runtime_audit', 'gui_real': True}

    row = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_group(GROUPS[0])
    assert row['evidence_kind'] == 'inconclusive'
    assert row['trace_summary']['trace_proven'] is True
    assert row['trace_summary']['oracle_isolation_proven'] is False
    assert [item['event'] for item in calls] == [
        'memory.note', 'memory.save_job_memory', 'memory.job_memory']


def test_command_adapter_parses_batch_result_line(tmp_path):
    result = CommandAdapter()(command=(sys.executable, '-c',
        "print('progress'); print('BATCH_RESULT: {\\\"outcome\\\":\\\"FIX_VERIFIED\\\"}')"),
        cwd=str(tmp_path), env=os.environ.copy(), timeout=10, binding={}, config=None,
        group=None)
    assert result['outcome'] == 'FIX_VERIFIED'
    assert result['internal_success'] is True


def test_fixture_oracle_never_promotes_audited_trace_to_real(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)
    private = tmp_path / 'evaluator'
    private.mkdir()
    script = private / 'oracle.py'
    script.write_text('print(\'{"case_id":"B01","passed":true}\')\n', encoding='utf-8')

    class FixtureBoundary:
        def verify(self, private_root, private_files):
            return {'kind': 'fixture_only', 'real_isolation': False}

    oracle = OracleBridge(private, (sys.executable, str(script)), script,
                          boundary=FixtureBoundary())

    def adapter(**kwargs):
        candidate = kwargs['root'] / 'candidate.patch'
        candidate.write_bytes(b'fixture candidate\n')
        from tracefix.runtime.contracts import digest
        return {'candidate_patch': str(candidate),
                'verify_candidate': lambda: digest(candidate.read_bytes()),
                'trace': [{'event': 'model_call', 'model_agent_id': 'fixture', 'response_audited': True}],
                'trace_origin': 'runtime_audit', 'gui_real': True}

    runner = FourCellRunner(config, tmp_path / 'runs', adapter=adapter, oracle=oracle)
    row = runner.run_group(GROUPS[0])
    assert row['oracle_passed'] is True
    assert row['evidence_kind'] == 'inconclusive'
    assert row['trace_summary']['trace_proven'] is True
    assert row['trace_summary']['oracle_isolation_proven'] is False
    assert not list(runner.root.rglob('row.json'))


def _session_fixture(monkeypatch, *, failure=None, cleanup_failure=None, retarget=False,
                     actual_runtime=False):
    from tracefix.cli.main import Session
    closed = []
    sessions = []
    gateways = []

    class Resource:
        def __init__(self, name):
            self.name = name

        async def close(self):
            closed.append(self.name)
            if self.name == cleanup_failure:
                raise RuntimeError('cleanup fixture')

    async def checkpoint_close():
        closed.append('checkpoint')

    async def ensure_runtime(session):
        assert session.runtime_stack is not None
        session.store = MemoryStore()
        session.runtime_stack.callback(lambda: closed.append('store'))
        session.runtime_stack.push_async_callback(checkpoint_close)

    if actual_runtime:
        class Store(MemoryStore):
            def __init__(self, dsn):
                assert dsn == 'fixture'
                super().__init__()

            def setup(self):
                closed.append('store-setup')
                if failure == 'store_setup':
                    raise RuntimeError('store setup fixture')

            def close(self):
                closed.append('store')

        @contextlib.asynccontextmanager
        async def checkpointer(dsn):
            assert dsn == 'fixture'
            closed.append('checkpoint-open')
            try:
                if failure == 'checkpoint_setup':
                    raise RuntimeError('checkpoint setup fixture')
                yield object()
            finally:
                closed.append('checkpoint')

        monkeypatch.setattr('tracefix.cli.main.PostgresStore', Store)
        monkeypatch.setattr('tracefix.storage.checkpoints.postgres_checkpointer', checkpointer)

    def bind(session, state, profile, workspace, source):
        assert profile is None and workspace is None and source is None
        session.engine = SimpleNamespace(memory=MemoryLibrary(Path(session.args.data) / 'memory.sqlite3'),
            browser=Resource('browser-' + state.run_id), runner=Resource('runner-' + state.run_id),
            workspace=SimpleNamespace(diff=lambda _base: ''), model=None, worker_model=None,
            current_run=state.run_id)
        session.run_id = state.run_id

    async def create(session):
        sessions.append(session)
        await session.ensure_runtime()
        if failure == 'before_bind':
            raise RuntimeError('create fixture')
        state = RunState(scope_id=session.args.project, run_id='parent',
                         goal='repair', url='http://app:3000')
        session.store.save(state)
        session.bind(state, None, None, None)
        if failure == 'after_bind':
            raise RuntimeError('bind fixture')

        async def drive():
            engine = session.engine
            gateways.append(engine.model.teacher)
            assert engine.model.student is None
            assert engine.worker_model is None
            assert 'TRACEFIX_STUDENT_URL' not in os.environ
            assert 'TRACEFIX_WORKER_TEXT_MODEL' not in os.environ
            if failure == 'drive':
                raise RuntimeError('drive fixture')
            if failure == 'timeout':
                await asyncio.sleep(30)
            engine.memory.note(state.scope_id, state.run_id, {'text': 'current public finding'})
            response_ref = session.artifacts.put(state.scope_id, state.run_id,
                {'model': engine.model.teacher.text_model,
                 'response': {'http_status': 200, 'body': {'model': 'reported-model',
                    'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}}})
            session.store.event(state, 'model.response.persisted',
                                {'http_status': 200, 'response_ref': response_ref})
            session.store.event(state, 'model.called', {'model_revision': engine.model.teacher.text_model})
            if retarget:
                child = state.model_copy(update={'run_id': 'retarget', 'parent_run_id': state.run_id})
                session.store.save(child)
                session.bind(child, None, None, None)

        session.task = asyncio.create_task(drive())

    if not actual_runtime:
        monkeypatch.setattr(Session, 'ensure_runtime', ensure_runtime)
    monkeypatch.setattr(Session, 'bind', bind)
    monkeypatch.setattr(Session, 'create', create)
    return sessions, gateways, closed


def _session_call(tmp_path, config, env):
    root = tmp_path / 'group'
    (root / 'data').mkdir(parents=True)
    return SessionAdapter()(source=config.source_root, root=root, env=env,
                            config=config, binding={'group_id': 'single-memory-off'})


def test_session_uses_actual_gateway_configuration_and_closes_resources(tmp_path, monkeypatch):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False, model='configured-text',
                     vision_model='configured-vision', thinking='enabled', tool_mode='json')
    sessions, gateways, closed = _session_fixture(monkeypatch)
    monkeypatch.setenv('TRACEFIX_TEXT_MODEL', 'ambient-text')
    monkeypatch.setenv('TRACEFIX_THINKING', 'disabled')
    env = {'TRACEFIX_API_KEY': 'fixture-key', 'TRACEFIX_DATABASE_URL': 'fixture-dsn',
           'TRACEFIX_STUDENT_URL': 'https://student.invalid', 'TRACEFIX_WORKER_TEXT_MODEL': 'other-model',
           'TRACEFIX_CROSS_RUN_MEMORY': 'off'}
    result = _session_call(tmp_path, config, env)
    gateway = gateways[0]
    assert (gateway.text_model, gateway.vision_model, gateway.thinking, gateway.tool_mode) == (
        'configured-text', 'configured-vision', 'enabled', 'json')
    assert closed == ['browser-parent', 'runner-parent', 'checkpoint', 'store']
    assert sessions[0].runtime_stack is not None
    assert result['initial_experience'] == {'status': 'disabled', 'loaded': False}
    assert result['gui_real'] is False
    assert result['trace'][-1]['model_agent_id'] == 'parent'
    assert result['trace'][-1]['response_audited'] is True
    assert result['usage']['input_tokens'] == 3
    assert result['usage']['cost_usd'] is None
    assert os.environ['TRACEFIX_TEXT_MODEL'] == 'ambient-text'
    assert os.environ['TRACEFIX_THINKING'] == 'disabled'


@pytest.mark.parametrize('failure', ['before_bind', 'after_bind', 'drive', 'timeout'])
def test_session_closes_checkpoint_and_partial_runtime_on_failure(tmp_path, monkeypatch, failure):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False,
                     timeout_seconds=0.1 if failure == 'timeout' else 10)
    sessions, _, closed = _session_fixture(monkeypatch, failure=failure)
    expected = TimeoutError if failure == 'timeout' else RuntimeError
    with pytest.raises(expected):
        _session_call(tmp_path, config, {'TRACEFIX_API_KEY': 'fixture',
                      'TRACEFIX_DATABASE_URL': 'fixture', 'TRACEFIX_CROSS_RUN_MEMORY': 'off'})
    assert closed[-2:] == ['checkpoint', 'store']
    if failure != 'before_bind':
        assert closed[:2] == ['browser-parent', 'runner-parent']
    if sessions[0].task:
        assert sessions[0].task.done()


def test_cleanup_failure_still_closes_other_runtime_and_database(tmp_path, monkeypatch):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False)
    _, _, closed = _session_fixture(monkeypatch, cleanup_failure='browser-parent')
    with pytest.raises(RuntimeError, match='资源清理失败'):
        _session_call(tmp_path, config, {'TRACEFIX_API_KEY': 'fixture',
                      'TRACEFIX_DATABASE_URL': 'fixture', 'TRACEFIX_CROSS_RUN_MEMORY': 'off'})
    assert closed == ['browser-parent', 'runner-parent', 'checkpoint', 'store']


def test_retarget_engines_are_all_closed(tmp_path, monkeypatch):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False)
    _, _, closed = _session_fixture(monkeypatch, retarget=True)
    _session_call(tmp_path, config, {'TRACEFIX_API_KEY': 'fixture',
                  'TRACEFIX_DATABASE_URL': 'fixture', 'TRACEFIX_CROSS_RUN_MEMORY': 'off'})
    assert closed == ['browser-retarget', 'runner-retarget', 'browser-parent', 'runner-parent',
                      'checkpoint', 'store']


def _model_response(store, artifacts, state, requested='same-model', reported='same-model', usage=None):
    request_ref = artifacts.put(state.scope_id, state.run_id,
        {'request': {'json': {'model': requested}}})
    response_ref = artifacts.put(state.scope_id, state.run_id,
        {'model': requested, 'response': {'http_status': 200,
            'body': {'model': reported, 'usage': usage}}})
    store.event(state, 'model.request.persisted', {'request_ref': request_ref})
    store.event(state, 'model.response.persisted',
                {'http_status': 200, 'request_ref': request_ref, 'response_ref': response_ref})
    store.event(state, 'model.called', {'model_revision': reported})
    return request_ref, response_ref


def test_child_agent_count_uses_child_model_response_trace(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    parent = RunState(scope_id='scope', run_id='parent', goal='repair', url='http://app:3000')
    child = parent.model_copy(update={'run_id': 'child', 'parent_run_id': parent.run_id})
    store.save(parent)
    store.save(child)
    _model_response(store, artifacts, parent)
    store.event(parent, 'subtask.started', {'child_run_id': child.run_id})
    trace = SessionAdapter._collect_trace(store, parent, artifacts)
    result = {'trace': trace, 'trace_origin': 'runtime_audit', 'gui_real': True}
    assert FourCellRunner._trace_summary(result, GROUPS[2])['trace_proven'] is False
    _model_response(store, artifacts, child)
    result['trace'] = SessionAdapter._collect_trace(store, parent, artifacts)
    summary = FourCellRunner._trace_summary(result, GROUPS[2])
    assert summary['trace_proven'] is True
    assert summary['model_agent_ids'] == ['child', 'parent']


def test_child_trace_with_wrong_parent_never_counts_model():
    store = MemoryStore()
    parent = RunState(scope_id='scope', run_id='parent', goal='repair', url='http://app:3000')
    child = parent.model_copy(update={'run_id': 'child', 'parent_run_id': 'other'})
    store.save(parent)
    store.save(child)
    store.event(parent, 'subtask.started', {'child_run_id': child.run_id})
    store.event(child, 'model.called', {'model_revision': 'model'})
    trace = SessionAdapter._collect_trace(store, parent)
    assert trace[-1]['event'] == 'child_trace.unavailable'
    assert not any(item['event'] == 'model_call' for item in trace)


def test_nonempty_initial_experience_and_unsupported_effort_are_unavailable(tmp_path):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False)
    config.initial_experience.write_text('{"items":[{"content":"public experience"}]}', encoding='utf-8')
    unavailable = _session_call(tmp_path, config, {'TRACEFIX_CROSS_RUN_MEMORY': 'on'})
    assert unavailable['status'] == 'unavailable'
    assert unavailable['initial_experience']['loaded'] is False
    config = replace(config, effort='high')
    effort = SessionAdapter()(source=source, root=tmp_path / 'unused', env={}, config=config, binding={})
    assert effort['status'] == 'unavailable'


def test_config_held_out_cannot_be_bypassed_by_adapter_flag(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)
    result = _session_call(tmp_path, config, {})
    assert result['status'] == 'isolation_error'


@pytest.mark.parametrize('failure', [None, 'store_setup', 'checkpoint_setup'])
def test_real_ensure_runtime_registers_and_releases_resources(tmp_path, monkeypatch, failure):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False)
    sessions, _, closed = _session_fixture(monkeypatch, failure=failure, actual_runtime=True)
    env = {'TRACEFIX_API_KEY': 'fixture', 'TRACEFIX_DATABASE_URL': 'fixture',
           'TRACEFIX_CROSS_RUN_MEMORY': 'off'}
    if failure:
        with pytest.raises(RuntimeError, match='setup fixture'):
            _session_call(tmp_path, config, env)
    else:
        _session_call(tmp_path, config, env)
        assert sessions[0].saver is not None
    if failure == 'store_setup':
        assert closed == ['store-setup', 'store']
    elif failure == 'checkpoint_setup':
        assert closed == ['store-setup', 'checkpoint-open', 'checkpoint', 'store']
    else:
        assert closed == ['store-setup', 'checkpoint-open', 'browser-parent', 'runner-parent',
                          'checkpoint', 'store']


def test_response_aliases_and_model_revision_remain_separate(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = RunState(scope_id='scope', run_id='run', goal='repair', url='http://app:3000')
    store.save(state)
    _model_response(store, artifacts, state, 'deepseek-v4-flash', 'deepseek-flash')
    trace = SessionAdapter._collect_trace(store, state, artifacts)
    call = next(item for item in trace if item['event'] == 'model_call')
    assert call['response_audited'] is True
    assert call['model_revision'] == 'deepseek-flash'
    assert call['response_audits'][0]['requested_model'] == 'deepseek-v4-flash'
    assert call['response_audits'][0]['reported_model'] == 'deepseek-flash'
    summary = FourCellRunner._trace_summary(
        {'trace': trace, 'trace_origin': 'runtime_audit', 'gui_real': True}, GROUPS[0])
    assert summary['trace_proven'] is True
    assert summary['model_calls'][0]['response_audits'] == call['response_audits']


def test_missing_model_response_cannot_prove_child_agent(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    parent = RunState(scope_id='scope', run_id='parent', goal='repair', url='http://app:3000')
    child = parent.model_copy(update={'run_id': 'child', 'parent_run_id': parent.run_id})
    store.save(parent)
    store.save(child)
    _model_response(store, artifacts, parent)
    store.event(parent, 'subtask.started', {'child_run_id': child.run_id})
    store.event(child, 'model.response.persisted',
                {'http_status': 200, 'response_ref': '0' * 64 + '.json'})
    store.event(child, 'model.called', {'model_revision': 'label-only'})
    trace = SessionAdapter._collect_trace(store, parent, artifacts)
    child_call = next(item for item in trace if item.get('event') == 'model_call'
                      and item['model_agent_id'] == child.run_id)
    assert child_call['response_audited'] is False
    assert child_call['response_audits'][0]['reported_model'] is None
    summary = FourCellRunner._trace_summary(
        {'trace': trace, 'trace_origin': 'runtime_audit', 'gui_real': True}, GROUPS[2])
    assert summary['trace_proven'] is False
    assert summary['model_agent_ids'] == ['parent']


def test_unknown_model_names_are_not_filled_from_labels(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = RunState(scope_id='scope', run_id='run', goal='repair', url='http://app:3000')
    store.save(state)
    _model_response(store, artifacts, state, None, None)
    call = SessionAdapter._collect_trace(store, state, artifacts)[-1]
    audit = call['response_audits'][0]
    assert audit['requested_model'] is None
    assert audit['reported_model'] is None
    assert call['model_revision'] is None


def test_usage_uses_provider_fields_and_keeps_missing_cost_unknown(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = RunState(scope_id='scope', run_id='run', goal='repair', url='http://app:3000')
    store.save(state)
    _, response_ref = _model_response(store, artifacts, state, usage={'prompt_tokens': 100,
        'completion_tokens': 20, 'total_tokens': 120, 'prompt_cache_hit_tokens': 70})
    store.event(state, 'model.response.persisted', {'http_status': 200, 'response_ref': response_ref})
    trace = SessionAdapter._collect_trace(store, state, artifacts)
    usage = SessionAdapter._usage(trace, attempted_calls=1)
    assert usage['attempted_calls'] == 1
    assert usage['input_tokens'] == 100
    assert usage['output_tokens'] == 20
    assert usage['total_tokens'] == 120
    assert usage['cache_read_tokens'] == 70
    assert usage['cache_write_tokens'] is None
    assert usage['cost_usd'] is None
    assert usage['coverage']['cost_usd'] == {'known_sum': None, 'known_calls': 0,
        'unknown_calls': 1, 'coverage': 0.0, 'complete': False, 'total': None}


def test_usage_partial_child_and_missing_request_report_known_sums(tmp_path):
    store = MemoryStore()
    artifacts = Artifacts(tmp_path / 'artifacts')
    parent = RunState(scope_id='scope', run_id='parent', goal='repair', url='http://app:3000')
    child = parent.model_copy(update={'run_id': 'child', 'parent_run_id': parent.run_id})
    store.save(parent)
    store.save(child)
    _model_response(store, artifacts, parent, usage={'prompt_tokens': 5, 'completion_tokens': 2,
        'total_tokens': 7, 'cost_usd': 0, 'prompt_tokens_details': {'cached_tokens': 3}})
    store.event(parent, 'subtask.started', {'child_run_id': child.run_id})
    _model_response(store, artifacts, child, usage={'completion_tokens': 4,
        'cost_usd': False, 'cache_write_tokens': -1})
    store.event(child, 'model.request.persisted', {'request_ref': 'missing-response'})
    trace = SessionAdapter._collect_trace(store, parent, artifacts)
    usage = SessionAdapter._usage(trace, attempted_calls=3)
    assert usage['input_tokens'] is None
    assert usage['output_tokens'] is None
    assert usage['cost_usd'] is None
    assert usage['coverage']['input_tokens']['known_sum'] == 5
    assert usage['coverage']['output_tokens']['known_sum'] == 6
    assert usage['coverage']['output_tokens']['coverage'] == 2 / 3
    assert usage['coverage']['cache_read_tokens']['known_sum'] == 3
    assert usage['coverage']['cost_usd']['known_sum'] == 0
    assert usage['coverage']['cost_usd']['known_calls'] == 1
    assert usage['coverage']['cache_write_tokens']['known_calls'] == 0


def test_unavailable_initial_experience_is_retained_in_evaluator_row(tmp_path):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), held_out=False)
    config.initial_experience.write_text('{"items":[{"content":"public"}]}', encoding='utf-8')
    row = FourCellRunner(config, tmp_path / 'runs').run_group(GROUPS[1])
    assert row['status'] == 'unavailable'
    assert row['initial_experience']['loaded'] is False
    assert row['error'] == row['initial_experience']['reason']
    assert row['oracle_passed'] is None
    assert row['evidence_kind'] == 'inconclusive'


def test_injected_defect_is_frozen_in_group_head_and_source_hashes_match(tmp_path):
    source = _source(tmp_path)
    config = replace(_config(tmp_path, source), mutation_file='src/app.ts',
                     mutation_before='true', mutation_after='false')

    def adapter(**kwargs):
        isolated = kwargs['source']
        assert 'false' in _git(isolated, 'show', 'HEAD:src/app.ts')
        assert _git(isolated, 'status', '--short') == ''
        return {'status': 'completed', 'outcome': 'INCONCLUSIVE'}

    rows = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_all()
    assert len({row['binding']['source_hash'] for row in rows}) == 1


def test_child_browser_tasks_and_thread_workers_finish_before_parent_resources_close():
    closed = []
    cancel_worker = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def worker():
        assert cancel_worker.wait(5)
        closed.append('worker-child')

    future = executor.submit(worker)

    async def execute():
        child_started = asyncio.Event()

        async def child():
            child_started.set()
            try:
                await asyncio.Future()
            finally:
                closed.append('browser-child')

        child_task = asyncio.create_task(child())
        await child_started.wait()

        class Runtime:
            scheduler = SimpleNamespace(executor=executor)
            _browser_tasks = {'parent': {child_task}}

            def cancel(self, run_id):
                assert run_id == 'parent'
                child_task.cancel()
                cancel_worker.set()

            def close(self):
                executor.shutdown(wait=False, cancel_futures=True)

        class Resource:
            async def close(self):
                assert child_task.done()
                assert future.done()
                closed.append('parent')

        engine = SimpleNamespace(subagent_runtime=Runtime(), current_run='parent',
                                 browser=Resource(), runner=Resource())
        session = SimpleNamespace(task=None, engine=engine, run_id='parent')
        await SessionAdapter._close_session(session, [engine])

    try:
        asyncio.run(execute())
    finally:
        cancel_worker.set()
        executor.shutdown(wait=True, cancel_futures=True)
    assert set(closed[:2]) == {'worker-child', 'browser-child'}
    assert closed[2:] == ['parent', 'parent']
