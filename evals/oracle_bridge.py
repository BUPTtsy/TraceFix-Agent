"""Final scoring runs only in a trusted evaluator; Agent receives settlement alone."""

import hashlib
import contextlib
import io
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from evals.isolation import HostAgentBoundary, IsolationBoundary, IsolationError, _plain_path


SETTLED_ACKNOWLEDGEMENT = '{"status":"settled"}'


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value) -> str:
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or '')


@dataclass
class OracleBridge:
    private_root: Path
    command: tuple[str, ...]
    oracle_script: Path
    boundary: IsolationBoundary = field(default_factory=HostAgentBoundary)
    timeout_seconds: float = 120
    report_paths: tuple[Path, ...] = ()
    environment: Mapping[str, str] | None = None

    def score(self, binding: Mapping[str, str], candidate_patch: Path,
              *, verify_candidate: Callable[[], str]) -> dict:
        record = {'binding': dict(binding), 'status': 'infrastructure_error', 'oracle_passed': None,
                  'stdout': '', 'stderr': '', 'returncode': None, 'reports': {}, 'error': None}
        root = None
        try:
            root = _plain_path(self.private_root)
            if not root.is_dir():
                raise IsolationError('evaluator private root must already exist')
            script = _plain_path(self.oracle_script)
            reports = tuple(_plain_path(path) for path in self.report_paths)
            if (not script.is_relative_to(root) or not script.is_file()
                    or not self.command or str(script) not in self.command
                    or any(not path.is_relative_to(root) for path in reports)):
                raise IsolationError('script and reports must be evaluator private')
            ledger = _plain_path(root / 'ledger.jsonl')
            if not ledger.is_relative_to(root):
                raise IsolationError('ledger escapes evaluator private root')
            private_files = (script, ledger, *reports)
            record['boundary'] = self.boundary.verify(root, private_files)
            self._validate_binding(binding)
            request_key = self._request_key(binding)
            record['request_key'] = request_key
            if self._request_seen(ledger, request_key):
                record.update(status='duplicate_request', error='scoring request already settled')
                self._append_private(root, record)
                return {'status': 'settled'}
            patch_hash = _hash(Path(candidate_patch))
            actual_hash = verify_candidate()
            record['candidate_patch_hash'] = patch_hash
            record['materialized_patch_hash'] = actual_hash
            if patch_hash != binding['patch_hash'] or actual_hash != binding['patch_hash']:
                record['status'] = 'binding_mismatch'
            elif any(path.exists() for path in reports):
                record['status'] = 'infrastructure_error'
                record['error'] = 'report path is not fresh'
            else:
                completed = subprocess.run(
                    self.command, cwd=root, env=None if self.environment is None else dict(self.environment),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=self.timeout_seconds,
                )
                record.update(stdout=_text(completed.stdout), stderr=_text(completed.stderr),
                              returncode=completed.returncode)
                for path in reports:
                    checked = _plain_path(path)
                    if not checked.is_relative_to(root):
                        raise IsolationError('report path changed during scoring')
                    record['reports'][path.relative_to(root).as_posix()] = (
                        _text(path.read_bytes()) if path.is_file() else None)
                record['boundary_after'] = self.boundary.verify(root, (script, ledger, *reports))
                after_hash = verify_candidate()
                record['materialized_patch_hash_after'] = after_hash
                if _hash(Path(candidate_patch)) != binding['patch_hash'] or after_hash != binding['patch_hash']:
                    record['status'] = 'binding_mismatch'
                else:
                    self._score_report(record, binding)
        except IsolationError as error:
            record.update(status='isolation_error', oracle_passed=None, error=str(error))
        except subprocess.TimeoutExpired as error:
            record.update(status='infrastructure_error', stdout=_text(error.stdout),
                          stderr=_text(error.stderr), error='oracle timeout')
        except Exception as error:
            record.update(status='infrastructure_error', oracle_passed=None,
                          error=f'{type(error).__name__}: {error}')
        self._append_private(root, record)
        return {'status': 'settled'}

    @staticmethod
    def _validate_binding(binding):
        required = {
            'run_id', 'group_id', 'case_id', 'source_revision', 'source_hash',
            'environment_digest', 'seed', 'spec_hash', 'protocol_hash', 'patch_hash',
        }
        if (not required <= binding.keys()
                or any((not isinstance(binding[key], (str, int)) or not binding[key])
                       if key == 'seed' else
                       (not isinstance(binding[key], str) or not binding[key])
                       for key in required)
                or re.fullmatch(r'[0-9a-f]{64}', binding['patch_hash']) is None):
            raise ValueError('invalid final candidate binding')

    @staticmethod
    def _request_key(binding):
        fields = ('run_id', 'group_id', 'case_id', 'source_revision', 'source_hash',
                  'environment_digest', 'seed', 'spec_hash', 'protocol_hash', 'patch_hash')
        return hashlib.sha256(json.dumps({key: binding[key] for key in fields},
                                         sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _request_seen(ledger, request_key):
        if not ledger.is_file():
            return False
        try:
            return any(json.loads(line).get('request_key') == request_key
                       for line in ledger.read_text(encoding='utf-8').splitlines() if line.strip())
        except (OSError, ValueError, TypeError):
            raise IsolationError('evaluator ledger cannot be read for duplicate protection')

    @staticmethod
    def _score_report(record, binding):
        lines = record['stdout'].strip().splitlines()
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = None
        if not isinstance(result, dict) or type(result.get('passed')) is not bool:
            record['status'] = 'missing_score' if record['returncode'] == 0 else 'infrastructure_error'
            return
        record['oracle_result'] = result
        if result.get('case_id') != binding['case_id']:
            record['status'] = 'binding_mismatch'
        elif record['returncode'] not in {0, 1} or (record['returncode'] == 0) != result['passed']:
            record['status'] = 'infrastructure_error'
            record['error'] = 'oracle result and exit status disagree'
        else:
            record.update(status='scored', oracle_passed=result['passed'])

    @staticmethod
    def _append_private(root, record):
        if root is None:
            return
        try:
            ledger = _plain_path(root / 'ledger.jsonl')
            with ledger.open('a', encoding='utf-8') as output:
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception:
            return


def acknowledge(action: Callable[[], object]) -> int:
    """Trusted evaluator transport keeps output and exit status independent of the score."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            action()
    except Exception:
        pass
    sys.stdout.write(SETTLED_ACKNOWLEDGEMENT + '\n')
    return 0
