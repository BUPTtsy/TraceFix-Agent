import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from tracefix.storage.artifacts import Artifacts


def test_exception_group_preserves_nested_failure_reasons():
    from tracefix.messages import error_message

    error = ExceptionGroup('unhandled errors in a TaskGroup', [
        ExceptionGroup('worker', [TimeoutError('provider timed out')]),
        RuntimeError('missing browser capability'),
    ])

    message = error_message(error)

    assert 'TimeoutError: 操作超时：provider timed out' in message
    assert 'RuntimeError: 运行失败：missing browser capability' in message


def test_exception_group_validation_error_does_not_expose_input():
    from pydantic import ValidationError
    from tracefix.messages import error_message
    from tracefix.runtime.contracts import BrowserAction

    with pytest.raises(ValidationError) as caught:
        BrowserAction.model_validate({'kind': 'invalid-action', 'secret': 'private-value'})

    message = error_message(ExceptionGroup('worker', [caught.value]))

    assert 'ValidationError: 数据校验失败' in message
    assert 'private-value' not in message


def test_exception_traceback_preserves_all_groups_and_chained_causes():
    from tracefix.messages import exception_traceback

    try:
        try:
            raise ConnectionError('original provider failure')
        except ConnectionError as cause:
            raise RuntimeError('wrapped provider failure') from cause
    except RuntimeError as error:
        nested = error
        for level in range(12):
            nested = ExceptionGroup(f'level-{level}', [nested])
        failure = ExceptionGroup('all workers', [nested] + [ValueError(f'worker-{index}') for index in range(20)])

    formatted = exception_traceback(failure)

    assert 'Traceback (most recent call last)' in formatted
    assert 'test_exception_traceback_preserves_all_groups_and_chained_causes' in formatted
    assert 'ConnectionError: original provider failure' in formatted
    assert 'The above exception was the direct cause' in formatted
    assert 'RuntimeError: wrapped provider failure' in formatted
    assert 'ValueError: worker-19' in formatted
    assert 'max_group_depth' not in formatted
    assert 'and 6 more exceptions' not in formatted


@pytest.mark.parametrize('nested_failure', [False, True])
def test_real_e2e_failure_preserves_batch_report_and_patch_refs(tmp_path, monkeypatch, nested_failure):
    root = Path(__file__).resolve().parents[1]
    module = runpy.run_path(str(root / 'scripts/checks/verify_real_e2e.py'))
    spec = tmp_path / 'persistence.spec.json'
    spec.write_bytes((root / 'profiles/persistence.spec.json').read_bytes())
    run_flow = module['run_flow']
    commands = []
    state = SimpleNamespace(run_status='FAILED', outcome='REPAIR_EXHAUSTED',
                            report_ref=None, error='连续三轮无有效补丁')
    details = {'terminal_reason': 'diagnosis_retry_limit'}
    if nested_failure:
        from tracefix.messages import exception_traceback

        try:
            raise ExceptionGroup('unhandled errors in a TaskGroup', [
                RuntimeError('root failure before long diagnostic ' + 'detail ' * 1000 + ' end of root failure')])
        except ExceptionGroup as error:
            details['exception_traceback'] = exception_traceback(error)

    def command(arguments, **options):
        arguments = [str(argument) for argument in arguments]
        commands.append(arguments)
        if 'bugboard/scripts/init_demo.py' in arguments:
            Path(arguments[-1]).mkdir(parents=True)
        if 'tracefix.service.web_run' in arguments:
            request = json.loads(options['input'])
            assert request['mode'] == 'repair'
            frozen = Path(request['spec'])
            assert frozen != spec
            assert frozen.read_bytes() == spec.read_bytes()
            assert options['check'] is False
            data_root = Path(request['data'])
            artifacts = Artifacts(data_root / 'artifacts')
            diff_ref = artifacts.put('bugboard', 'run_batch_fixture', '', 'diff')
            state.report_ref = artifacts.put('bugboard', 'run_batch_fixture', {
                'patch_diff_ref': diff_ref, 'patch_available': False,
                'patch_verification': 'none', 'result_summary': '多次追问仍无有效补丁',
                'error_details': details})
            return SimpleNamespace(returncode=1, stdout='stdout start ' + 'data ' * 1000,
                                   stderr='stderr start ' + 'error ' * 1000)
        return SimpleNamespace(returncode=0, stdout='existing-postgres' if 'ps' in arguments else '', stderr='')

    monkeypatch.setitem(run_flow.__globals__, 'ROOT', tmp_path)
    monkeypatch.setitem(run_flow.__globals__, 'command', command)
    monkeypatch.setitem(run_flow.__globals__, 'load_state', lambda *args: state)
    output = tmp_path / 'batch-report.json'
    with pytest.raises(RuntimeError, match='root failure before long diagnostic' if nested_failure else '连续三轮无有效补丁'):
        run_flow(SimpleNamespace(case='B01', spec=spec), output)

    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == 'failed'
    assert report['run_id'] == 'run_batch_fixture'
    assert report['outcome'] == 'REPAIR_EXHAUSTED'
    assert report['agent_exit_code'] == 1
    assert report['report_ref'] == state.report_ref
    assert report['patch_diff_ref']
    assert report['patch_available'] is False
    assert report['patch_verification'] == 'none'
    assert report['error_details']['terminal_reason'] == 'diagnosis_retry_limit'
    assert 'Traceback (most recent call last)' in report['exception_traceback']
    assert 'in run_flow' in report['exception_traceback']
    assert Path(report['agent_stdout_log']).read_text(encoding='utf-8') == 'stdout start ' + 'data ' * 1000
    assert Path(report['agent_stderr_log']).read_text(encoding='utf-8') == 'stderr start ' + 'error ' * 1000
    if nested_failure:
        assert 'root failure before long diagnostic' in report['error']
        assert 'end of root failure' in report['error']
        assert 'test_real_e2e_failure_preserves_batch_report_and_patch_refs' in report['error']
    assert not any('/approve' in argument for arguments in commands for argument in arguments)
    assert not any('down' in arguments for arguments in commands)
