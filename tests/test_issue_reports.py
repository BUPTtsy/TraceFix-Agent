import json
from urllib.parse import quote

import pytest

from tracefix.cli.render import Renderer
from tracefix.console import dispatch
from tracefix.knowledge.documents import DocumentLibrary
from tracefix.rules import Rule, RuleLibrary, RuleResolver
from tracefix.runtime.contracts import BrowserAction, Decision, GUIIssue, Phase, RunState
from tracefix.runtime.smoke import make_engine
from tracefix.storage.artifacts import Artifacts
from tracefix.storage.presentation import report_page
from tracefix.storage.reporting import collect_issues, report_text


@pytest.mark.parametrize('mode,expected_status', [('test', 'confirmed'), ('repair', 'fixed')])
async def test_run_explains_actual_bug_in_terminal_web_and_html(tmp_path, mode, expected_status):
    engine, state = make_engine(tmp_path)
    state.mode = mode
    renderer = Renderer(plain=True)
    renderer.artifacts = engine.artifacts
    renderer.scope, renderer.run_id = state.scope_id, state.run_id
    engine.notify = renderer.event
    with renderer.console.capture() as capture:
        await engine.run(state)
        current = engine.store.load(state.run_id, state.scope_id)
        if current.approval_ref:
            engine.store.decide_approval(current.approval_ref, current, 'reject')
            await engine.run(resume='reject')
    current = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(current, current.report_ref)
    assert len(report['issues']) == 1
    issue = report['issues'][0]
    assert issue['status'] == expected_status
    assert issue['location'] == 'http://app:3000'
    assert 'Complete task' in issue['actual'] and '未选中' in issue['actual']
    assert '保持选中' in issue['expected']
    assert issue['steps'] == ['打开页面 http://app:3000']
    assert len(issue['evidence_refs']) >= 2
    output = capture.get()
    assert '问题报告' in output and '未选中' in output and '保持选中' in output
    assert '"action"' not in output
    finished = next(event for event in engine.store.trace(state.run_id, state.scope_id) if event['type'] == 'run.finished')
    page = engine.artifacts.read(state.scope_id, state.run_id, finished['payload']['html_ref']).decode()
    assert '问题清单' in page and '未选中' in page and '复现步骤' in page
    assert ('测试报告' if mode == 'test' else '修复报告') in page
    assert f'href="{quote(issue["evidence_refs"][0], safe="")}"' in page
    library = DocumentLibrary(tmp_path / 'console.sqlite3')
    library.update_run('console_run', {'projectId': state.scope_id, 'agentRunId': state.run_id,
        'reportRef': current.report_ref, 'goal': state.goal, 'mode': mode, 'status': 'completed'}, create=True)
    detail = dispatch('run', {'id': 'console_run'}, library=library, data_root=tmp_path)
    assert detail['issueReport']['issues'] == report['issues']
    assert set(issue['evidence_refs']) <= {entry['ref'] for entry in detail['artifacts']}
    segments = [engine.get(current, entry['ref']) for entry in detail['artifacts'] if entry['label'].endswith('_阶段轨迹')]
    assert [event for segment in segments for event in segment['events']] == engine.store.trace(state.run_id, state.scope_id)


