import json

import pytest

from tracefix.storage import artifacts as artifacts_module
from tracefix.storage.artifacts import Artifacts


def test_corrupt_copy_falls_back_to_new_write_without_poisoning_put(tmp_path):
    artifacts = Artifacts(tmp_path)
    value = {'goal': '检查刷新后的状态'}
    first = artifacts.put('demo', 'run_1', value, label='测试规范')
    artifacts._path('demo', 'run_1', first).write_bytes(b'truncated')

    second = artifacts.put('demo', 'run_1', value, label='测试规范')
    assert second != first
    assert artifacts.json('demo', 'run_1', second) == value
    # 损坏副本仍要在显式读取时报错，不能因为去重放宽校验
    with pytest.raises(ValueError, match='完整性'):
        artifacts.read('demo', 'run_1', first)

    # 后续写入应复用新的完好副本，而不是每次都再写一份
    assert artifacts.put('demo', 'run_1', value, label='测试规范') == second
    index = json.loads((tmp_path/'demo'/'run_1'/'文件索引.json').read_text(encoding='utf-8'))
    assert len(index) == 2


def test_missing_file_with_stale_index_entry_is_rewritten(tmp_path):
    artifacts = Artifacts(tmp_path)
    first = artifacts.put('demo', 'run_1', 'data', 'txt', label='记录')
    artifacts._path('demo', 'run_1', first).unlink()
    second = artifacts.put('demo', 'run_1', 'data', 'txt', label='记录')
    assert second != first
    assert artifacts.read('demo', 'run_1', second) == b'data'


def test_callback_reports_each_skipped_copy_and_separates_the_two_reasons(tmp_path):
    reports = []
    artifacts = Artifacts(tmp_path, notify=reports.append)
    value = {'goal': '检查刷新后的状态'}
    corrupt = artifacts.put('alpha', 'run_1', value, label='测试规范')
    artifacts._path('alpha', 'run_1', corrupt).write_bytes(b'truncated')

    good = artifacts.put('alpha', 'run_1', value, label='测试规范')
    assert reports == [{'scope': 'alpha', 'run': 'run_1', 'ref': corrupt, 'reason': '证据校验和不匹配'}]

    # 复用完好副本时不再报告；损坏项每被跳过一次才报告一次
    assert artifacts.put('alpha', 'run_1', value, label='测试规范') == good
    assert len(reports) == 2 and reports[1]['ref'] == corrupt

    reports.clear()
    artifacts._path('alpha', 'run_1', corrupt).unlink()
    artifacts._path('alpha', 'run_1', good).unlink()
    third = artifacts.put('alpha', 'run_1', value, label='测试规范')
    assert third not in (corrupt, good)
    assert [r['ref'] for r in reports] == [corrupt, good]
    assert {r['reason'] for r in reports} == {'证据文件缺失'}


def test_failing_callback_never_breaks_put(tmp_path):
    def explode(report):
        raise RuntimeError('回调故障')

    artifacts = Artifacts(tmp_path, notify=explode)
    first = artifacts.put('alpha', 'run_1', 'data', 'txt', label='记录')
    artifacts._path('alpha', 'run_1', first).write_bytes(b'truncated')
    second = artifacts.put('alpha', 'run_1', 'data', 'txt', label='记录')
    assert second != first
    assert artifacts.read('alpha', 'run_1', second) == b'data'


def test_default_construction_stays_silent_and_unchanged(tmp_path):
    artifacts = Artifacts(tmp_path)
    assert artifacts.notify is None
    first = artifacts.put('alpha', 'run_1', 'data', 'txt', label='记录')
    assert artifacts.put('alpha', 'run_1', 'data', 'txt', label='记录') == first
    assert artifacts.exists('alpha', 'run_1', first)
    artifacts._path('alpha', 'run_1', first).write_bytes(b'truncated')
    assert not artifacts.exists('alpha', 'run_1', first)
    assert artifacts.put('alpha', 'run_1', 'data', 'txt', label='记录') != first


