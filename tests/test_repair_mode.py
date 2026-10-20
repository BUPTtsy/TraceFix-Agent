from types import SimpleNamespace

import pytest

from tracefix.runtime import repair_mode
from tracefix.runtime.continuation import continuation_state
from tracefix.runtime.contracts import (FindingRepairSource, Guidance, Outcome,
                                       RunState, RunStatus, reduce_state)
from tracefix.runtime.guidance import retarget_state


def resolve(**overrides):
    return repair_mode.resolve_repair_mode(**{
        'run_mode': 'repair', 'requested': None, 'project_config': {},
        'model': SimpleNamespace(supports_tool_executor=True), 'repair_source': None,
        **overrides})


@pytest.fixture(autouse=True)
def clean_mode_environment(monkeypatch):
    monkeypatch.delenv('TRACEFIX_REPAIR_MODE', raising=False)


def test_default_uses_hybrid_after_repair_loop_is_available():
    assert resolve() == ('hybrid', 'hybrid', None)


def test_request_project_environment_default_priority(monkeypatch):
    monkeypatch.setattr(repair_mode, 'HYBRID_AVAILABLE', True)
    monkeypatch.setenv('TRACEFIX_REPAIR_MODE', 'pipeline')
    assert resolve()[0] == 'pipeline'
    assert resolve(project_config={'repair_mode': 'hybrid'}) == ('hybrid', 'hybrid', None)
    assert resolve(requested='pipeline', project_config={'repair_mode': 'hybrid'}) == (
        'pipeline', 'pipeline', None)
    # 优先级高的显式请求不读取低优先级的无效值。
    monkeypatch.setenv('TRACEFIX_REPAIR_MODE', 'invalid')
    assert resolve(requested='pipeline')[2] is None


@pytest.mark.parametrize('source', ['request', 'project', 'environment'])
@pytest.mark.parametrize('value', ['', 'Hybrid', 'unknown', 1, True])
def test_invalid_selected_mode_is_rejected(monkeypatch, source, value):
    options = {}
    if source == 'request':
        options['requested'] = value
    elif source == 'project':
        options['project_config'] = {'repair_mode': value}
    else:
        monkeypatch.setenv('TRACEFIX_REPAIR_MODE', str(value))
    with pytest.raises(ValueError, match='hybrid'):
        resolve(**options)


def test_fallback_reasons_follow_documented_order(monkeypatch):
    no_tools = SimpleNamespace(supports_tool_executor=False)
    source = object()
    monkeypatch.setattr(repair_mode, 'HYBRID_AVAILABLE', False)
    monkeypatch.setattr(repair_mode, 'FINDING_REPAIR_AVAILABLE', False)
    assert resolve(run_mode='test', model=no_tools, repair_source=source)[2] == 'test_mode'
    assert resolve(model=no_tools, repair_source=source)[2] == 'not_implemented'
    monkeypatch.setattr(repair_mode, 'HYBRID_AVAILABLE', True)
    assert resolve(model=no_tools, repair_source=source)[2] == 'model_without_tools'
    assert resolve(repair_source=source)[2] == 'finding_repair_pending'
    monkeypatch.setattr(repair_mode, 'FINDING_REPAIR_AVAILABLE', True)
    assert resolve(repair_source=source) == ('hybrid', 'hybrid', None)


def test_explicit_pipeline_never_counts_as_fallback():
    assert resolve(requested='pipeline', run_mode='test', model=None,
                   repair_source=object()) == ('pipeline', 'pipeline', None)


def test_old_checkpoint_defaults_pipeline_and_actual_mode_is_immutable():
    previous = RunState(scope_id='demo', goal='修复持久化', url='http://app:3000')
    assert repair_mode.repair_mode_fields(previous) == {
        'repair_mode': 'pipeline', 'repair_mode_requested': None, 'repair_mode_reason': None}
    with pytest.raises(ValueError, match='不可变更'):
        reduce_state(previous, previous.revision, repair_mode='hybrid')


def test_continuation_and_retarget_inherit_frozen_mode(monkeypatch):
    previous = RunState(scope_id='demo', goal='修复持久化', url='http://app:3000', mode='repair',
        repair_mode='pipeline', repair_mode_requested='hybrid', repair_mode_reason='not_implemented',
        run_status=RunStatus.FAILED, outcome=Outcome.INFRA_FAILURE)
    monkeypatch.setattr(repair_mode, 'HYBRID_AVAILABLE', True)
    monkeypatch.setenv('TRACEFIX_REPAIR_MODE', 'hybrid')
    continued = continuation_state(previous, '继续检查', [])
    assert repair_mode.repair_mode_fields(continued) == repair_mode.repair_mode_fields(previous)
    previous.run_status = RunStatus.RUNNING
    entry = Guidance(run_id=previous.run_id, scope_id=previous.scope_id,
        level='retarget', text='修复另外的持久化问题', confirmed_at=1.0)
    child = retarget_state(previous, entry)
    assert repair_mode.repair_mode_fields(child) == repair_mode.repair_mode_fields(previous)