async def test_rule_findings_are_included_without_claiming_run_wide_fix(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    engine.rule_library = RuleLibrary(tmp_path / 'rules.sqlite3')
    engine.rule_library.save_rule(Rule(id='console_check', name='页面不能有控制台错误', status='enabled',
        detection={'type': 'oracle', 'oracle': {'kind': 'console_no_error'}}))
    engine.rule_resolver = RuleResolver(engine.rule_library)
    state.mode, state.phase = 'test', Phase.EXPLORE
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    observation = await engine.browser.action(BrowserAction(kind='observe'))
    observation['console'] = [{'level': 'error', 'message': '<script>保存任务失败</script>'}]
    reference = await engine.capture(state, observation)
    report = collect_issues(state, engine.store.trace(state.run_id, state.scope_id), lambda ref: engine.get(state, ref))
    issue = next(item for item in report['issues'] if item.get('rule_id') == 'console_check')
    assert all(item['status'] == 'suspected' for item in report['issues'])
    assert issue['evidence_refs'] == [reference]
    assert issue['actual'] == '<script>保存任务失败</script>'
    page = report_page(report)
    assert '<script>' not in page and '&lt;script&gt;保存任务失败' in page


async def test_dom_rule_describes_count_and_role_only_locator(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    engine.rule_library = RuleLibrary(tmp_path / 'rules.sqlite3')
    engine.rule_library.save_rule(Rule(id='checkbox_count', name='任务数量检查', status='enabled',
        detection={'type': 'oracle', 'oracle': {'kind': 'dom_assertion', 'locator': {'role': 'checkbox'}, 'count': 2}}))
    engine.rule_resolver = RuleResolver(engine.rule_library)
    state.phase = Phase.EXPLORE
    engine.store.save(state)
    state = engine.ensure_rule_snapshot(state)
    await engine.capture(state, await engine.browser.action(BrowserAction(kind='observe')))
    report = collect_issues(state, engine.store.trace(state.run_id, state.scope_id), lambda ref: engine.get(state, ref))
    issue = next(item for item in report['issues'] if item.get('rule_id') == 'checkbox_count')
    assert '匹配数量应为 2' in issue['expected']
    assert '当前匹配 1 个' in issue['actual']
    assert '且唯一' not in issue['expected']


async def test_passing_run_reports_coverage_without_inventing_issues(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    state.mode = 'test'
    await engine.run(state)
    current = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(current, current.report_ref)
    assert report['issues'] == []
    assert '不代表所有页面和功能均无问题' in report_text(report)
    assert report['tested_urls'] == ['http://app:3000']


async def test_model_gui_issues_survive_final_report_as_suspected_with_real_evidence(tmp_path):
    engine, state = make_engine(tmp_path, bugfree=True)
    state.mode = 'test'
    generate = engine.model.generate

    async def model(schema, context, **options):
        result = await generate(schema, context, **options)
        if schema is Decision:
            result.value.issues = [GUIIssue(title='任务操作缺少文字反馈', expected='操作后显示明确的结果提示',
                actual='当前页面快照中未见操作结果提示', evidence_refs=[context['observation']['id']]),
                GUIIssue(title='无证据的猜测', expected='预期表现', actual='假设表现', evidence_refs=['invented.json'])]
        return result

    engine.model.generate = model
    await engine.run(state)
    current = engine.store.load(state.run_id, state.scope_id)
    report = engine.get(current, current.report_ref)
    assert len(report['issues']) == 1
    issue = report['issues'][0]
    assert issue['title'] == '任务操作缺少文字反馈'
    assert issue['status'] == 'suspected' and issue['source'] == 'guided'
    assert issue['actual'] == '当前页面快照中未见操作结果提示'
    assert '未经独立复现' in issue['verification']
    assert issue['steps'] == ['打开页面 http://app:3000']
    assert all(engine.artifacts.exists(state.scope_id, state.run_id, ref) for ref in issue['evidence_refs'])


def test_single_failure_and_runtime_errors_are_not_confirmed_gui_bugs():
    state = RunState(scope_id='demo', goal='检查前端页面', url='http://app:3000')
    records = {'check.json': {'observation_ref': 'observation.json', 'assertions': [
        {'assertion': {'locator': {'role': 'button', 'name': '保存'}, 'condition': 'enabled'}, 'passed': False, 'matches': 1}]},
        'observation.json': {'url': state.url, 'snapshot': '- button "保存" [disabled]'}}
    events = [{'type': 'run.error', 'phase': 'EXPLORE', 'payload': {'error': '网络不可用'}},
              {'type': 'gate.decided', 'phase': 'EXPLORE', 'payload': {'evidence_ref': 'check.json'}}]
    report = collect_issues(state, events, records.__getitem__)
    assert len(report['issues']) == 1
    assert report['issues'][0]['status'] == 'suspected'
    assert '处于禁用状态' in report['issues'][0]['actual']
    assert '网络不可用' not in report_text(report)


@pytest.mark.parametrize('changed_field,changed_value', [('patch_hash', 'old-patch'),
    ('environment_digest', 'old-environment'), ('test_spec_hash', 'old-spec'), ('source_manifest', 'old-source')])
def test_verified_run_does_not_promote_issue_using_stale_validation(changed_field, changed_value):
    state = RunState(scope_id='demo', goal='验证保存按钮', url='http://app:3000', outcome='FIX_VERIFIED',
        patch_hash='current-patch', environment_digest='current-environment', test_spec_hash='current-spec',
        source_manifest='current-source', validation_refs=['validation.json'])
    assertion = {'locator': {'role': 'button', 'name': '保存'}, 'condition': 'enabled'}
    records = {'failure.json': {'observation_ref': 'observation.json', 'assertions': [
        {'assertion': assertion, 'passed': False, 'matches': 1}]},
        'observation.json': {'url': state.url, 'snapshot': '- button "保存" [disabled]'},
        'success.json': {'observation_ref': 'observation.json', 'assertions': [{'assertion': assertion, 'passed': True}]},
        'validation.json': {'kind': 'original', 'passed': True, 'artifact_ref': 'success.json',
            'patch_hash': state.patch_hash, 'environment_digest': state.environment_digest,
            'test_spec_hash': state.test_spec_hash, 'source_manifest': state.source_manifest}}
    records['validation.json'][changed_field] = changed_value
    report = collect_issues(state, [{'type': 'gate.decided', 'phase': 'EXPLORE',
        'payload': {'evidence_ref': 'failure.json'}}], records.__getitem__)
    assert report['issues'][0]['status'] == 'suspected'
    assert 'validation.json' not in report['issues'][0]['evidence_refs']


def test_phase_trace_appends_and_reentry_creates_new_segment_after_restart(tmp_path):
    artifacts = Artifacts(tmp_path)
    phases = ['EXPLORE', 'EXPLORE', 'REPRODUCE', 'DIAGNOSE', 'EXPLORE', 'VERIFY']
    references = []
    for sequence, phase in enumerate(phases, 1):
        event = {'scope_id': 'demo', 'run_id': 'run_1', 'phase': phase, 'seq': sequence,
                 'type': 'tool.completed', 'payload': {'action': str(sequence), 'password': 'private'}}
        artifacts = Artifacts(tmp_path)
        references.append(artifacts.append_trace(event))
    assert references[0] == references[1]
    assert len(set(references)) == 5
    assert references[0] != references[4]
    assert artifacts.trace_position('demo', 'run_1') == 6
    first = artifacts.json('demo', 'run_1', references[0])
    assert [event['seq'] for event in first['events']] == [1, 2]
    assert first['end_seq'] == 2 and first['start_seq'] == 1
    assert 'private' not in json.dumps(first)
    assert artifacts.append_trace(event) == references[-1]
    assert len(artifacts.json('demo', 'run_1', references[-1])['events']) == 1
    assert all(artifacts.exists('demo', 'run_1', reference) for reference in references)
    with pytest.raises(ValueError, match='不连续'):
        artifacts.append_trace({**event, 'seq': 8})
    with pytest.raises(ValueError, match='冲突'):
        artifacts.append_trace({**event, 'payload': {'changed': True}})


async def test_engine_recovery_collects_unnotified_events_in_order(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    engine.event(state, 'run.started')
    engine.store.event(state, 'input.applied', {'instruction': '补充说明'})
    engine.artifacts = Artifacts(engine.artifacts.root)
    engine._trace_sequences.clear()
    engine.event(state, 'tool.started', {'operation_id': 'test'})
    index = engine.artifacts._index(state.scope_id, state.run_id)
    segments = [engine.get(state, reference) for reference, entry in index.items() if entry['用途'].endswith('_阶段轨迹')]
    assert len(segments) == 1
    assert segments[0]['events'] == engine.store.trace(state.run_id, state.scope_id)
