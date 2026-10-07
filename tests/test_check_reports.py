import base64
import json

import pytest

from tracefix.runtime.contracts import digest
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.presentation import report_page
from tracefix.storage.reporting import collect_report_images, summarize_checks, with_check_report


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


def test_check_report_preserves_legacy_issues_and_unifies_goal_with_rules():
    plan = [
        {'id': 'rule_console', 'name': 'Console 无错误', 'criteria': '不得有 Error', 'source': 'rule', 'severity': 'blocker'},
        {'id': 'goal_refresh', 'name': '刷新后完成状态保持', 'criteria': '刷新后仍选中', 'source': 'user_goal'},
    ]
    results = [{**plan[0], 'status': 'fail', 'actual': '<script>Error</script>'},
               {**plan[1], 'status': 'pass', 'actual': 'checked'}]
    legacy = {'mode': 'test', 'issues': [{'id': 'old', 'title': '旧问题', 'status': 'suspected'}]}
    report = with_check_report(legacy, check_plan=plan, check_results=results)
    assert report['issues'] == legacy['issues']
    assert report['overall_status'] == 'FAILED'
    assert report['check_summary']['total'] == 2
    assert report['check_summary']['executed'] == 2
    assert report['check_summary']['passed'] == 1
    assert report['check_summary']['blocker_failed'] == 1
    assert report['check_summary']['coverage_complete'] is True
    assert report['check_results'][1]['source'] == 'user_goal'
    assert report['check_results'][1]['status'] == 'pass'
    page = report_page(report)
    assert '逐项检查' in page and '刷新后完成状态保持' in page
    assert '用户目标' in page and '旧问题' in page
    assert '<script>' not in page and '&lt;script&gt;Error&lt;/script&gt;' in page
    assert 'check_plan' not in legacy


@pytest.mark.parametrize('status', ['fail', 'error', 'inconclusive', 'skipped'])
def test_blocking_check_must_have_a_pass_result(status):
    summary, overall = summarize_checks([{'id': 'first', 'severity': 'blocker'}],
                                       [{'id': 'first', 'severity': 'blocker', 'status': status}])
    assert overall == 'FAILED'
    assert summary['blocker_failed'] == 1


def test_missing_check_is_not_a_passing_suite():
    summary, overall = summarize_checks([{'id': 'first'}, {'id': 'second'}],
                                       [{'id': 'first', 'status': 'pass'}])
    assert overall == 'INCONCLUSIVE'
    assert summary['coverage_complete'] is False
    assert summary['missing_ids'] == ['second']


def test_missing_blocker_fails_but_critical_findings_do_not_block():
    summary, overall = summarize_checks([{'id': 'first', 'severity': 'blocker'}], [])
    assert overall == 'FAILED'
    assert summary['blocker_failed'] == 1
    summary, overall = summarize_checks([{'id': 'first', 'severity': 'critical'}],
                                       [{'id': 'first', 'severity': 'critical', 'status': 'failed'}])
    assert overall == 'PASSED_WITH_FINDINGS'
    assert summary['failed'] == 1


def test_json_images_keep_metadata_only():
    report = with_check_report({}, images=[{'ref': '0001_截图.png', 'hash': digest(PNG),
        'mime': 'image/png', 'alt': '检查截图', 'data': base64.b64encode(PNG).decode(), 'url': 'file:///secret'}])
    assert set(report['images'][0]) == {'ref', 'hash', 'mime', 'alt'}
    assert 'data:image' not in json.dumps(report)
    assert 'file:///secret' not in json.dumps(report)


