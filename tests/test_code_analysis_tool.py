import json
from pathlib import Path

import pytest

from tracefix.config import Project
from tracefix.rules.analyzers import analyze_source
from tracefix.rules.codegraph import CODEGRAPH_VERSION, CodeGraphError, RepositoryCodeGraph
from tracefix.runtime.contracts import CheckJudgement, Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools.evidence import attach_source_evidence_tools
from tracefix.tools.handlers import build_runtime_tools


@pytest.mark.parametrize('mode,phase', [('test', Phase.EXPLORE), ('repair', Phase.VERIFY)])
async def test_code_analysis_reads_overlay_and_records_current_check_evidence(tmp_path, mode, phase):
    engine, state = make_engine(tmp_path / mode)
    state.mode, state.phase, state.patch_hash = mode, phase, 'current-patch'
    engine.store.save(state)
    source = engine.workspace.root / 'src/value.ts'
    baseline = source.read_bytes()
    candidate = 'export const persisted = true;\n'
    engine.workspace.stage('src/value.ts', candidate)
    context = {'available_evidence_refs': ['existing-evidence']}
    previous_context = context
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    attach_source_evidence_tools(engine, state, CheckJudgement, lambda: context, runtime)
    context = {'available_evidence_refs': ['current-evidence']}

    receipt = await runtime.pipeline().execute('CodeAnalyze', {
        'paths': [str(source)], 'check': 'regex', 'config': {'pattern': 'persisted = true'},
    }, 'analyze-overlay')

    assert not receipt.is_error
    assert receipt.result['status'] == 'fail'
    assert receipt.result['findings'][0]['match'] == 'persisted = true'
    assert receipt.result['source_manifest'] == state.source_manifest
    assert receipt.result['content_versions'] == {'src/value.ts': digest(candidate.encode())}
    reference = receipt.result['artifact_ref']
    evidence = engine.get(state, reference)
    assert evidence['tool'] == 'code.analyze'
    assert evidence['source_manifest'] == state.source_manifest
    assert evidence['patch_hash'] == 'current-patch'
    assert reference in engine.store.load(state.run_id, state.scope_id).evidence_refs
    assert context['available_evidence_refs'] == ['current-evidence', reference]
    assert context['evidence_refs'] == context['available_evidence_refs']
    assert previous_context == {'available_evidence_refs': ['existing-evidence']}
    assert source.read_bytes() == baseline


async def test_code_analysis_rejects_outside_paths_and_preserves_analyzer_fallback(tmp_path, monkeypatch):
    engine, state = make_engine(tmp_path / 'run')
    state.phase = Phase.EXPLORE
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    pipeline = runtime.pipeline()
    denied = await pipeline.execute('CodeAnalyze', {'paths': [str(tmp_path / 'outside.ts')]}, 'denied')
    assert denied.is_error and denied.error['executed'] is False

    def unavailable(arguments, root):
        raise CodeGraphError('codegraph unavailable')

    monkeypatch.setattr('tracefix.rules.codegraph.run_cli', unavailable)
    result = await pipeline.execute('CodeAnalyze', {
        'paths': [str(engine.workspace.root / 'src/value.ts')],
    }, 'analyzer-failed')
    assert not result.is_error
    assert result.result['status'] == 'error'
    assert result.result['fallback_required'] is True
    assert 'codegraph unavailable' in result.result['errors'][0]['message']
    assert engine.get(state, result.result['artifact_ref'])['fallback_required'] is True


@pytest.fixture
def codegraph_cli(monkeypatch):
    calls = []

    def invoke(arguments, root):
        calls.append((arguments, root))
        operation = arguments[0]
        if operation == '--version':
            return CODEGRAPH_VERSION
        if operation == 'init':
            database = root / '.codegraph' / 'codegraph.db'
            database.parent.mkdir(parents=True)
            database.write_bytes(b'fake-codegraph-db')
        if operation == 'status':
            return json.dumps({'initialized': True, 'version': CODEGRAPH_VERSION,
                'index': {'state': 'complete'}, 'pendingChanges': {}, 'languages': ['typescript']})
        if operation == 'query':
            return json.dumps([{'node': {'filePath': 'src/value.ts', 'startLine': 1,
                'kind': 'constant', 'name': 'persisted'}}])
        if operation == 'context':
            return json.dumps({'nodes': [{'filePath': 'src/value.ts'}, {'filePath': 'src/related.ts'}],
                'edges': [{'source': 'value', 'target': 'related', 'kind': 'calls'}]})
        return ''

    monkeypatch.setattr('tracefix.rules.codegraph.run_cli', invoke)
    return calls


