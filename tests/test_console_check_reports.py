from tracefix.console import dispatch
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.storage.artifacts import Artifacts


def test_console_publishes_check_plan_results_and_images(tmp_path):
    artifacts = Artifacts(tmp_path / 'artifacts')
    scope, run_id = 'alpha', 'run_checks'
    screenshot = artifacts.put(scope, run_id, b'\x89PNG\r\n\x1a\nimage', 'png', label='页面截图')
    evidence = artifacts.put(scope, run_id, {'screenshot_ref': screenshot}, label='检查证据')
    check = {'id': 'goal', 'name': '保存反馈', 'criteria': '保存后显示反馈', 'source': 'user_goal'}
    result = {**check, 'status': 'fail', 'evidence_refs': [evidence]}
    result_ref = artifacts.put(scope, run_id, result, label='逐项检查结果')
    plan_ref = artifacts.put(scope, run_id, {'items': [check]}, label='冻结检查计划')
    report = {'run_id': run_id, 'scope_id': scope, 'issues': [], 'check_plan': [check],
              'check_results': [result], 'check_plan_ref': plan_ref, 'check_result_refs': [result_ref],
              'initial_check_results': [result], 'initial_check_result_refs': [result_ref],
              'check_summary': {'total': 1, 'failed': 1}, 'overall_status': 'FAILED',
              'images': [{'ref': screenshot, 'mime': 'image/png'}]}
    report_ref = artifacts.put(scope, run_id, report, label='修复报告数据')
    library = DocumentLibrary(tmp_path / 'console.sqlite3')
    library.update_run('console_checks', {'projectId': scope, 'agentRunId': run_id,
                       'reportRef': report_ref, 'goal': '保存反馈', 'mode': 'test', 'status': 'failed'}, create=True)
    detail = dispatch('run', {'id': 'console_checks'}, library=library, data_root=tmp_path)
    assert detail['issueReport'] == report
    for key in ('check_plan', 'check_results', 'check_summary', 'overall_status', 'images', 'initial_check_results'):
        assert detail[key] == report[key]
    assert {screenshot, evidence, result_ref, plan_ref, report_ref} <= {item['ref'] for item in detail['artifacts']}
