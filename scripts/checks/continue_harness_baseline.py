from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'artifacts/harness-migration'
PROGRESS = EVIDENCE / 'pytest-progress.jsonl'


class BaselineProgress:
    def pytest_collection_modifyitems(self, config, items):
        collected = (EVIDENCE / 'baseline-collected.txt').read_text(encoding='utf-8-sig').splitlines()
        original = [line for line in collected if line.startswith('tests/') and '::' in line]
        boundary = next(index for index, identity in enumerate(original)
            if identity.startswith('tests/test_check_suite_execution.py::')) + 15
        complete = set(original[:boundary])
        if PROGRESS.exists():
            for line in PROGRESS.read_text(encoding='utf-8').splitlines():
                record = json.loads(line)
                if record['when'] == 'teardown' or record['outcome'] == 'failed':
                    complete.add(record['nodeid'])
        selected = [item for item in items if item.nodeid not in complete]
        deselected = [item for item in items if item.nodeid in complete]
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)
        (EVIDENCE / 'pytest-continuation-selection.json').write_text(json.dumps({
            'previous_console_confirmed_passes': boundary, 'deselected': len(deselected),
            'remaining': len(selected)}, indent=2), encoding='utf-8')

    def pytest_runtest_logreport(self, report):
        with PROGRESS.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'nodeid': report.nodeid, 'when': report.when,
                'outcome': report.outcome, 'duration': report.duration}) + '\n')
            handle.flush()
            os.fsync(handle.fileno())


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'backend/packages/agent/src'))
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / 'pytest-continuation.log').open('w', encoding='utf-8', buffering=1) as output:
        sys.stdout = output
        sys.stderr = output
        (EVIDENCE / 'pytest-continuation-exit.json').write_text(
            json.dumps({'status': 'running'}), encoding='utf-8')
        baseline_temp = Path(tempfile.gettempdir()) / 'tracefix-harness-baseline'
        status = pytest.main(['-q', '-p', 'no:cacheprovider',
            '--basetemp=' + str(baseline_temp), '--junitxml=artifacts/harness-migration/pytest-continuation.xml'],
            plugins=[BaselineProgress()])
        (EVIDENCE / 'pytest-continuation-exit.json').write_text(
            json.dumps({'exit_code': int(status)}), encoding='utf-8')
    return int(status)


if __name__ == '__main__':
    raise SystemExit(main())
