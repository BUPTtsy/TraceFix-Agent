"""解析诊断期测试反馈；这些结果不作为 VERIFY 的验证证据。"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter

import yaml

from tracefix.runtime.contracts import digest


_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def normalize_failure_message(message: str) -> str:
    """保留断言内容，去掉机器位置、时间及随机标识造成的噪声。"""
    text = _ANSI.sub('', message).replace('\\', '/')
    text = re.sub(r'\b\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?', '<time>', text)
    text = re.sub(r'\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b', '<time>', text)
    text = re.sub(r'\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b', '<id>', text, flags=re.I)
    text = re.sub(r'\b0x[0-9a-f]+\b', '<address>', text, flags=re.I)
    text = re.sub(r'(?<![\w:])(?:[a-zA-Z]:/|/)[^\s\'"<>|)\],;]+', '<path>', text)
    text = re.sub(r'(?<=\w):\d+(?::\d+)?\b', ':<line>', text)
    text = re.sub(r'\bline\s+\d+(?:\s*[,;]\s*(?:column|col)\s+\d+)?', 'line <line>', text, flags=re.I)
    text = re.sub(r'\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|seconds?|secs?)\b', '<duration>', text, flags=re.I)
    text = re.sub(r'\b((?:request|run|trace|random)[_-]?id\s*[:=]\s*)[\w-]+', r'\1<id>', text, flags=re.I)
    return ' '.join(text.split())


def failure_signature(name: str, message: str) -> str:
    return digest([normalize_failure_message(name), normalize_failure_message(message)])


def _result(cases: list[dict], *, unparsed: bool = False, counts: dict | None = None) -> dict:
    statuses = Counter(case['status'] for case in cases)
    summary = {status: statuses[status] for status in ('passed', 'failed', 'skipped')}
    if counts is not None:
        summary.update(counts)
    summary['unparsed'] = unparsed
    signatures = sorted({failure_signature(case['name'], case.get('message', ''))
                         for case in cases if case['status'] == 'failed'})
    return {'summary': summary, 'cases': cases, 'failure_signatures': signatures}


def _junit(output: str) -> dict | None:
    start = re.search(r'<(?:testsuites|testsuite)(?:\s|>)', output)
    if not start:
        return None
    # 测试命令可能在 XML 前后打印日志；只取完整的根元素。
    root_name = 'testsuites' if output[start.start():].startswith('<testsuites') else 'testsuite'
    end_tag = '</' + root_name + '>'
    end = output.rfind(end_tag)
    payload = output[start.start():end + len(end_tag)] if end >= start.start() else output[start.start():]
    # 测试输出不应携带实体声明，避免解析器展开不受信任内容。
    if '<!DOCTYPE' in output.upper() or '<!ENTITY' in output.upper():
        return None
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    cases = []
    for element in root.iter('testcase'):
        name = element.get('name')
        if not name:
            continue
        classname = element.get('classname')
        case = {'name': f'{classname}::{name}' if classname else name, 'status': 'passed'}
        failures = [child for child in element if child.tag in {'failure', 'error'}]
        if failures:
            case['status'] = 'failed'
            case['message'] = '\n'.join('\n'.join(filter(None, (child.get('message'), child.text)))
                                        for child in failures)
        elif element.find('skipped') is not None:
            case['status'] = 'skipped'
        if element.get('file'):
            case['location'] = element.get('file') + (':' + element.get('line') if element.get('line') else '')
        cases.append(case)
    if cases:
        return _result(cases)
    # 合法空套件可以报告零用例；缺少 testcase 的非空报告不能假称已解析。
    if root.get('tests') == '0':
        return _result([])
    return None


def _json(output: str) -> dict | None:
    decoder = json.JSONDecoder()
    data = None
    for match in re.finditer(r'\{', output):
        try:
            candidate, _ = decoder.raw_decode(output[match.start():])
        except (ValueError, RecursionError):
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get('testResults'), list):
            data = candidate
            break
    if data is None:
        return None
    cases = []
    statuses = {'passed': 'passed', 'failed': 'failed', 'pending': 'skipped',
                'todo': 'skipped', 'disabled': 'skipped', 'skipped': 'skipped'}
    for suite in data['testResults']:
        if not isinstance(suite, dict):
            return None
        assertions = suite.get('assertionResults', suite.get('testResults', []))
        if not isinstance(assertions, list):
            return None
        for assertion in assertions:
            if not isinstance(assertion, dict) or assertion.get('status') not in statuses:
                return None
            name = assertion.get('fullName') or ' '.join([
                *assertion.get('ancestorTitles', []), assertion.get('title', '')]).strip()
            if not name:
                return None
            case = {'name': name, 'status': statuses[assertion['status']]}
            messages = assertion.get('failureMessages') or []
            if not isinstance(messages, list):
                return None
            if messages:
                case['message'] = '\n'.join(str(message) for message in messages)
            if suite.get('name'):
                case['location'] = suite['name']
            cases.append(case)
        if not assertions and suite.get('status') == 'failed':
            # 编译/加载失败可能没有单用例结果，仍需保留失败。
            cases.append({'name': suite.get('name') or 'test suite', 'status': 'failed',
                          'message': str(suite.get('message') or 'test suite failed')})
    if cases or data.get('numTotalTests') == 0:
        return _result(cases)
    return None


def _tap(output: str) -> dict | None:
    lines = output.splitlines()
    cases = []
    index = 0
    while index < len(lines):
        match = re.match(r'^(\s*)(not ok|ok)\s+\d+(?:\s*-\s*|\s+)?(.*?)\s*$', lines[index])
        index += 1
        if not match:
            continue
        _, verdict, name = match.groups()
        directive = re.search(r'\s+#\s*(SKIP|TODO)\b', name, flags=re.I)
        status = 'skipped' if directive else 'failed' if verdict == 'not ok' else 'passed'
        if directive:
            name = name[:directive.start()]
        details = {}
        if index < len(lines) and lines[index].strip() == '---':
            index += 1
            diagnostic = []
            while index < len(lines) and lines[index].strip() != '...':
                diagnostic.append(lines[index])
                index += 1
            index += 1
            try:
                parsed = yaml.safe_load('\n'.join(diagnostic))
                if isinstance(parsed, dict):
                    details = parsed
            except yaml.YAMLError:
                details = {'error': '\n'.join(diagnostic).strip()}
        # node:test 嵌套套件的汇总节点不应再计为一个失败用例。
        if details.get('failureType') == 'subtestsFailed' or details.get('type') == 'suite':
            continue
        case = {'name': name or 'unnamed test', 'status': status}
        if details.get('error') is not None:
            case['message'] = str(details['error'])
        elif status == 'failed' and details.get('message') is not None:
            case['message'] = str(details['message'])
        if details.get('location'):
            case['location'] = str(details['location'])
        cases.append(case)
    if cases:
        return _result(cases)
    if re.search(r'^1\.\.0(?:\s|$)', output, re.M):
        return _result([])
    return None


def _console_summary(output: str) -> dict | None:
    # pytest 的普通控制台汇总只能提供数量，不伪造单用例或失败签名。
    for row in reversed(output.splitlines()):
        pairs = re.findall(r'\b(\d+) (passed|failed|skipped|errors?|xfailed|xpassed)\b', row)
        if pairs:
            counts = {'passed': 0, 'failed': 0, 'skipped': 0}
            for number, status in pairs:
                key = 'failed' if status in {'error', 'errors'} else 'skipped' if status in {'xfailed', 'xpassed'} else status
                counts[key] += int(number)
            return _result([], unparsed=True, counts=counts)
    return None


def parse_test_report(output: str) -> dict:
    """自动识别 JUnit、Jest/Vitest JSON、TAP；未知输出返回 unparsed。"""
    clean = _ANSI.sub('', output)
    for parser in (_junit, _json, _tap, _console_summary):
        try:
            result = parser(clean)
        except (TypeError, ValueError, RecursionError):
            # 畸形结构化输出只降低解析质量，不能让测试工具本身失败。
            continue
        if result is not None:
            return result
    return _result([], unparsed=True)
