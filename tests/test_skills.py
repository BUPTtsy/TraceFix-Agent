import hashlib
import os
from collections import Counter
from pathlib import Path

import pytest

from tracefix.knowledge.context import SkillCatalog


def _write_skill(root, name, body='Skill instructions.', phases=('PATCH',)):
    directory = root / name
    directory.mkdir(exist_ok=True)
    path = directory / 'SKILL.md'
    path.write_text(
        f'---\nname: {name}\ndescription: {name} skill\n'
        f'phases: [{", ".join(phases)}]\n---\n\n{body}',
        encoding='utf-8',
    )
    return path


@pytest.fixture
def skill_reads(monkeypatch):
    reads = []
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name == 'SKILL.md':
            reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    return reads


def test_standard_skill_frontmatter_defaults_to_all_phases(tmp_path):
    skill = tmp_path / 'plain'
    skill.mkdir()
    (skill / 'SKILL.md').write_text(
        '---\nname: plain\ndescription: 一个兼容标准格式的 Skill。\n---\n\n正文不应被截断。',
        encoding='utf-8',
    )

    catalog = SkillCatalog(tmp_path)
    entry = catalog.details()[0]

    assert entry['name'] == 'plain'
    assert entry['description'] == '一个兼容标准格式的 Skill。'
    assert entry['version'] == '1.0.0'
    assert catalog.summaries('DIAGNOSE') == [
        {'name': 'plain', 'description': '一个兼容标准格式的 Skill。'}
    ]
    assert catalog.load('plain', 'DIAGNOSE').endswith('正文不应被截断。')


def test_trigger_selection_uses_phase_framework_rule_and_files():
    catalog = SkillCatalog(Path('backend/skills'))

    selected = {entry['name'] for entry in catalog.select('PATCH', {
        'frameworks': ['react'],
        'rule_categories': ['functional'],
        'files': ['src/TaskList.tsx'],
    })}

    assert 'react-checkbox-persistence-fix' in selected
    assert 'react-state-bug-diagnose' in selected
    assert 'workflow-test-plan' not in selected


def test_skill_index_has_no_skill_token_budget():
    catalog = SkillCatalog(Path('backend/skills'))

    assert all('max_tokens' not in entry for entry in catalog.details())
    assert all(set(summary) == {'name', 'description'}
               for summary in catalog.summaries('PREPARE'))


def test_catalog_reuses_file_reads_across_public_interfaces(skill_reads):
    root = Path('backend/skills')
    paths = sorted(root.glob('*/SKILL.md'))
    catalog = SkillCatalog(root)

    assert catalog.index()
    assert catalog.details()
    assert catalog.summaries('PATCH')
    selected = catalog.select('PATCH', {
        'frameworks': ['react'],
        'rule_categories': ['functional'],
        'files': ['src/TaskList.tsx'],
    })
    assert selected
    for entry in selected:
        name = entry['name']
        metadata, content = catalog.load_document(name, 'PATCH')
        assert catalog.load_entry(name, 'PATCH') == metadata
        assert catalog.load(name, 'PATCH') == content
        assert metadata['content_hash'] == hashlib.sha256(content.encode('utf-8')).hexdigest()
    assert catalog.index()

    assert Counter(skill_reads) == Counter({path: 1 for path in paths})


def test_catalog_invalidates_added_changed_and_deleted_files(tmp_path, skill_reads):
    first = _write_skill(tmp_path, 'first', 'First instructions.')
    second = _write_skill(tmp_path, 'second')
    catalog = SkillCatalog(tmp_path)
    original_metadata, original_content = catalog.load_document('first', 'PATCH')

    _write_skill(tmp_path, 'first', 'Updated instructions, with another sentence.')
    updated_metadata, updated_content = catalog.load_document('first', 'PATCH')

    assert original_content.endswith('First instructions.')
    assert updated_content.endswith('Updated instructions, with another sentence.')
    assert updated_metadata['content_hash'] != original_metadata['content_hash']
    assert updated_metadata['content_hash'] == hashlib.sha256(
        updated_content.encode('utf-8')).hexdigest()
    assert Counter(skill_reads) == Counter({first: 2, second: 1})

    third = _write_skill(tmp_path, 'third')
    assert [entry['name'] for entry in catalog.index()] == ['first', 'second', 'third']
    assert Counter(skill_reads) == Counter({first: 2, second: 1, third: 1})

    second.unlink()
    assert [entry['name'] for entry in catalog.index()] == ['first', 'third']
    with pytest.raises(PermissionError):
        catalog.load_document('second', 'PATCH')
    assert Counter(skill_reads) == Counter({first: 2, second: 1, third: 1})

    _write_skill(tmp_path, 'second', 'Recreated instructions.')
    assert catalog.load('second', 'PATCH').endswith('Recreated instructions.')
    assert Counter(skill_reads) == Counter({first: 2, second: 2, third: 1})