@pytest.mark.parametrize('winerror', [5, 32, 33])
@pytest.mark.parametrize('target', ['trace', 'index'])
def test_phase_trace_replace_retries_windows_denial_and_keeps_integrity(tmp_path, monkeypatch, winerror, target):
    artifacts = Artifacts(tmp_path)
    first = {'scope_id': 'demo', 'run_id': 'run_1', 'phase': 'VERIFY', 'seq': 1,
             'type': 'tool.started', 'payload': {}}
    reference = artifacts.append_trace(first)
    trace_path = artifacts._path('demo', 'run_1', reference)
    destination = trace_path if target == 'trace' else artifacts._index_path('demo', 'run_1')
    replace = artifacts_module.os.replace
    attempts, waits = [], []
    denied = PermissionError(13, 'injected Windows replacement denial')
    denied.winerror = winerror

    def temporarily_denied(source, path):
        if path == destination:
            attempts.append((source, source.read_bytes()))
            if len(attempts) < 3:
                raise denied
        return replace(source, path)

    monkeypatch.setattr(artifacts_module.sys, 'platform', 'win32')
    monkeypatch.setattr(artifacts_module.os, 'replace', temporarily_denied)
    monkeypatch.setattr(artifacts_module.time, 'sleep', waits.append)
    second = {**first, 'seq': 2, 'type': 'tool.completed'}
    assert artifacts.append_trace(second) == reference
    assert len(attempts) == 3 and waits == [0.02, 0.04]
    assert all(attempt == attempts[0] for attempt in attempts)
    restarted = Artifacts(tmp_path)
    assert restarted.json('demo', 'run_1', reference)['events'] == [first, second]
    assert restarted.trace_position('demo', 'run_1') == 2
    assert restarted.append_trace(second) == reference
    assert restarted.exists('demo', 'run_1', reference)
    assert not list(trace_path.parent.glob('*.tmp'))


@pytest.mark.parametrize('target', ['trace', 'index'])
def test_phase_trace_permanent_windows_denial_is_bounded_and_keeps_hash_checks(tmp_path, monkeypatch, target):
    artifacts = Artifacts(tmp_path)
    first = {'scope_id': 'demo', 'run_id': 'run_1', 'phase': 'VERIFY', 'seq': 1,
             'type': 'tool.started', 'payload': {}}
    reference = artifacts.append_trace(first)
    trace_path = artifacts._path('demo', 'run_1', reference)
    index_path = artifacts._index_path('demo', 'run_1')
    previous_trace, previous_index = trace_path.read_bytes(), index_path.read_bytes()
    destination = trace_path if target == 'trace' else index_path
    replace = artifacts_module.os.replace
    attempts, waits = [], []
    denied = PermissionError(13, 'injected permanent Windows replacement denial')
    denied.winerror = 5

    def permanently_denied(source, path):
        if path == destination:
            attempts.append(source)
            raise denied
        return replace(source, path)

    monkeypatch.setattr(artifacts_module.sys, 'platform', 'win32')
    monkeypatch.setattr(artifacts_module.os, 'replace', permanently_denied)
    monkeypatch.setattr(artifacts_module.time, 'sleep', waits.append)
    with pytest.raises(PermissionError) as raised:
        artifacts.append_trace({**first, 'seq': 2})
    assert raised.value is denied
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert waits == [0.02, 0.04, 0.08]
    assert index_path.read_bytes() == previous_index
    assert not list(trace_path.parent.glob('*.tmp'))
    if target == 'trace':
        assert trace_path.read_bytes() == previous_trace
        assert artifacts.trace_position('demo', 'run_1') == 1
    else:
        assert trace_path.read_bytes() != previous_trace
        with pytest.raises(ValueError, match='完整性'):
            artifacts.read('demo', 'run_1', reference)
        with pytest.raises(ValueError, match='完整性'):
            artifacts.trace_position('demo', 'run_1')
        assert not artifacts.exists('demo', 'run_1', reference)


@pytest.mark.parametrize('platform,winerror,error_type', [
    ('linux', 5, PermissionError), ('win32', None, PermissionError),
    ('win32', 87, PermissionError), ('win32', None, OSError)])
def test_atomic_write_does_not_retry_unrelated_errors(tmp_path, monkeypatch, platform, winerror, error_type):
    artifacts = Artifacts(tmp_path)
    path = tmp_path / 'target.txt'
    path.write_bytes(b'original')
    attempts, waits = [], []
    denied = error_type(28 if error_type is OSError else 13, 'injected unrelated write failure')
    if winerror is not None:
        denied.winerror = winerror

    def rejected_replace(source, destination):
        attempts.append(source)
        raise denied

    monkeypatch.setattr(artifacts_module.sys, 'platform', platform)
    monkeypatch.setattr(artifacts_module.os, 'replace', rejected_replace)
    monkeypatch.setattr(artifacts_module.time, 'sleep', waits.append)
    with pytest.raises(OSError) as raised:
        artifacts._write(path, b'replacement')
    assert raised.value is denied and len(attempts) == 1 and waits == []
    assert path.read_bytes() == b'original'
    assert not list(tmp_path.glob('*.tmp'))