def test_html_embeds_png_read_through_current_run_artifacts(tmp_path):
    artifacts = Artifacts(tmp_path)
    screenshot = artifacts.put('demo', 'run_1', PNG, 'png')
    observation = artifacts.put('demo', 'run_1', {'scope_id': 'demo', 'run_id': 'run_1',
        'url': 'http://app:3000', 'screenshot_ref': screenshot, 'screenshot_hash': digest(PNG)})
    evidence = artifacts.put('demo', 'run_1', {'observation_ref': observation})
    report = {'scope_id': 'demo', 'run_id': 'run_1', 'mode': 'test',
              'check_results': [{'id': 'goal', 'name': '保存反馈', 'status': 'passed', 'evidence_refs': [evidence]}]}
    images, errors = collect_report_images(report, artifacts, 'demo', 'run_1')
    assert errors == []
    assert images == [{'ref': screenshot, 'hash': digest(PNG), 'mime': 'image/png',
                       'alt': '页面证据截图：http://app:3000'}]
    page = report_page({**report, 'images': images}, artifacts=artifacts)
    assert 'img-src \'self\' data:' in page
    assert 'src="data:image/png;base64,' + base64.b64encode(PNG).decode() + '"' in page
    assert '页面证据截图：http://app:3000' in page


def test_initial_check_results_contribute_images_and_render_before_after_tables(tmp_path):
    artifacts = Artifacts(tmp_path)
    screenshot = artifacts.put('demo', 'run_1', PNG, 'png')
    evidence = artifacts.put('demo', 'run_1', {'screenshot_ref': screenshot,
        'url': 'http://app:3000/before', 'screenshot_hash': digest(PNG)})
    item = {'id': 'goal', 'name': '保存反馈', 'criteria': '保存后显示反馈',
            'source': 'user_goal', 'severity': 'blocker', 'detector': 'model'}
    report = {'scope_id': 'demo', 'run_id': 'run_1', 'mode': 'repair',
              'check_plan': [item],
              'initial_check_results': [{**item, 'status': 'fail', 'actual': '修复前缺少反馈',
                                         'evidence_refs': [evidence]}],
              'check_results': [{**item, 'status': 'pass', 'stage': 'verify', 'actual': '修复后已显示反馈',
                                 'evidence_refs': []}]}
    images, errors = collect_report_images(report, artifacts, 'demo', 'run_1')
    assert errors == []
    assert images == [{'ref': screenshot, 'hash': digest(PNG), 'mime': 'image/png',
                       'alt': '页面证据截图：http://app:3000/before'}]
    page = report_page({**report, 'images': images}, artifacts=artifacts)
    assert '修复前检查结果' in page and '修复后检查结果' in page
    assert '修复前缺少反馈' in page and '修复后已显示反馈' in page
    assert 'data:image/png;base64,' in page


def test_repair_before_verification_does_not_label_initial_results_as_fixed():
    result = {'id': 'rule', 'name': '检查', 'severity': 'critical', 'status': 'fail', 'actual': '发现问题'}
    page = report_page({'mode': 'repair', 'check_results': [result], 'initial_check_results': [result]})
    assert '初始检查结果' in page and '当前检查结果' in page
    assert '修复后检查结果' not in page
    assert '严重' in page


@pytest.mark.parametrize('damage', ['missing', 'hash', 'not_png', 'foreign', 'unsafe'])
def test_invalid_image_evidence_is_reported_without_embedding(tmp_path, damage):
    artifacts = Artifacts(tmp_path)
    target_run = 'foreign_run' if damage == 'foreign' else 'run_1'
    raw = b'not a PNG' if damage == 'not_png' else PNG
    screenshot = artifacts.put('demo', target_run, raw, 'png')
    if damage == 'missing':
        artifacts._path('demo', target_run, screenshot).unlink()
    if damage == 'unsafe':
        screenshot = '../' + screenshot
    expected_hash = 'f' * 64 if damage == 'hash' else digest(raw)
    report = {'scope_id': 'demo', 'run_id': 'run_1',
              'images': [{'ref': screenshot, 'hash': expected_hash, 'mime': 'image/png', 'alt': '截图'}]}
    images, errors = collect_report_images(report, artifacts, 'demo', 'run_1')
    assert images == [] and errors
    page = report_page(report, artifacts=artifacts)
    assert '图片证据无法读取' in page
    assert 'src="data:image' not in page
