from types import SimpleNamespace

import pytest

from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import digest
from tracefix.runtime.local_tools import GrepInput, LocalTools, ReadInput
from tracefix.runtime.tools import ToolRejected


@pytest.fixture
def source_tools(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'src').mkdir()
    workspace = Workspace(root, ['src/**', '*.py'])
    workspace.repository_snapshot = {'fixture': 'source-baseline'}
    state = SimpleNamespace(source_manifest=digest(workspace.repository_snapshot))
    tools = LocalTools(SimpleNamespace(workspace=workspace), {}, state)
    return workspace, tools


def test_read_overlay_content_version_keeps_disk_before_hash(source_tools):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'store.ts'
    path.write_bytes(b'const checked = false;\n')
    disk_version = digest(path.read_bytes())
    workspace.stage('src/store.ts', 'const checked = true;\n')

    result = tools.read(ReadInput(file_path=str(path)))

    assert result['content'] == '1\tconst checked = true;'
    assert result['before_hash'] == disk_version
    assert result['content_version'] == digest(b'const checked = true;\n')
    assert result['content_version'] != result['before_hash']
    assert result['source_revision'] == tools.state.source_manifest
    assert result['relative_path'] == 'src/store.ts'
    assert result['overlay'] is True
    assert (result['start_line'], result['end_line'], result['total_lines']) == (1, 1, 1)


def test_read_expected_version_rejects_changed_overlay(source_tools):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'store.ts'
    path.write_bytes(b'const checked = false;\n')
    initial = tools.read(ReadInput(file_path=str(path)))
    workspace.stage('src/store.ts', 'const checked = true;\n')

    with pytest.raises(ToolRejected, match='content_version'):
        tools.read(ReadInput(file_path=str(path), expected_content_version=initial['content_version']))


def test_grep_matches_overlay_and_preserves_multiple_same_labels(source_tools):
    workspace, tools = source_tools
    first = workspace.root / 'src' / 'form.tsx'
    second = workspace.root / 'src' / 'dialog.tsx'
    first.write_bytes(b'const label = "Old label";\n')
    second.write_bytes(b'const label = "Save release";\n')
    workspace.stage('src/form.tsx', 'const label = "Save release";\n')

    result = tools.grep(GrepInput(pattern='Save release', output_mode='content'))

    assert {match['path'] for match in result['matches']} == {str(first), str(second)}
    for match in result['matches']:
        assert match['start_line'] == match['end_line'] == 1
        assert match['source_revision'] == tools.state.source_manifest
        assert match['content_version']
        assert match['relative_path'].startswith('src/')
    assert result['metadata'][str(first)]['content_version'] == digest(workspace.staged_content('src/form.tsx'))
    assert result['metadata'][str(first)]['before_hash'] == digest(first.read_bytes())
    assert result['metadata'][str(first)]['overlay'] is True
    assert result['metadata'][str(second)]['overlay'] is False
    assert set(tools.grep(GrepInput(pattern='Save release'))['matches']) == {str(first), str(second)}
    assert tools.grep(GrepInput(pattern='Old label'))['matches'] == []


@pytest.mark.parametrize('change', ['disk', 'overlay'])
def test_grep_rejects_concurrent_source_drift(source_tools, monkeypatch, change):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'store.ts'
    path.write_bytes(b'const checked = false;\n')
    from tracefix.runtime import local_tools
    original = local_tools.subprocess.run

    def scan_then_change(command, **options):
        result = original(command, **options)
        if change == 'disk':
            path.write_bytes(b'const checked = true;\n')
        else:
            workspace.stage('src/store.ts', 'const checked = true;\n')
        return result

    monkeypatch.setattr(local_tools.subprocess, 'run', scan_then_change)
    with pytest.raises(ToolRejected, match='过期命中'):
        tools.grep(GrepInput(pattern='checked', output_mode='content'))


def test_grep_protected_files_never_enter_frozen_scan(source_tools):
    workspace, tools = source_tools
    (workspace.root / '.env').write_text('hidden-token\n', encoding='utf-8')
    (workspace.root / '.git').mkdir()
    (workspace.root / '.git' / 'secret.json').write_text('hidden-token\n', encoding='utf-8')
    visible = workspace.root / 'src' / 'app.ts'
    visible.write_text('visible-token\n', encoding='utf-8')

    result = tools.grep(GrepInput(pattern='hidden-token', output_mode='content'))

    assert result['matches'] == []
    assert result['metadata'] == {}


def test_fragments_find_large_middle_literal_and_bind_version(source_tools):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'screen.tsx'
    lines = [f'const filler_{number} = "' + 'x' * 250 + '";' for number in range(1200)]
    lines[700] = 'const saveLabel = "Save release";'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='')
    assert path.stat().st_size > 200000

    cards = workspace.fragments('Save release', limit_chars=40000)

    assert len(cards) == 1
    card = cards[0]
    assert card['path'] == 'src/screen.tsx'
    assert card['start_line'] <= 701 <= card['end_line']
    assert 80 <= card['end_line'] - card['start_line'] + 1 <= 160
    assert 'Save release' in card['content']
    assert card['before_hash'] == card['content_version'] == digest(path.read_bytes())
    assert card['source_revision'] == tools.state.source_manifest
    assert card['truncated'] is True
    bounded = workspace.fragments('Save release')
    assert len(bounded) == 1
    assert bounded[0]['start_line'] <= 701 <= bounded[0]['end_line']
    assert len(bounded[0]['content']) <= 16000


def test_fragments_overlay_uses_current_bytes_and_fallback_is_small(source_tools):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'store.ts'
    disk = '\n'.join(f'const value_{number} = {number};' for number in range(500)) + '\n'
    path.write_text(disk, encoding='utf-8', newline='')
    overlay = disk.replace('const value_250 = 250;', 'const hydrationKey = "/api/tasks";')
    workspace.stage('src/store.ts', overlay)

    card = workspace.fragments('/api/tasks')[0]

    assert card['start_line'] <= 251 <= card['end_line']
    assert card['before_hash'] == digest(disk.encode())
    assert card['content_version'] == digest(overlay.encode())
    assert card['overlay'] is True
    fallback = workspace.fragments('unmatched-symptom', preferred_paths=['src/store.ts'])
    assert len(fallback) == 1
    assert fallback[0]['start_line'] == 1
    assert fallback[0]['end_line'] == 160


def test_read_large_middle_range_is_versioned_without_expanding_file(source_tools):
    workspace, tools = source_tools
    path = workspace.root / 'src' / 'large.ts'
    lines = [f'const line_{number} = {number};' for number in range(3000)]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='')

    result = tools.read(ReadInput(file_path=str(path), offset=1500, limit=120))

    assert (result['start_line'], result['end_line']) == (1500, 1619)
    assert len(result['content'].splitlines()) == 120
    assert result['total_lines'] == 3000
    assert result['next_offset'] == 1620
    assert result['truncated'] is True
    assert result['content_version'] == digest(path.read_bytes())
