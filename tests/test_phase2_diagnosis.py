import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from tracefix.model.gateway import ModelResult
from tracefix.runtime.contracts import FileEdit, PatchProposal, Phase, digest
from tracefix.runtime.diagnosis import (CandidatePath, DiagnosisDraft, DiagnosisReport, Hypothesis,
    binding_from_observation, symptom_query, validate_report)
from tracefix.runtime.smoke import make_engine
from tracefix.runtime.worker import ReadOnlyWorker, SubtaskResult, SubtaskSpec


async def diagnosis_engine(tmp_path):
    engine, state = make_engine(tmp_path)
    state.goal = '核对 persisted 状态与刷新后页面'
    engine.store.save(state)
    state = type(state)(**(await engine.prepare(state, None))['data'])
    state.phase = Phase.DIAGNOSE
    state.reproduced = True
    from tracefix.runtime.contracts import BrowserAction
    state.observation_ref = await engine.capture(state, await engine.browser.action(BrowserAction(kind='observe')))
    state.evidence_refs = [state.observation_ref]
    engine.store.save(state)
    return engine, state


async def test_structured_diagnosis_precedes_patch_and_keeps_versioned_fragments(tmp_path, monkeypatch):
    engine, state = await diagnosis_engine(tmp_path)
    monkeypatch.setenv('TRACEFIX_AGENT_MODE', 'single')
    calls = []

    class Model:
        supports_structured_diagnosis = True

        async def generate(self, schema, context, **options):
            calls.append(schema)
            if schema is DiagnosisDraft:
                card = context['source_fragments'][0]
                assert card['content_version']
                assert card['end_line'] - card['start_line'] < 160
                binding = context['symptom_binding']
                assert binding['source_map']['status'] == 'unavailable'
                value = DiagnosisDraft(
                    hypotheses=[Hypothesis(id='persistence', summary='持久化状态源码候选',
                        support_refs=[state.observation_ref, card['artifact_ref']],
                        candidate_paths=[CandidatePath(path=card['path'], symbol='persisted',
                            content_version=card['content_version'])],
                        prediction='将 persisted 切换后观察 checkbox checked',
                        minimal_probe='读取同版 value.ts 并核对刷新后断言', status='supported')])
            else:
                assert schema is PatchProposal
                assert context['diagnosis']['hypotheses'][0]['status'] == 'supported'
                value = PatchProposal(summary='修复持久化', evidence_refs=[state.observation_ref],
                    edits=[FileEdit(path='src/value.ts', before_hash=digest(engine.workspace.path('src/value.ts').read_bytes()),
                                    content='export const persisted = true;\n')])
            return ModelResult(value, {}, 'test-structured-model', 'stop')

    engine.model = Model()
    output = await engine.diagnose(state, None)
    saved = type(state)(**output['data'])
    assert calls == [DiagnosisDraft, PatchProposal]
    assert saved.phase == Phase.PATCH
    report = engine.get(saved, saved.hypothesis_refs[-2])
    assert report['generated_by'] == 'model'
    assert report['binding']['observation_ref'] == state.observation_ref


async def test_diagnosis_rejects_unknown_refs_and_marks_stale_source(tmp_path):
    engine, state = await diagnosis_engine(tmp_path)
    binding = binding_from_observation(state, engine.get(state, state.observation_ref))
    report = DiagnosisReport(binding=binding, source_version=state.source_manifest,
        hypotheses=[Hypothesis(id='candidate', summary='候选源码链', support_refs=['invented.json'],
            prediction='区分字段映射', minimal_probe='只读当前源码', status='supported',
            candidate_paths=[CandidatePath(path='src/value.ts', content_version='old')])])
    with pytest.raises(ValueError, match='不存在'):
        validate_report(report, evidence_refs=state.evidence_refs, allowed_files=['src/value.ts'],
            source_manifest=state.source_manifest, environment_digest=state.environment_digest,
            binding=binding, content_versions={'src/value.ts': 'current'})
    report.hypotheses[0].support_refs = [state.observation_ref]
    validate_report(report, evidence_refs=state.evidence_refs, allowed_files=['src/value.ts'],
        source_manifest=state.source_manifest, environment_digest=state.environment_digest,
        binding=binding, content_versions={'src/value.ts': 'current'})
    assert report.hypotheses[0].status == 'unresolved'
    assert report.hypotheses[0].candidate_paths[0].status == 'stale'


async def test_worker_same_model_and_capabilities_are_enforced(tmp_path, monkeypatch):
    engine, state = await diagnosis_engine(tmp_path)
    monkeypatch.delenv('TRACEFIX_AGENT_MODE', raising=False)
    before_ref = state.observation_ref

    class ReadModel(BaseChatModel):
        requests: int = 0
        registered: set[str] = Field(default_factory=set)

        @property
        def _llm_type(self):
            return 'same-main-model'

        def bind_tools(self, tools, **options):
            self.registered = {tool.name for tool in tools}
            return self.bind(tools=tools)

        def _generate(self, messages, **options):
            pytest.fail('调查必须异步执行')

        async def _agenerate(self, messages, stop=None, run_manager=None, **options):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'Read', {'path': 'src/value.ts'}
            else:
                assert messages[-1].name == 'Read' and messages[-1].tool_call_id == 'investigate-1'
                name = 'SubtaskResult'
                arguments = SubtaskResult(summary='只读源码已核对', evidence_refs=[state.observation_ref],
                    files=['src/value.ts'], suggested_experiments=[], unresolved=[],
                    worker_generation=state.revision, source_manifest=state.source_manifest).model_dump()
            message = AIMessage(content='', tool_calls=[dict(name=name, args=arguments,
                id=f'investigate-{self.requests}', type='tool_call')])
            return ChatResult(generations=[ChatGeneration(message=message)])

    class WrongModel:
        async def generate(self, *arguments, **options):
            pytest.fail('调查不能使用另一个 Worker model')

    engine.model = ReadModel()
    engine.worker_model = WrongModel()
    async def old_executor(*arguments, **options):
        pytest.fail('调查不能调用旧 engine.model_call executor')
    monkeypatch.setattr(engine, 'model_call', old_executor)
    spec = SubtaskSpec(state.goal, 'code-explorer', state.revision, ('src/value.ts',), (state.observation_ref,))
    result = await ReadOnlyWorker(engine).run(state, spec)
    assert result.worker_id
    assert result.worker_generation == spec.generation
    assert result.source_manifest == state.source_manifest
    assert engine.model.requests == 2
    assert engine.model.registered == {'Read', 'Grep', 'Glob', 'write_todos', 'SubtaskResult'}
    assert result.usage['model_calls'] == state.budget.model_calls == 2
    assert state.observation_ref == before_ref
    assert state.patch_ref is None


async def test_worker_single_mode_blocks_before_model_dispatch(tmp_path, monkeypatch):
    engine, state = await diagnosis_engine(tmp_path)
    monkeypatch.setenv('TRACEFIX_AGENT_MODE', 'single')
    spec = SubtaskSpec(state.goal, 'code-explorer', state.revision, ('src/value.ts',), (state.observation_ref,))
    with pytest.raises(PermissionError, match='单 Agent'):
        await ReadOnlyWorker(engine).run(state, spec)
    assert state.budget.subtasks == 0


def test_symptom_query_extracts_real_mcp_network_api_anchor():
    query = symptom_query('完成后刷新', [], {
        'network': '[POST] http://app:3000/api/tasks/1 => [404] Not Found'})
    assert '/api/tasks/1' in query
    assert '/api/tasks' in query.splitlines()
