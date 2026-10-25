from types import SimpleNamespace

from tracefix.knowledge.retrieval import Retriever
from tracefix.runtime.engine import Engine
from tracefix.runtime.smoke import make_engine


async def test_memory_store_retrieval_degradation_is_safe_and_emitted_once(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)

    assert await engine.retriever.retrieve('diagnose', state.source_manifest) == []
    engine.retriever.candidate(state, '{"outcome":"unknown"}', 'failure_recipe')

    engine.warn_retrieval_degraded(state)
    engine.warn_retrieval_degraded(state)
    events = [event for event in engine.store.trace(state.run_id, state.scope_id)
              if event['type'] == 'retrieval.degraded']
    assert len(events) == 1
    assert events[0]['payload']['reason'] == 'postgres_unavailable'
    assert events[0]['payload']['disabled_capabilities'] == ['repair_memory_read', 'repair_memory_write']
    assert '代码检索' not in events[0]['payload']['message'] and '项目文档检索' not in events[0]['payload']['message']


def test_postgres_retriever_does_not_report_degradation(tmp_path):
    engine, _ = make_engine(tmp_path)
    retriever = Retriever(SimpleNamespace(conn=object()), engine.scopes, engine.context)
    assert retriever.degradation() is None


async def test_run_warns_before_model_and_restart_deduplicates_persisted_event(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    state.mode = 'test'
    await engine.run(state)
    events = engine.store.trace(state.run_id, state.scope_id)
    warnings = [event for event in events if event['type'] == 'retrieval.degraded']
    assert len(warnings) == 1
    assert warnings[0]['seq'] < next(event['seq'] for event in events if event['type'] == 'model.started')
    assert warnings[0]['payload']['reason'] == 'postgres_unavailable'
    restarted = Engine(engine.store, engine.artifacts, engine.scopes, engine.context,
                       engine.profile, engine.workspace, engine.runner, engine.browser,
                       engine.model, engine.retriever, engine.source, engine.graph.checkpointer)
    restarted.warn_retrieval_degraded(state)
    assert len([event for event in engine.store.trace(state.run_id, state.scope_id)
                if event['type'] == 'retrieval.degraded']) == 1
