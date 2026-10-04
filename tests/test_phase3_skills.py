import hashlib
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.knowledge.context import SkillCatalog
from tracefix.runtime.skills import SkillStore
from tracefix.storage.artifacts import Artifacts


def _skill(root: Path, body: str = '正文 A。') -> Path:
    directory = root / 'reproduce'
    (directory / 'references').mkdir(parents=True)
    (directory / 'references' / 'checklist.md').write_text('引用 A。', encoding='utf-8')
    (directory / 'SKILL.md').write_text(
        '---\nname: reproduce\ndescription: 最小复现\nversion: "2.0"\n'
        'phases: [REPRODUCE]\nreferences: [references/checklist.md]\n---\n\n' + body,
        encoding='utf-8',
    )
    return directory / 'SKILL.md'


def _state():
    return SimpleNamespace(scope_id='scope', run_id='run', skills_loaded=[])


def test_catalog_loads_real_reference_bytes_and_hash(tmp_path):
    _skill(tmp_path)
    catalog = SkillCatalog(tmp_path)

    references = catalog.load_references('reproduce', 'REPRODUCE')

    assert len(references) == 1
    assert references[0]['path'] == 'references/checklist.md'
    assert references[0]['content'] == '引用 A。'
    assert references[0]['content_hash'] == hashlib.sha256('引用 A。'.encode()).hexdigest()
    assert references[0]['source'].endswith('reproduce/references/checklist.md')


def test_snapshot_restores_same_body_and_reference_after_source_change_or_loss(tmp_path):
    skill_path = _skill(tmp_path)
    artifacts = Artifacts(tmp_path / 'artifacts')
    catalog = SkillCatalog(tmp_path)
    state = _state()
    store = SkillStore(artifacts)

    first = store.load(state, catalog, 'reproduce', 'REPRODUCE')
    snapshot_ref = first['snapshot_ref']
    skill_path.write_text(skill_path.read_text(encoding='utf-8').replace('正文 A。', '正文 B。'), encoding='utf-8')
    (skill_path.parent / 'references' / 'checklist.md').unlink()

    restored = store.load(state, catalog, 'reproduce', 'REPRODUCE')

    assert restored['snapshot_ref'] == snapshot_ref
    assert restored['content'].endswith('正文 A。')
    assert restored['references'][0]['content'] == '引用 A。'
    assert state.skills_loaded[0]['snapshot_ref'] == snapshot_ref


def test_wrong_phase_and_missing_optional_skill_degrade_to_plain_tools(tmp_path):
    _skill(tmp_path)
    catalog = SkillCatalog(tmp_path)
    state = _state()
    context = SkillStore().context(state, catalog, 'DIAGNOSE', explicit=['reproduce'])

    assert context['skills'] == []
    assert context['skill_index'] == []
    assert context['skill_unavailable'][0]['name'] == 'reproduce'

    empty_catalog_root = tmp_path / 'empty'
    empty_catalog_root.mkdir()
    missing = SkillStore().context(state, SkillCatalog(empty_catalog_root), 'REPRODUCE', explicit=['missing'])
    assert missing['skill_index'] == missing['skills'] == []
    assert missing['skill_unavailable'][0]['name'] == 'missing'


def test_selection_is_bounded_and_index_does_not_contain_bodies(tmp_path):
    for index in range(8):
        directory = tmp_path / f'skill-{index}'
        directory.mkdir()
        (directory / 'SKILL.md').write_text(
            f'---\nname: skill-{index}\ndescription: d{index}\nphases: [REPRODUCE]\n---\n\nsecret-{index}',
            encoding='utf-8',
        )
    catalog = SkillCatalog(tmp_path)
    context = SkillStore().context(_state(), catalog, 'REPRODUCE', limit=3)

    assert len(context['skill_index']) == 3
    assert len(context['skills']) == 3
    assert all('content' not in entry for entry in context['skill_index'])
    assert all(entry['content'].startswith('---') for entry in context['skills'])


def test_context_uses_frozen_metadata_after_body_phase_update_and_deletion(tmp_path):
    path = _skill(tmp_path)
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = _state()
    store = SkillStore(artifacts)
    original = store.context(state, SkillCatalog(tmp_path), 'REPRODUCE')
    path.write_text(path.read_text(encoding='utf-8').replace('[REPRODUCE]', '[DIAGNOSE]'),
                    encoding='utf-8')
    resumed = store.context(state, SkillCatalog(tmp_path), 'REPRODUCE')
    assert resumed == original
    path.unlink()
    resumed = SkillStore(artifacts).context(state, SkillCatalog(tmp_path), 'REPRODUCE')
    assert resumed == original
    with pytest.raises(PermissionError):
        store.load(state, SkillCatalog(tmp_path), 'reproduce', 'DIAGNOSE')


def test_snapshot_preserves_source_bytes_and_hashes_actual_injected_content(tmp_path):
    path = _skill(tmp_path)
    raw = path.read_bytes().replace(b'\n', b'\r\n') + b'\r\napi_key: fixture-secret\r\n'
    path.write_bytes(raw)
    reference = path.parent / 'references/checklist.md'
    reference_raw = b'\xef\xbb\xbfReference\r\nnext\r\n'
    reference.write_bytes(reference_raw)
    artifacts = Artifacts(tmp_path / 'artifacts')
    state = _state()
    store = SkillStore(artifacts)
    loaded = store.load(state, SkillCatalog(tmp_path), 'reproduce', 'REPRODUCE')
    snapshot = artifacts.json(state.scope_id, state.run_id, loaded['snapshot_ref'])
    saved = base64.b64decode(snapshot['content_b64'])
    assert saved == raw.replace(b'fixture-secret', b'[REDACTED]')
    assert snapshot['source_redacted'] is True
    assert snapshot['source_hash'] == hashlib.sha256(raw).hexdigest()
    assert base64.b64decode(snapshot['references'][0]['content_b64']) == reference_raw
    assert 'fixture-secret' not in loaded['content']
    assert hashlib.sha256(loaded['content'].encode()).hexdigest() == loaded['content_hash']
    assert store.load(state, SkillCatalog(tmp_path), 'reproduce', 'REPRODUCE') == loaded


def test_missing_reference_only_skips_its_optional_skill(tmp_path):
    path = _skill(tmp_path)
    (path.parent / 'references/checklist.md').unlink()
    context = SkillStore(Artifacts(tmp_path / 'artifacts')).context(
        _state(), SkillCatalog(tmp_path), 'REPRODUCE')
    assert context['skills'] == []
    assert context['skill_unavailable'][0]['name'] == 'reproduce'


def test_reference_paths_cannot_escape_skill_directory(tmp_path):
    path = _skill(tmp_path)
    path.write_text(path.read_text(encoding='utf-8').replace('references/checklist.md', '../../outside.md'),
                    encoding='utf-8')
    with pytest.raises(ValueError, match='路径超出'):
        SkillCatalog(tmp_path).details()
