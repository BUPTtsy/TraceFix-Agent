import json
from pathlib import Path

import pytest

from tracefix.config import Project
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.knowledge.scope import ScopeResolver
from tracefix.tools.chat import MAX_FILE_BYTES, MAX_SCAN_FILES, build_chat_tools
from tracefix.runtime.contracts import Phase, digest
from tracefix.tools.core import ToolProtocolError, estimate_tokens
from tracefix.storage.artifacts import Artifacts


@pytest.fixture
def chat_tools(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    scopes = ScopeResolver({'alpha': Project(id='alpha', repo_id='alpha-repo', root=root)})
    ctx = scopes.context('alpha')
    library = DocumentLibrary(tmp_path / 'console.sqlite3')
    artifacts = Artifacts(tmp_path / 'artifacts')
    events = []
    pipeline = build_chat_tools(scopes, ctx, library, artifacts, 'chat_session',
                                emit=lambda kind, payload: events.append((kind, payload)))
    return root, scopes, ctx, library, artifacts, pipeline, events


async def test_chat_tools_offer_only_scoped_read_tools_and_preserve_result_content(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    source = root / 'answer.py'
    source.write_text('answer = 42\n', encoding='utf-8')
    assert {spec.name for spec in pipeline.registry.visible(Phase.DIAGNOSE)} == {'Read', 'Grep', 'Glob', 'DocumentSearch'}
    assert all(spec.side_effect == 'read' and not spec.submission for spec in pipeline.registry.visible(Phase.DIAGNOSE))
    result = await pipeline.execute('Read', {'file_path': str(source)}, 'read-1')
    assert result.result['raw_content'] == 'answer = 42\n'
    assert result.result['before_hash'] == digest(source.read_bytes())
    assert result.result['overlay'] is False and result.executed is True
    assert json.loads(result.to_content())['result']['raw_content'] == 'answer = 42\n'
    assert [kind for kind, payload in events] == ['tool.started', 'tool.completed']
    with pytest.raises(ToolProtocolError):
        await pipeline.execute('Write', {'file_path': str(source), 'content': 'changed'}, 'write-1')
    assert source.read_text(encoding='utf-8') == 'answer = 42\n'


@pytest.mark.parametrize('relative', ['../outside.txt', '.env', '.env.local', '.git/config', 'oracle/hidden.json',
                                     'held_out/assertions.json', 'private/answer.txt', 'hidden/answer.txt',
                                     'final-scoring/answer.txt', 'report.oracle.json'])
async def test_chat_read_rejects_outside_sensitive_and_oracle_paths(chat_tools, relative):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('blocked-content-sentinel', encoding='utf-8')
    result = await pipeline.execute('Read', {'file_path': str(target)}, 'blocked-1')
    assert result.is_error and result.executed is False
    assert 'blocked-content-sentinel' not in result.to_content()


async def test_chat_tools_reject_nested_project_and_scope_epoch_drift(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    nested = root / 'nested'
    nested.mkdir()
    source = nested / 'answer.py'
    source.write_text('not visible', encoding='utf-8')
    scopes.projects['beta'] = Project(id='beta', repo_id='beta-repo', root=nested)
    result = await pipeline.execute('Read', {'file_path': str(source)}, 'nested-1')
    assert result.is_error and result.executed is False
    scopes.projects['alpha'].access_epoch += 1
    with pytest.raises(PermissionError, match='作用域授权已变更'):
        await pipeline.execute('Glob', {'pattern': '**/*'}, 'glob-revoked')


async def test_chat_glob_grep_use_existing_handlers_and_skip_protected_paths(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    source = root / 'answer.py'
    source.write_text('answer = 42\n', encoding='utf-8')
    for relative in ('.env', 'oracle/hidden.py', 'hidden/data.py', 'private/data.py',
                     'final-scoring/data.py', 'data.held-out.json'):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('answer = hidden', encoding='utf-8')
    found = await pipeline.execute('Glob', {'pattern': '**/*'}, 'glob-1')
    assert found.result['files'] == [str(source)]
    match = await pipeline.execute('Grep', {'pattern': 'answer', 'output_mode': 'content'}, 'grep-1')
    assert not match.is_error
    assert 'answer = 42' in match.to_content() and 'hidden' not in match.to_content()
    assert source.read_text(encoding='utf-8') == 'answer = 42\n'


async def test_chat_read_clips_with_integrity_checked_complete_artifact(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    source = root / 'long.txt'
    original = '完整工具结果内容' * 8000
    source.write_text(original, encoding='utf-8')
    result = await pipeline.execute('Read', {'file_path': str(source)}, 'long-1')
    assert result.truncated and result.artifact_ref
    assert estimate_tokens(result.to_content()) <= 4000
    complete = artifacts.json(ctx.active_scope, 'chat_session', result.artifact_ref)
    assert complete['result']['raw_content'] == original
    assert complete['result']['before_hash'] == digest(source.read_bytes())
    with pytest.raises(FileNotFoundError):
        artifacts.json(ctx.active_scope, 'different_session', result.artifact_ref)


async def test_chat_tools_reject_size_scan_limits_and_invalid_pattern(chat_tools, monkeypatch):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    source = root / 'large.txt'
    with source.open('wb') as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    result = await pipeline.execute('Read', {'file_path': str(source)}, 'large-1')
    assert result.is_error and '2 MB' in result.error['message']
    source.unlink()
    for index in range(3):
        (root / f'{index}.txt').write_text('answer', encoding='utf-8')
    monkeypatch.setattr('tracefix.tools.chat.MAX_SCAN_FILES', 2)
    scan = await pipeline.execute('Glob', {'pattern': '**/*'}, 'scan-1')
    assert scan.is_error and '请指定更小的 path' in scan.error['message']
    monkeypatch.setattr('tracefix.tools.chat.MAX_SCAN_FILES', MAX_SCAN_FILES)
    invalid = await pipeline.execute('Grep', {'pattern': '['}, 'invalid-regex')
    assert invalid.is_error and invalid.executed is False


async def test_chat_document_search_respects_scope_disabled_and_public_evidence(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    for project_id, enabled, title in [('alpha', True, 'current'), (None, True, 'global'), ('beta', True, 'other'), ('alpha', False, 'disabled')]:
        library.save_document({'projectId': project_id, 'enabled': enabled, 'kind': 'repair',
                               'title': title, 'content': 'answer guidance', 'tags': []})
    library.save_document({'projectId': 'alpha', 'enabled': True, 'kind': 'repair',
                           'title': 'hidden', 'content': json.dumps({'held_out': True, 'answer': 'hidden assertion'}), 'tags': []})
    result = await pipeline.execute('DocumentSearch', {'query': 'answer'}, 'docs-1')
    records = result.result['documents']
    assert {record['title'] for record in records} == {'current', 'global'}
    assert all(record['version'] == 1 and record['excerpt'] == 'answer guidance' for record in records)
    disabled = build_chat_tools(scopes, ctx, library, artifacts, 'chat_disabled', use_knowledge=False)
    assert {spec.name for spec in disabled.registry.visible(Phase.DIAGNOSE)} == {'Read', 'Grep', 'Glob'}


async def test_chat_completed_call_is_reused_without_reading_changed_content(chat_tools):
    root, scopes, ctx, library, artifacts, pipeline, events = chat_tools
    source = root / 'answer.py'
    source.write_text('answer = 42', encoding='utf-8')
    first = await pipeline.execute('Read', {'file_path': str(source)}, 'same-call')
    source.write_text('answer = changed', encoding='utf-8')
    replay = await pipeline.execute('Read', {'file_path': str(source)}, 'same-call')
    assert replay.to_content() == first.to_content()
    assert [kind for kind, payload in events] == ['tool.started', 'tool.completed']