def test_codegraph_databases_are_repository_scoped_and_reused(tmp_path, codegraph_cli):
    first = Project(id='first', repo_id='owner/repo', root=tmp_path / 'first')
    second = Project(id='second', repo_id='owner/repo', root=tmp_path / 'second')
    index = RepositoryCodeGraph(first, tmp_path / 'data')
    other = RepositoryCodeGraph(second, tmp_path / 'data')
    assert index.database != other.database
    sources = {'src/value.ts': 'export const persisted = false;\n',
               'src/related.ts': 'export function related() {}\n',
               'tsconfig.json': '{"compilerOptions":{"baseUrl":"."}}'}
    initial = index.analyze(sources, selected=['src/value.ts'], binding={'run_id': 'first'})
    assert initial['status'] == 'pass'
    assert index.database.is_file()
    assert (index.root / 'src/related.ts').is_file()
    assert (index.root / 'tsconfig.json').read_bytes() == sources['tsconfig.json'].encode()
    recreated = RepositoryCodeGraph(first, tmp_path / 'data')
    changed = recreated.analyze({'src/value.ts': 'export const persisted = true;\n'},
                               selected=['src/value.ts'], binding={'run_id': 'second'})
    assert changed['status'] == 'pass'
    assert changed['codegraph']['database_path'] == initial['codegraph']['database_path']
    assert changed['codegraph']['source_version'] != initial['codegraph']['source_version']
    assert changed['codegraph']['binding'] == {'run_id': 'second'}
    assert not (index.root / 'src/related.ts').exists()
    assert [arguments[0] for arguments, _ in codegraph_cli].count('init') == 1
    assert [arguments[0] for arguments, _ in codegraph_cli].count('sync') == 1
    assert not (first.root / '.codegraph').exists()


async def test_codegraph_tool_indexes_complete_overlay_but_filters_symbol_results(tmp_path, codegraph_cli):
    engine, state = make_engine(tmp_path / 'run')
    state.phase = Phase.EXPLORE
    related = engine.workspace.root / 'src/related.ts'
    related.write_text('export function related() {}\n', encoding='utf-8')
    candidate = 'export const persisted = true;\n'
    engine.workspace.stage('src/value.ts', candidate)
    runtime = build_runtime_tools(engine, state, CheckJudgement)
    receipt = await runtime.pipeline().execute('CodeAnalyze', {
        'paths': [str(engine.workspace.root / 'src/value.ts')],
        'config': {'project_root': str(tmp_path / 'forbidden'), 'codegraph_command': 'not-allowed'},
    }, 'graph-overlay')
    assert not receipt.is_error
    result = receipt.result
    assert result['status'] == 'pass' and result['parser'] == 'codegraph'
    assert result['content_versions'] == {'src/value.ts': digest(candidate.encode())}
    assert 'src/related.ts' in result['graph_content_versions']
    root = Path(result['codegraph']['database_path']).parent.parent
    assert (root / 'src/value.ts').read_bytes() == candidate.encode()
    assert (root / 'src/related.ts').read_bytes() == related.read_bytes()
    assert result['codegraph']['binding']['run_id'] == state.run_id
    assert all(item['path'] == 'src/value.ts' for item in result['items'])
    context = await runtime.pipeline().execute('CodeAnalyze', {
        'paths': [str(engine.workspace.root / 'src/value.ts')], 'check': 'context',
        'config': {'query': 'persisted'},
    }, 'graph-context')
    assert context.result['graph']['edges'][0]['kind'] == 'calls'
    assert context.result['codegraph']['database_path'] == result['codegraph']['database_path']


