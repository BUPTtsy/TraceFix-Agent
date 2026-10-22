import json
import os

import pytest

from test_sdk_workspace import sdk_workspace
from tracefix.agents import claude_sdk_worker
from tracefix.execution.candidate_export import SDKCandidateRejected, _snapshot, export_candidate

SECRET = 'sk-ant-test-secret-0123456789'


def _tree(root):
    for relative, text in {'src/app.js': 'x\n', 'node_modules/pkg/index.js': 'y\n',
                           'dist/out.js': 'z\n', 'lib/node_modules/keep.js': 'k\n'}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


def test_snapshot_skip_drops_only_top_level_subtrees(tmp_path):
    root = _tree(tmp_path / 'ws')
    full = _snapshot(root)
    skipped = _snapshot(root, skip=('node_modules', 'dist'))
    assert _snapshot(root, skip=()) == full
    assert {'node_modules', 'node_modules/pkg/index.js', 'dist', 'dist/out.js'} <= full.entries.keys()
    assert not any(key == name or key.startswith(name + '/')
                   for key in skipped.entries for name in ('node_modules', 'dist'))
    # 非顶层同名目录照常扫描
    assert 'lib/node_modules/keep.js' in skipped.entries
    assert skipped.entries == {key: value for key, value in full.entries.items()
                               if key.split('/')[0] not in {'node_modules', 'dist'}}
    assert skipped.content_hash != full.content_hash


def test_worker_skip_only_host_mounts(tmp_path, monkeypatch):
    root = _tree(tmp_path / 'ws')
    assert claude_sdk_worker.snapshot_skip(root) == ()
    mounted = {os.path.normcase(str(root / 'node_modules'))}
    monkeypatch.setattr(claude_sdk_worker.os.path, 'ismount',
                        lambda path: os.path.normcase(str(path)) in mounted)
    assert claude_sdk_worker.snapshot_skip(root) == ('node_modules',)


def test_worker_event_is_summary_and_file_holds_skipped_manifest(tmp_path, monkeypatch):
    root = _tree(tmp_path / 'ws')
    monkeypatch.setattr(claude_sdk_worker.os.path, 'ismount',
                        lambda path: os.path.basename(str(path)) in {'node_modules', 'dist'})
    target = tmp_path / 'state' / 'linux_input_manifest.json'
    target.parent.mkdir()
    event = claude_sdk_worker.write_snapshot(root, target)
    expected = _snapshot(root, skip=('node_modules', 'dist'))
    assert event == {'content_hash': expected.content_hash, 'entries_count': len(expected.entries),
                     'skipped': ['node_modules', 'dist']}
    assert json.loads(target.read_text(encoding='utf-8')) == expected.model_dump()


def test_worker_event_inlines_small_entries_only(tmp_path, monkeypatch):
    root = _tree(tmp_path / 'ws')
    target = tmp_path / 'manifest.json'
    event = claude_sdk_worker.write_snapshot(root, target)
    assert event['entries'] == _snapshot(root).entries and event['skipped'] == []
    monkeypatch.setattr(claude_sdk_worker, 'INLINE_ENTRIES_LIMIT', 0)
    event = claude_sdk_worker.write_snapshot(root, target)
    assert 'entries' not in event and event['entries_count'] == len(_snapshot(root).entries)


def _export(sdk, authority, **kwargs):
    return export_candidate(sdk, authority, summary='修复', evidence_refs=['evidence'], **kwargs)


def test_forbidden_value_rejects_without_leaking(sdk_workspace):
    authority, sdk = sdk_workspace
    (sdk.source / 'src/new.py').write_text(f'leak = "{SECRET}"\n', encoding='utf-8')
    with pytest.raises(SDKCandidateRejected) as error:
        _export(sdk, authority, forbidden_values=(SECRET,))
    assert error.value.reason == '候选包含受保护凭据' and error.value.path == 'src/new.py'
    assert SECRET not in str(error.value) and SECRET not in repr(error.value.args)


def test_forbidden_value_short_ignored_and_default_unchanged(sdk_workspace):
    authority, sdk = sdk_workspace
    (sdk.source / 'src/app.py').write_text(f'value = 2  # {SECRET}\n', encoding='utf-8')
    baseline = _export(sdk, authority)
    assert [edit.path for edit in baseline.edits] == ['src/app.py']
    # 长度 <8 的值即使出现在内容里也被忽略
    short = _export(sdk, authority, forbidden_values=('value', 'abc1234', ''))
    assert short.model_dump() == baseline.model_dump()
    with pytest.raises(SDKCandidateRejected, match='受保护凭据'):
        _export(sdk, authority, forbidden_values=('value', SECRET))