@pytest.mark.parametrize('source_mode', ['test', 'repair'])
def test_finding_new_repair_resolves_test_source_but_inherits_repair_source(monkeypatch, source_mode):
    from tracefix.service import finding

    source = RunState(scope_id='demo', goal='检查持久化', url='http://app:3000', mode=source_mode,
        repair_mode='pipeline', repair_mode_requested='pipeline')
    repair_source = FindingRepairSource.model_construct(source_run_id=source.run_id,
        source_scope_id=source.scope_id, source_repo_id='org/repo')
    child = source.model_copy(update={'run_id': 'repair_child', 'mode': 'repair', 'repair_source': repair_source})
    monkeypatch.setattr(finding, 'create_finding_repair_run', lambda *args, **kwargs: child)
    request = SimpleNamespace(source_run_id=source.run_id, source_finding_ids=['finding_1'],
        repo_id='demo', run_id=child.run_id, goal='修复持久化', additional_rule_ids=[],
        execution=SimpleNamespace(url=None, repair_mode='hybrid'),
        repository_snapshot=SimpleNamespace(repository_config={'owner': 'org', 'repo': 'repo'}))
    session = SimpleNamespace(documents=None, artifacts=None,
        store=SimpleNamespace(load=lambda *args: source))
    result = finding.finding_repair_state(request, session, None, SimpleNamespace(finding=lambda _: None))
    if source_mode == 'test':
        assert (result.repair_mode, result.repair_mode_requested, result.repair_mode_reason) == ('hybrid', 'hybrid', None)
    else:
        assert repair_mode.repair_mode_fields(result) == repair_mode.repair_mode_fields(source)


async def test_report_provenance_and_service_result_expose_frozen_mode(tmp_path):
    from tracefix.runtime.smoke import make_engine
    from tracefix.service.results import serialize_result

    engine, state = make_engine(tmp_path, bugfree=True)
    state.repair_mode, state.repair_mode_requested, state.repair_mode_reason = resolve()
    state.outcome = Outcome.INCONCLUSIVE
    engine.store.save(state)
    await engine.finalize(state, None)
    finished = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(finished, finished.report_ref)
    expected = repair_mode.repair_mode_fields(state)
    assert {key: report[key] for key in expected} == expected
    assert {key: report['provenance'][key] for key in expected} == expected
    result = serialize_result(finished, engine.artifacts, task_id='task_1', repo_id=state.scope_id)
    assert {key: result[key] for key in expected} == expected


async def test_cli_creation_records_resolution_event(engine_session):
    session, engine, _ = engine_session
    session.args.repair_mode = 'pipeline'
    session.mode = 'repair'
    session.goal = '修复持久化'
    created = await session.create_agent(start=False)
    assert repair_mode.repair_mode_fields(created) == {
        'repair_mode': 'pipeline', 'repair_mode_requested': 'pipeline', 'repair_mode_reason': None}
    events = session.store.trace(created.run_id, created.scope_id)
    resolved = [event for event in events if event['type'] == 'repair_mode.resolved']
    assert len(resolved) == 1
    assert resolved[0]['payload'] == repair_mode.repair_mode_fields(created)


async def test_patch_submission_preserves_protected_path_error_for_model(tmp_path):
    from tracefix.runtime.contracts import FileEdit, PatchProposal, Phase, digest
    from tracefix.runtime.smoke import make_engine
    from tracefix.tools.handlers import build_runtime_tools

    engine, state = make_engine(tmp_path)
    state.phase = Phase.DIAGNOSE
    state.evidence_refs = ['evidence_1']
    engine.workspace.protected_paths = ['src/value.ts']
    original = engine.workspace.read('src/value.ts')
    proposal = PatchProposal(summary='修复持久化', evidence_refs=state.evidence_refs, edits=[
        FileEdit(path='src/value.ts', before_hash=digest(original.encode()),
                 content='export const persisted = true;\n')])
    engine.store.save(state)
    pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
    result = await pipeline.execute('propose_patch', proposal.model_dump(mode='json'), 'protected-patch')
    assert result.is_error
    assert result.error['error_code'] == 'protected_path'
    assert result.result['error_code'] == 'protected_path'
    assert engine.workspace.read('src/value.ts') == original
    assert pipeline.submission_value is None
