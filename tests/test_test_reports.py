import json
from pathlib import Path

import pytest

from tracefix.execution.test_reports import (failure_signature, normalize_failure_message,
                                             parse_test_report)


FIXTURES = Path(__file__).parent / 'fixtures' / 'test_reports'


@pytest.mark.parametrize('filename', ['node.tap', 'pytest.xml', 'jest.json', 'vitest.json'])
def test_structured_reports_preserve_case_statuses(filename):
    result = parse_test_report((FIXTURES / filename).read_text(encoding='utf-8'))
    assert result['summary'] == {'passed': 1, 'failed': 1, 'skipped': 1, 'unparsed': False}
    assert len(result['cases']) == 3
    failed = next(case for case in result['cases'] if case['status'] == 'failed')
    assert 'title' in failed['name']
    assert '201' in failed['message'] and '400' in failed['message']
    assert len(result['failure_signatures']) == 1
    assert len(result['failure_signatures'][0]) == 64


@pytest.mark.parametrize('filename', ['node.tap', 'pytest.xml', 'jest.json', 'vitest.json'])
def test_signatures_ignore_paths_and_locations_in_real_reports(filename):
    text = (FIXTURES / filename).read_text(encoding='utf-8')
    other = text.replace('/workspace/', '/tmp/run-382/source/').replace(':18:5', ':399:20')
    other = other.replace(':12:3', ':499:10').replace('2.75', '13.459')
    assert parse_test_report(text)['failure_signatures'] == parse_test_report(other)['failure_signatures']


def test_failure_signature_ignores_runtime_noise_but_preserves_assertion_values():
    one = ('at C:\\runs\\abc\\test.py:12:3 line 12, column 3 '
           '2026-10-20T12:23:34.211Z took 13.2 ms address 0x1234 '
           'request_id=abcdef 12345678-1234-1234-1234-123456789abc expected 400 got 201')
    two = ('at /tmp/other/test.py:198:9 line 198, column 9 '
           '2027-01-01T01:02:03.999Z took 88 ms address 0xabcd '
           'request_id=ghijkl abcdefab-abcd-abcd-abcd-abcdefabcdef expected 400 got 201')
    assert normalize_failure_message(one) == normalize_failure_message(two)
    assert failure_signature('same case', one) == failure_signature('same case', two)
    assert failure_signature('same case', one) != failure_signature('same case', one.replace('201', '202'))
    assert failure_signature('same case', one) != failure_signature('different case', one)


def test_tap_nested_suite_does_not_double_count_failure():
    output = '''TAP version 13
    not ok 1 - child
      ---
      error: bad value
      ...
    ok 2 - other child
not ok 1 - parent
  ---
  type: suite
  failureType: subtestsFailed
  error: 1 subtest failed
  ...
1..1
'''
    result = parse_test_report(output)
    assert result['summary'] == {'passed': 1, 'failed': 1, 'skipped': 0, 'unparsed': False}
    assert [case['name'] for case in result['cases']] == ['child', 'other child']


def test_junit_multiple_suites_and_error_nodes():
    output = '<testsuites><testsuite><testcase name="load"><error message="ImportError">missing module</error></testcase></testsuite><testsuite><testcase name="ready"/></testsuite></testsuites>'
    result = parse_test_report(output)
    assert result['summary']['failed'] == 1
    assert result['summary']['passed'] == 1
    assert 'ImportError' in result['cases'][0]['message']


def test_json_suite_load_failure_is_not_lost():
    result = parse_test_report(json.dumps({'testResults': [{
        'name': '/workspace/broken.test.ts', 'status': 'failed',
        'message': 'SyntaxError: unexpected token', 'assertionResults': []}]}))
    assert result['summary']['failed'] == 1
    assert result['cases'][0]['message'] == 'SyntaxError: unexpected token'


@pytest.mark.parametrize('output', [
    'TAP version 13\n1..0\n', '<testsuite tests="0"/>',
    '{"numTotalTests": 0, "testResults": []}',
])
def test_structured_empty_suite_is_parsed(output):
    assert parse_test_report(output)['summary'] == {'passed': 0, 'failed': 0, 'skipped': 0, 'unparsed': False}


@pytest.mark.parametrize('output', ['', 'fatal: missing test runner', '<testsuite><testcase',
                                    '{"testResults": [null]}', '{"testResults": "bad"}',
                                    '<!DOCTYPE testsuite [<!ENTITY x "abc">]><testsuite>&x;</testsuite>'])
def test_unknown_or_malformed_reports_remain_unparsed(output):
    result = parse_test_report(output)
    assert result['summary']['unparsed'] is True
    assert result['failure_signatures'] == []


def test_console_summary_keeps_counts_without_claiming_case_parsing():
    result = parse_test_report('collecting tests\n================ 2 failed, 3 passed, 1 skipped in 0.53s ================\n')
    assert result['summary'] == {'passed': 3, 'failed': 2, 'skipped': 1, 'unparsed': True}
    assert result['cases'] == []


@pytest.mark.parametrize('filename', ['node.tap', 'pytest.xml', 'jest.json'])
def test_command_logs_and_ansi_before_report_are_allowed(filename):
    output = '\x1b[32m> npm test\x1b[0m\n' + (FIXTURES / filename).read_text(encoding='utf-8') + '\ncommand complete\n'
    assert parse_test_report(output)['summary']['failed'] == 1
