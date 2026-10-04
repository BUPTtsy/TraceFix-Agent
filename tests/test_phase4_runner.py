import json
import subprocess

import pytest

from evals.config import EvaluationConfig, GROUPS
from evals.runner import FourCellRunner, instrument_memory_calls
from tracefix.knowledge.memory import MemoryLibrary


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def _source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'src').mkdir()
    (source / 'src' / 'app.ts').write_text('export const ok = true;\n', encoding='utf-8')
    _git(source, 'init', '-q')
    _git(source, 'add', '.')
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=fixture', '-c',
                    'user.email=fixture@localhost', 'commit', '-qm', 'base'], check=True)
    return source


def _config(tmp_path, source):
    source_revision = _git(source, 'rev-parse', 'HEAD')
    skills = tmp_path / 'skills'
    skills.mkdir(exist_ok=True)
    (skills / 'skill.txt').write_text('public', encoding='utf-8')
    spec = tmp_path / 'spec.json'
    spec.write_text('{"goal":"public"}', encoding='utf-8')
    protocol = tmp_path / 'protocol.json'
    protocol.write_text('{"reset":true}', encoding='utf-8')
    experience = tmp_path / 'experience.json'
    experience.write_text('{"items":[]}', encoding='utf-8')
    profile = tmp_path / 'profile.json'
    profile.write_text('{"project":"demo"}', encoding='utf-8')
    return EvaluationConfig(case_id='B01', family='mutation', source_root=source,
                            source_revision=source_revision, profile=profile, spec=spec,
                            public_protocol=protocol, skills_root=skills,
                            initial_experience=experience, goal='repair', model='fixture-model',
                            seed=7, tools_hash='tools', recovery_rules_hash='recovery',
                            held_out=True)


def test_group_directories_are_fresh_and_switches_reach_adapter(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)

    def adapter(**kwargs):
        group = kwargs['group']
        env = kwargs['env']
        assert env['TRACEFIX_AGENT_MODE'] == group.agent_mode
        assert env['TRACEFIX_CROSS_RUN_MEMORY'] == ('on' if group.cross_run_memory else 'off')
        return {'status': 'completed', 'outcome': 'INCONCLUSIVE',
                'trace': [{'event': 'model_call', 'model_agent_id': 'one'}],
                'usage': {'prompt_tokens': 2}}

    rows = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_all()
    assert len(rows) == 4
    assert {row['configuration'] for row in rows} == {group.name for group in GROUPS}
    assert all(row['evidence_kind'] == 'inconclusive' for row in rows)
    for row in rows:
        group = tmp_path / 'runs' / 'B01' / row['configuration']
        assert all((group / name).is_dir() for name in ('data', 'artifacts', 'cache', 'browser', 'session'))
        assert row['binding']['source_revision'] == config.source_revision


def test_existing_group_is_never_reused(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)
    runner = FourCellRunner(config, tmp_path / 'runs', adapter=lambda **_: {})
    runner.run_group(GROUPS[0])
    with pytest.raises(FileExistsError):
        runner.run_group(GROUPS[0])


def test_single_trace_with_delegation_is_inconclusive(tmp_path):
    source = _source(tmp_path)
    config = _config(tmp_path, source)

    def adapter(**_):
        return {'trace': [{'event': 'model_call', 'model_agent_id': 'one'},
                          {'event': 'delegation', 'model_agent_id': 'two'}]}

    row = FourCellRunner(config, tmp_path / 'runs', adapter=adapter).run_group(GROUPS[0])
    assert row['evidence_kind'] == 'inconclusive'
    assert row['trace_summary']['trace_proven'] is False


def test_off_switch_reaches_existing_memory_methods_and_keeps_l1(tmp_path, monkeypatch):
    memory = MemoryLibrary(tmp_path / 'memory.sqlite3')
    trace = []
    instrument_memory_calls(memory, trace)
    monkeypatch.setenv('TRACEFIX_CROSS_RUN_MEMORY', 'off')
    memory.note('scope', 'run', {'kind': 'finding', 'text': 'current observation'})
    memory.save_job_memory('scope', 'job', {'text': 'must not persist'},
                           source_run_id='run', source_manifest='source')
    assert memory.job_memory('scope', 'job', 'source') == []
    assert [item['event'] for item in trace] == ['memory.note', 'memory.save_job_memory', 'memory.job_memory']
    assert memory.working_memory('scope', 'run')['finding'][0]['text'] == 'current observation'
