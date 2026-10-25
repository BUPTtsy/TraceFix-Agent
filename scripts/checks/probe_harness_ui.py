from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:5179')
    parser.add_argument('--output', default='artifacts/harness-migration/ui-probe')
    options = parser.parse_args()
    output = Path(options.output)
    output.mkdir(parents=True, exist_ok=True)
    run = {'id': 'console-sdk', 'taskId': 'task-sdk', 'agentRunId': 'console-sdk',
        'current_run_id': 'increment-two', 'projectId': 'probe', 'goal': '冻结 SDK 测试目标',
        'mode': 'repair', 'status': 'stopped', 'phase': 'DIAGNOSE',
        'startedAt': '2026-10-20T00:00:00Z', 'artifacts': [],
        'coding_backend': 'claude_sdk', 'control_status': 'STOPPED',
        'can_resume': True, 'can_stop': False, 'resume_blockers': [],
        'accepted_commit': 'a' * 40, 'quality': 'IN_PROGRESS'}
    requests = []

    def respond(route):
        request = route.request
        path = request.url.split('/api', 1)[1].split('?', 1)[0]
        if request.method == 'POST':
            requests.append({'path': path, 'body': request.post_data_json,
                'idempotency_key': request.headers.get('idempotency-key')})
            assert path == '/v1/tasks/task-sdk/resume'
            assert request.post_data_json == {}
            assert request.headers.get('idempotency-key')
            run.update(status='running', control_status='RUNNING', can_resume=False, can_stop=True)
            value = {'task': {'id': 'task-sdk'}, 'run': run}
        elif path == '/projects':
            value = [{'id': 'probe', 'ready': True, 'runCount': 1}]
        elif path == '/services':
            value = [{'id': 'repair', 'label': 'Repair 修复'}, {'id': 'chat', 'label': 'Chat 咨询'}]
        elif path == '/runs':
            value = [run]
        elif path == '/agent/status':
            value = {'running': False}
        elif path == '/v1/tasks':
            value = [{'id': 'task-sdk', 'status': run['status'], 'currentRunId': run['id'],
                'updatedAt': run['startedAt']}]
        elif path == '/runs/console-sdk':
            value = run
        elif path.endswith('/result'):
            value = {'runId': run['id'], 'ready': False, 'outcome': None}
        else:
            value = []
        route.fulfill(json=value)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', respond)
        page.goto(options.url + '/#runs')
        page.wait_for_load_state('networkidle')
        page.get_by_role('button', name='冻结 SDK 测试目标').click()
        expect(page.get_by_role('button', name='继续 SDK Job')).to_be_visible()
        expect(page.get_by_label('补充引导', exact=True)).to_have_count(0)
        expect(page.get_by_text('increment-two', exact=True)).to_be_visible()
        expect(page.get_by_text('a' * 40, exact=True)).to_be_visible()
        page.screenshot(path=str(output / 'stopped.png'), full_page=True)
        page.get_by_role('button', name='继续 SDK Job').click()
        expect(page.get_by_role('button', name='继续 SDK Job')).to_have_count(0)
        assert len(requests) == 1
        run.update(status='blocked', control_status='BLOCKED', can_resume=False,
            can_stop=False, resume_blockers=['EXTERNAL_OPERATION_UNKNOWN'])
        page.reload()
        page.wait_for_load_state('networkidle')
        page.get_by_role('button', name='冻结 SDK 测试目标').click()
        expect(page.get_by_text('恢复受阻：EXTERNAL_OPERATION_UNKNOWN')).to_be_visible()
        expect(page.get_by_role('button', name='继续 SDK Job')).to_have_count(0)
        page.screenshot(path=str(output / 'blocked-after-refresh.png'), full_page=True)
        assert not errors, errors
        browser.close()
    report = {'passed': True, 'mode': 'real_browser_mocked_api', 'requests': requests,
        'checks': ['stopped_resume', 'empty_body', 'idempotency_key', 'no_guidance_form',
            'current_increment', 'accepted_commit', 'blocked_after_refresh'],
        'limitations': ['未调用真实模型或真实服务停止恢复']}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
