import json
import re
from urllib.parse import quote

import pytest

from tracefix.runtime.contracts import digest
from tracefix.runtime.smoke import make_engine
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.presentation import report_page


def test_readable_names_restart_deduplication_and_integrity(tmp_path):
    artifacts = Artifacts(tmp_path)
    first = artifacts.put('demo', 'run_1', {'goal': '检查刷新后的状态'}, label='测试规范')
    assert first == '0001_测试规范.json'
    assert '\n  "goal": "检查刷新后的状态"\n' in artifacts.read('demo', 'run_1', first).decode('utf-8')
    restarted = Artifacts(tmp_path)
    assert restarted.put('demo', 'run_1', {'goal': '检查刷新后的状态'}, label='测试规范') == first
    second = restarted.put('demo', 'run_1', {'goal': '另一项测试'}, label='测试规范')
    assert second == '0002_测试规范.json'
    index = json.loads((tmp_path/'demo'/'run_1'/'文件索引.json').read_text(encoding='utf-8'))
    assert index[first]['SHA256'] == digest(artifacts.read('demo', 'run_1', first))
    assert len(index) == 2
    artifacts._path('demo', 'run_1', first).write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='完整性'):
        restarted.read('demo', 'run_1', first)


def test_legacy_hash_files_remain_readable_without_index(tmp_path):
    artifacts = Artifacts(tmp_path)
    raw = b'{"legacy": true}'
    reference = digest(raw)+'.json'
    path = artifacts._path('demo', 'run_old', reference)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    assert artifacts.json('demo', 'run_old', reference) == {'legacy': True}
    fresh = artifacts.put('demo', 'run_old', {'new': True}, label='修复报告数据')
    assert artifacts.json('demo', 'run_old', reference) == {'legacy': True}
    assert artifacts.json('demo', 'run_old', fresh) == {'new': True}


@pytest.mark.parametrize('reference', ['../0001_报告.json', '0001_报告.json/../../secret',
                                       '文件索引.json', '0001_报告.exe', '0001_报告:secret.json'])
def test_readable_reference_rejects_unsafe_paths(tmp_path, reference):
    with pytest.raises(PermissionError):
        Artifacts(tmp_path).read('demo', 'run_1', reference)


def test_readable_file_requires_index_entry(tmp_path):
    artifacts = Artifacts(tmp_path)
    reference = artifacts.put('demo', 'run_1', 'data', 'txt', label='记录')
    (tmp_path/'demo'/'run_1'/'文件索引.json').unlink()
    with pytest.raises(ValueError, match='完整性'):
        artifacts.read('demo', 'run_1', reference)


def test_report_chinese_links_escaping_and_collapsed_hashes():
    reference = '0001_探索_模型调用001_尝试1_输出.json'
    page = report_page({'goal': '<script>alert(1)</script>', 'outcome': 'FIX_VERIFIED',
                        'model_exchange_refs': [reference], 'patch_hash': 'f'*64})
    assert '修复已通过本地验证' in page
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert f'href="{quote(reference, safe="")}"' in page
    assert 'f'*64 not in page.split('<details>')[0]
    assert 'f'*64 in page.split('<details>')[1]


def test_report_provenance_links_nested_refs_without_linking_arbitrary_text():
    reference = '0001_诊断_源码<片段>.json'
    ordinary = 'https://example.invalid/<script>'
    page = report_page({'provenance': {'evidence_refs': [reference],
        '<script>': {'source_ref': reference, 'empty_ref': '',
            'records': [{'verification_refs': [reference], 'description': ordinary}]}}})
    assert f'href="{quote(reference, safe="")}"' in page.split('<details>')[1]
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert '源码&lt;片段&gt;' in page
    assert 'href=""' not in page and f'href="{quote(ordinary, safe="")}"' not in page


def test_report_provenance_links_only_intact_current_run_artifacts(tmp_path):
    artifacts = Artifacts(tmp_path)
    reference = artifacts.put('demo', 'run_1', {'file': 'src/value.ts'}, label='源码片段')
    other = artifacts.put('other', 'run_2', {'file': 'private.ts'}, label='另一项源码')
    missing = '9999_缺失源码.json'
    unsafe = '../0001_报告.json'
    page = report_page({'scope_id': 'demo', 'run_id': 'run_1',
        'provenance': {'evidence_refs': [reference, other, missing, unsafe]}}, artifacts=artifacts)
    assert f'href="{quote(reference, safe="")}"' in page
    for invalid in (other, missing, unsafe):
        assert f'href="{quote(invalid, safe="")}"' not in page
        assert invalid in page
    artifacts._path('demo', 'run_1', reference).write_bytes(b'corrupt')
    damaged = report_page({'scope_id': 'demo', 'run_id': 'run_1',
        'provenance': {'evidence_refs': [reference]}}, artifacts=artifacts)
    assert f'href="{quote(reference, safe="")}"' not in damaged
    unidentified = report_page({'provenance': {'evidence_refs': [reference]}}, artifacts=artifacts)
    assert f'href="{quote(reference, safe="")}"' not in unidentified
    assert reference in unidentified


async def test_complete_run_outputs_linked_chinese_report_and_logs(tmp_path):
    engine, state = make_engine(tmp_path)
    await engine.run(state)
    state = engine.store.load(state.run_id, 'b')
    engine.store.decide_approval(state.approval_ref, state, 'reject')
    await engine.run(resume='reject')
    state = engine.store.load(state.run_id, 'b')
    assert state.report_ref.endswith('_收尾_修复报告数据.json')
    events = engine.store.trace(state.run_id, 'b')
    finished = next(event for event in events if event['type'] == 'run.finished')
    page = engine.artifacts.read('b', state.run_id, finished['payload']['html_ref']).decode('utf-8')
    assert '候选补丁差异' in page and '修复已通过本地验证' in page
    for reference in state.model_exchange_refs + state.validation_refs + state.evidence_refs:
        assert f'href="{quote(reference, safe="")}"' in page
        assert engine.artifacts.exists('b', state.run_id, reference)
    folder = engine.artifacts._path('b', state.run_id, state.report_ref).parent
    files = list(folder.iterdir())
    assert all(not re.fullmatch(r'[a-f0-9]{64}\.[a-z]+', path.name) for path in files)
    text_log = next(path for path in files if path.name.endswith('_中文事件日志.txt'))
    assert '运行结束' in text_log.read_text(encoding='utf-8')
    data_log = next(path for path in files if path.name.endswith('_完整事件数据.json'))
    assert json.loads(data_log.read_text(encoding='utf-8'))[-1]['type'] == 'run.finished'