@pytest.mark.parametrize('failure', ['invalid_json', 'incomplete', 'wrong_version', 'timeout'])
def test_codegraph_failure_cannot_be_reported_as_pass(tmp_path, monkeypatch, codegraph_cli, failure):
    from tracefix.rules import codegraph
    normal = codegraph.run_cli

    def broken(arguments, root):
        if failure == 'wrong_version' and arguments[0] == '--version':
            return '0.0.0'
        if failure == 'timeout':
            raise CodeGraphError('CodeGraph query timeout')
        if arguments[0] == 'status':
            return 'not json' if failure == 'invalid_json' else json.dumps({'initialized': True, 'index': {'state': 'indexing'}})
        return normal(arguments, root)

    monkeypatch.setattr(codegraph, 'run_cli', broken)
    index = RepositoryCodeGraph(Project(id='repo', repo_id='repo', root=tmp_path / 'repo'), tmp_path / 'data')
    result = index.analyze({'src/value.ts': 'export const persisted = true;'}, selected=['src/value.ts'])
    assert result['status'] == 'error' and result['fallback_required']
    assert result['errors']


def test_codegraph_success_does_not_hide_syntax_or_accessibility_findings(tmp_path, codegraph_cli):
    index = RepositoryCodeGraph(Project(id='repo', repo_id='repo', root=tmp_path / 'repo'), tmp_path / 'data')
    invalid = analyze_source({'src/value.ts': 'export function ( {'},
        {'analyzer': 'codegraph', 'check': 'syntax'}, index=index)
    assert invalid['status'] == 'fail' and invalid['findings']
    unnamed = analyze_source({'src/value.tsx': 'export const App = () => <button />;'},
        {'analyzer': 'codegraph', 'check': 'a11y_name'}, index=index)
    assert unnamed['status'] == 'fail' and unnamed['findings'][0]['tag'] == 'button'


def test_codegraph_rejects_unsafe_source_paths_before_invoking_cli(tmp_path, codegraph_cli):
    index = RepositoryCodeGraph(Project(id='repo', repo_id='repo', root=tmp_path / 'repo'), tmp_path / 'data')
    for path in ['../outside.ts', 'C:/outside.ts', '.codegraph/overwrite.ts', '.env/key.ts']:
        result = index.analyze({path: 'source'}, selected=[path])
        assert result['status'] == 'error' and result['fallback_required']
    assert not codegraph_cli


def test_repository_registration_provisions_a_persistent_graph(tmp_path, codegraph_cli):
    from tracefix.repository_registry import RepositoryRegistry
    engine, _ = make_engine(tmp_path / 'run')
    root = engine.scopes.projects[engine.context.active_scope].root
    registry = RepositoryRegistry(tmp_path / 'projects.yaml', data_root=tmp_path / 'data')
    project = registry.create_legacy('registered', str(root), 'ci')
    index = RepositoryCodeGraph(project, tmp_path / 'data')
    assert index.database.is_file()
    health = json.loads((index.directory / 'health.json').read_text(encoding='utf-8'))
    assert health['status'] == 'pass'
    assert health['codegraph']['binding'] == {'scope_id': 'registered', 'view': 'repository'}
    assert (index.root / 'src/value.ts').read_bytes() == (root / 'src/value.ts').read_bytes()


def test_concurrent_run_views_keep_sync_and_query_under_one_repository_lock(tmp_path, monkeypatch, codegraph_cli):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from tracefix.rules import codegraph
    normal = codegraph.run_cli

    def query_view(arguments, root):
        if arguments[0] == 'query':
            time.sleep(0.02)
            name = (root / 'src/value.ts').read_text(encoding='utf-8')
            return json.dumps([{'node': {'filePath': 'src/value.ts', 'name': name}}])
        return normal(arguments, root)

    monkeypatch.setattr(codegraph, 'run_cli', query_view)
    project = Project(id='repo', repo_id='repo', root=tmp_path / 'repo')

    def run_view(name):
        index = RepositoryCodeGraph(project, tmp_path / 'data')
        return index.analyze({'src/value.ts': name}, selected=['src/value.ts'], binding={'run_id': name})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run_view, ['first-view', 'second-view']))
    assert results[0]['codegraph']['database_path'] == results[1]['codegraph']['database_path']
    for result, name in zip(results, ['first-view', 'second-view']):
        assert result['status'] == 'pass'
        assert result['items'][0]['name'] == result['codegraph']['binding']['run_id'] == name
