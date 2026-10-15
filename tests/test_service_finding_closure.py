import pytest

from tracefix.runtime.finding_source import create_finding_repair_run
from tests.test_finding_repair_source import source_fixture


def test_repair_source_hashes_replay_and_feedback_evidence_closure(tmp_path):
    source, _, artifacts, store, _, options = source_fixture(tmp_path)
    extra = artifacts.put(source.scope_id, source.run_id, b'feedback pixels', 'png')
    source.diagnosis_feedback_refs = [artifacts.put(source.scope_id, source.run_id,
        {'reason': '失败证据', 'evidence_refs': [extra]}, label='闭包反馈')]
    store.save(source)
    repair = create_finding_repair_run('control_task', ['selected_finding'], **options)
    hashes = repair.repair_source.source_evidence_hashes
    assert extra in hashes and source.replay_plan_ref in hashes
    assert source.diagnosis_feedback_refs[0] in hashes
    artifacts._path(source.scope_id, source.run_id, extra).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='完整性'):
        create_finding_repair_run('control_task', ['selected_finding'], **options)
