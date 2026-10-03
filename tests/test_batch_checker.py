import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from tracefix.storage.artifacts import Artifacts


def test_real_e2e_failure_preserves_batch_report_and_patch_refs(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    module = runpy.run_path(str(root / 'tools/checks/verify_real_e2e.py'))
    spec = tmp_path / 'persistence.spec.json'
    spec.write_bytes((root / 'profiles/persistence.spec.json').read_bytes())
    run_flow = module['run_flow']
    commands = []
    state = SimpleNamespace(run_status='FAILED', outcome='REPAIR_EXHAUSTED',
                            report_ref=None, error='连续三轮无有效补丁')

    def command(arguments, **options):
        arguments = [str(argument) for argument in arguments]
        commands.append(arguments)
        if 'bugboard/scripts/init_demo.py' in arguments:
            Path(arguments[-1]).mkdir(parents=True)
        if '--run' in arguments:
            assert arguments[arguments.index('--execution-mode') + 1] == 'batch'
            frozen = Path(arguments[arguments.index('--spec') + 1])
            assert frozen != spec
            assert frozen.read_bytes() == spec.read_bytes()
            assert options['check'] is False
            data_root = Path(arguments[arguments.index('--data') + 1])
            artifacts = Artifacts(data_root / 'artifacts')
            diff_ref = artifacts.put('bugboard', 'run_batch_fixture', '', 'diff')
            state.report_ref = artifacts.put('bugboard', 'run_batch_fixture', {
                'patch_diff_ref': diff_ref, 'patch_available': False,
                'patch_verification': 'none', 'result_summary': '多次追问仍无有效补丁',
                'error_details': {'terminal_reason': 'diagnosis_retry_limit'}})
            return SimpleNamespace(returncode=1, stdout='BATCH_RESULT: {}', stderr='')
        return SimpleNamespace(returncode=0, stdout='existing-postgres' if 'ps' in arguments else '', stderr='')

    monkeypatch.setitem(run_flow.__globals__, 'ROOT', tmp_path)
    monkeypatch.setitem(run_flow.__globals__, 'command', command)
    monkeypatch.setitem(run_flow.__globals__, 'load_state', lambda *args: state)
    output = tmp_path / 'batch-report.json'
    with pytest.raises(RuntimeError, match='连续三轮无有效补丁'):
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
    assert not any('/approve' in argument for arguments in commands for argument in arguments)
    assert not any('down' in arguments for arguments in commands)