def test_catalog_invalidates_same_size_edits_and_phase_changes(tmp_path, skill_reads):
    path = _write_skill(tmp_path, 'example', 'First body.', phases=('PATCH',))
    catalog = SkillCatalog(tmp_path)
    original_metadata = catalog.load_entry('example', 'PATCH')
    original_status = path.stat()

    _write_skill(tmp_path, 'example', 'Other body.', phases=('PATCH',))
    os.utime(path, ns=(original_status.st_atime_ns, original_status.st_mtime_ns + 1_000_000))
    assert path.stat().st_size == original_status.st_size
    metadata, content = catalog.load_document('example', 'PATCH')
    assert content.endswith('Other body.')
    assert metadata['content_hash'] != original_metadata['content_hash']
    assert skill_reads == [path, path]

    _write_skill(tmp_path, 'example', 'Other body.', phases=('REVIEW',))
    assert catalog.summaries('PATCH') == []
    assert catalog.load('example', 'review').endswith('Other body.')
    with pytest.raises(PermissionError):
        catalog.load('example', 'PATCH')
    assert skill_reads == [path, path, path]


def test_catalog_invalidates_replacement_with_preserved_size_and_timestamp(tmp_path, skill_reads):
    path = _write_skill(tmp_path, 'example', 'First body.')
    catalog = SkillCatalog(tmp_path)
    original_metadata, original_content = catalog.load_document('example', 'PATCH')
    original_status = path.stat()
    replacement = path.with_name('replacement.md')
    replacement.write_text(original_content.replace('First body.', 'Other body.'), encoding='utf-8')
    os.utime(replacement, ns=(original_status.st_atime_ns, original_status.st_mtime_ns))
    replacement.replace(path)

    assert path.stat().st_size == original_status.st_size
    assert path.stat().st_mtime_ns == original_status.st_mtime_ns
    metadata, content = catalog.load_document('example', 'PATCH')
    assert content.endswith('Other body.')
    assert metadata['content_hash'] != original_metadata['content_hash']
    assert skill_reads == [path, path]


def test_load_document_keeps_metadata_and_content_together_during_edit(tmp_path, monkeypatch):
    path = _write_skill(tmp_path, 'example', 'Original instructions.')
    original_read_text = Path.read_text
    original_content = original_read_text(path, encoding='utf-8')
    changed = False

    def read_text_and_edit(current_path, *args, **kwargs):
        nonlocal changed
        content = original_read_text(current_path, *args, **kwargs)
        if current_path == path and not changed:
            changed = True
            _write_skill(tmp_path, 'example', 'Changed instructions with different length.')
        return content

    monkeypatch.setattr(Path, 'read_text', read_text_and_edit)
    catalog = SkillCatalog(tmp_path)

    metadata, content = catalog.load_document('example', 'PATCH')
    assert content == original_content
    assert metadata['content_hash'] == hashlib.sha256(content.encode('utf-8')).hexdigest()

    updated_metadata, updated_content = catalog.load_document('example', 'PATCH')
    assert updated_content.endswith('Changed instructions with different length.')
    assert updated_metadata['content_hash'] == hashlib.sha256(
        updated_content.encode('utf-8')).hexdigest()
    assert updated_metadata['content_hash'] != metadata['content_hash']


def test_catalog_cached_metadata_is_isolated_from_callers(tmp_path, skill_reads):
    path = _write_skill(tmp_path, 'example')
    catalog = SkillCatalog(tmp_path)
    entries = [
        catalog.details()[0],
        catalog.select('PATCH')[0],
        catalog.load_entry('example', 'PATCH'),
        catalog.load_document('example', 'PATCH')[0],
    ]
    for entry in entries:
        entry['name'] = 'changed'
        entry['phases'].clear()
        entry['triggers']['frameworks'].append('unexpected')
        entry['tools_hint'].append('unexpected')

    metadata = catalog.load_entry('example', 'PATCH')
    assert metadata['phases'] == ['PATCH']
    assert metadata['triggers']['frameworks'] == []
    assert metadata['tools_hint'] == []
    assert [entry['name'] for entry in catalog.select('PATCH')] == ['example']
    assert skill_reads == [path]
