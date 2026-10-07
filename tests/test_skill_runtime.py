import copy
from pathlib import Path

import pytest

from tracefix.execution.repository import file_manifest
from tracefix.knowledge.context import SkillCatalog
from tracefix.model.gateway import Gateway, ModelError
from tracefix.rules import Rule, RuleResolver
from tracefix.runtime import smoke
from tracefix.runtime.contracts import BrowserAction, Decision, Phase, digest
from tracefix.runtime.smoke import make_engine


def events(engine, state, kind):
    return [event['payload'] for event in engine.store.trace(state.run_id, state.scope_id)
            if event['type'] == kind]


def test_builtin_skills_have_a_model_consumer_phase():
    model_phases = {'PREPARE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE'}
    catalog = SkillCatalog(Path('backend/skills'))
    for entry in catalog.details():
        assert model_phases.intersection(entry['phases']), entry['name']


async def skill_engine(root, *, category='a11y', url_patterns=None):
    original_index = smoke.safe_index_files

    def initialize_skill_source(source_root, paths):
        # 在冻结快照前真实索引源码，让 make_engine 安全提交并导出完整的 tracked entries。
        (source_root / 'src/Search.tsx').write_bytes(b'export const Search = () => null;\n')
        return original_index(source_root, [*paths, 'src/Search.tsx'])

    with pytest.MonkeyPatch.context() as fixture_patch:
        fixture_patch.setattr(smoke, 'safe_index_files', initialize_skill_source)
        engine, state = make_engine(root)
    engine.subagent_enabled = False
    rule = Rule(id='skill-rule', name='页面状态规则', status='enabled', category=category,
        phases=['PREPARE', 'EXPLORE', 'DIAGNOSE'],
        scope={'url_patterns': url_patterns or []},
        detection={'type': 'guided', 'guided': {'prompt': '检查页面状态并引用证据'}})
    engine.rule_resolver = RuleResolver([rule])
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    state.observation_ref = await engine.capture(state, await engine.browser.action(
        BrowserAction(kind='observe')))
    engine.store.save(state)
    engine.model = Gateway(key='fixture', vision_model='', max_retry_delay=0)
    return engine, state


async def test_skill_engine_binds_complete_tracked_snapshot(tmp_path):
    engine, state = await skill_engine(tmp_path)
    source = copy.deepcopy(engine.source)
    assert source['entries'] == file_manifest(engine.workspace.root)
    assert source['entries']['src/Search.tsx']['tracked'] is True
    assert source['entries']['src/Search.tsx']['git_mode'] == '100644'
    assert source['files']['src/Search.tsx'] == digest(b'export const Search = () => null;\n')
    assert engine.workspace.require_repository_snapshot() == source
    engine.workspace.validate_repository(engine.scopes, engine.context, source)
    assert engine.get(state, state.repo_snapshot_ref) == source
    assert state.source_manifest == engine.runner.source == digest(source)
    assert state.test_spec_hash == digest(engine.spec(state))
    assert engine.source == source

async def test_skill_events_bind_snapshot_and_reference_metadata_without_content(tmp_path, monkeypatch):
    engine, state = await skill_engine(tmp_path)
    skill = tmp_path / 'skills/referenced/SKILL.md'
    reference = skill.parent / 'references/checklist.md'
    reference.parent.mkdir(parents=True)
    skill.write_text('---\nname: referenced\nversion: "2.0.0"\n'
                     'description: 带引用的测试 Skill。\nphases: [EXPLORE]\n'
                     'references: [references/checklist.md]\n---\n正文。', encoding='utf-8')
    reference.write_text('引用正文。', encoding='utf-8')
    engine.skills = SkillCatalog(skill.parent.parent)
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    result = engine.load_skill(state, 'referenced')
    event = next(item for item in events(engine, state, 'skill.loaded') if item['name'] == 'referenced')
    assert event['snapshot_ref'] == result['snapshot_ref']
    assert event['references'] == [{
        key: result['references'][0][key]
        for key in ('path', 'content_hash', 'byte_count', 'source_hash',
                    'frozen_source_hash', 'source_redacted')
        if key in result['references'][0]
    }]
    assert 'content' not in event['references'][0]

async def test_configuration_failure_does_not_claim_request_injection(tmp_path):
    engine, state = await skill_engine(tmp_path)
    engine.model.key = ''
    with pytest.raises(ModelError, match='TRACEFIX_API_KEY'):
        await engine.model_call(state, Decision, {})
    assert events(engine, state, 'skill.loaded')
    assert events(engine, state, 'skills.injected') == []
    assert events(engine, state, 'model.request.persisted') == []

async def test_failed_document_read_does_not_mark_skill_loaded(tmp_path, monkeypatch):
    engine, state = await skill_engine(tmp_path)

    def unavailable(name, phase):
        raise OSError('fixture unreadable document')

    monkeypatch.setattr(engine.skills, 'load_document', unavailable)
    with pytest.raises(OSError, match='unreadable document'):
        engine.load_skill(state, 'empty-state-accessibility')
    assert state.skills_loaded == []
    assert events(engine, state, 'skill.loaded') == []
