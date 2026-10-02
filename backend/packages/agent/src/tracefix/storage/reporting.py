"""Human-readable findings derived from recorded checks and observations."""
from collections import Counter

from tracefix.execution.browser import elements
from tracefix.runtime.contracts import digest
from tracefix.storage.presentation import label


CONDITIONS = {'visible': '可见且唯一', 'absent': '不存在', 'checked': '保持选中',
              'disabled': '处于禁用状态', 'enabled': '处于可用状态'}
ISSUE_STATUSES = {'suspected': '待确认', 'confirmed': '已确认', 'fixed': '已验证修复',
                  'reproduced': '已确认', 'not_reproducible': '未稳定复现',
                  'wont_fix': '暂不修复', 'false_positive': '已判定误报'}


def assertion_description(check, snapshot=''):
    assertion = check['assertion']
    locator = assertion['locator']
    target = f"{locator['role']}「{locator['name']}」"
    expected = f"{target}应{CONDITIONS.get(assertion['condition'], assertion['condition'])}"
    matches = check.get('matches', 0)
    if not matches:
        actual = f'当前页面未找到{target}'
    elif matches > 1:
        actual = f'当前页面匹配到 {matches} 个{target}，无法唯一定位'
    else:
        found = [item for item in elements(snapshot)
                 if item['role'] == locator['role'] and item['name'] == locator['name']]
        attrs = found[0]['attrs'] if found else ''
        condition = assertion['condition']
        actual = f'当前页面仍存在{target}' if condition == 'absent' else (
            f'{target}未选中' if condition == 'checked' and '[checked]' not in attrs else
            f'{target}仍可操作，未处于禁用状态' if condition == 'disabled' and '[disabled]' not in attrs else
            f'{target}处于禁用状态' if condition == 'enabled' and '[disabled]' in attrs else
            f'{target}未满足预期条件')
    return expected, actual


def action_description(action):
    kind = action.get('kind')
    locator = action.get('locator') or {}
    target = f"{locator.get('role', '')}「{locator.get('name', '')}」"
    if kind == 'navigate':
        return f"打开页面 {action.get('value', '')}"
    if kind == 'click':
        return f'点击{target}'
    if kind in {'type', 'select'}:
        verb = '输入' if kind == 'type' else '选择'
        return f"在{target}{verb}「{action.get('value', '')}」"
    if kind == 'press':
        return f"按下 {action.get('value', '')}"
    return ''


def dom_rule_description(details, locator, snapshot):
    config = details.get('config', {})
    condition = details.get('condition', 'visible')
    target = f"{locator.get('role', '元素')}「{locator.get('name') or '任意名称'}」"
    expected = f"{target}应{CONDITIONS.get(condition, condition).replace('且唯一', '')}"
    found = [item for item in elements(snapshot) if item['role'] == locator.get('role')
             and (locator.get('name') is None or item['name'] == locator['name'])]
    actual = f"当前匹配 {details.get('matches', len(found))} 个{target}"
    if 'count' in config:
        expected += f"；匹配数量应为 {config['count']}"
    if 'text_matches' in config:
        expected += f"；名称应匹配表达式 {config['text_matches']}"
        actual += '；实际名称：' + '、'.join(item['name'] for item in found)
    if found and condition == 'checked':
        actual += '；首个元素' + ('已选中' if '[checked]' in found[0]['attrs'] else '未选中')
    if found and condition in {'disabled', 'enabled'}:
        actual += '；首个元素' + ('处于禁用状态' if '[disabled]' in found[0]['attrs'] else '处于可用状态')
    if 'attribute' in config:
        attribute = config['attribute']
        attribute_name = attribute.get('name') if isinstance(attribute, dict) else str(attribute)
        expected += f'；应具有属性 {attribute_name}'
        if isinstance(attribute, dict) and attribute.get('value') is not None:
            expected += f"，值为 {attribute['value']}"
        actual += '；已记录属性：' + '、'.join(item['attrs'] for item in found)
    return expected, actual


def collect_issues(state, events, read):
    operations, steps, observation_steps, urls = {}, [], {}, []
    issues, repetitions = {}, Counter()
    for event in events:
        payload = event['payload']
        if event['type'] == 'tool.started':
            operations[payload['operation_id']] = payload.get('intent', {})
        if event['type'] != 'tool.completed':
            continue
        observation_ref = (payload.get('receipt') or {}).get('observation_ref')
        if not observation_ref:
            continue
        observation = read(observation_ref)
        if observation.get('url') and observation['url'] not in urls:
            urls.append(observation['url'])
        operation_id = payload['operation_id']
        action = operations.get(operation_id, {})
        if operation_id.endswith('scenario.reset'):
            steps = ['重置场景并重新打开页面 ' + observation.get('url', state.url)]
        else:
            description = action_description(action)
            if description:
                steps.append(description)
        observation_steps[observation_ref] = list(steps)

    checked_refs = set()
    checks = [(event['payload']['evidence_ref'], event['phase']) for event in events
              if event['type'] == 'gate.decided' and event['payload'].get('evidence_ref')]
    checks.extend((ref, '') for ref in state.evidence_refs if ref not in {item[0] for item in checks})
    for reference, phase in checks:
        result = read(reference)
        observation_ref = result.get('observation_ref')
        observation = read(observation_ref) if observation_ref else {}
        for check in result.get('assertions', []):
            if check.get('passed'):
                continue
            key = digest([check['assertion'], observation.get('url')])
            if phase == 'REPRODUCE':
                repetitions[key] += 1
            expected, actual = assertion_description(check, observation.get('snapshot', ''))
            issue = issues.setdefault(key, {'id': key, 'source': 'assertion',
                'title': actual, 'location': observation.get('url', state.url),
                'expected': expected, 'actual': actual,
                'steps': observation_steps.get(observation_ref, ['复现步骤未记录，请核对关联证据']),
                'status': 'suspected', 'verification': '已观察到断言失败，尚未稳定复现。',
                'evidence_refs': [], 'assertion': check['assertion']})
            if reference not in issue['evidence_refs']:
                issue['evidence_refs'].append(reference)
        checked_refs.add(reference)

    for key, issue in issues.items():
        if state.reproduced and repetitions[key] >= 2:
            issue.update(status='confirmed', verification=f'独立重放中观察到 {repetitions[key]} 次相同断言失败。')
        if state.outcome != 'FIX_VERIFIED':
            continue
        for reference in state.validation_refs:
            validation = read(reference)
            if (validation.get('kind') not in {'original', 'regression'} or not validation.get('passed')
                    or validation.get('patch_hash') != state.patch_hash
                    or validation.get('test_spec_hash') != state.test_spec_hash
                    or validation.get('source_manifest') != state.source_manifest
                    or validation.get('environment_digest') != state.environment_digest):
                continue
            result = read(validation['artifact_ref'])
            observation = read(result['observation_ref']) if result.get('observation_ref') else {}
            if observation.get('url') != issue['location']:
                continue
            if any(check.get('passed') and check.get('assertion') == issue['assertion']
                   for check in result.get('assertions', [])):
                issue.update(status='fixed', verification='当前补丁的对应场景重测通过，且本地验证门禁已通过。')
                issue['evidence_refs'].append(reference)
                break

    for event in events:
        if event['type'] != 'finding.created' or not event['payload'].get('finding'):
            continue
        finding = event['payload']['finding']
        location, details = finding.get('location', {}), finding.get('details', {})
        references = finding.get('evidence_refs', [])
        observation_ref = references[0] if references else None
        actual = str(details.get('actual') or details.get('message') or details.get('match') or details.get('failure') or finding['title'])
        expected = event['payload'].get('expectation') or '符合对应检测规则'
        if location.get('locator') and 'condition' in details:
            observation = read(observation_ref) if observation_ref else {}
            expected, actual = dom_rule_description(details, location['locator'], observation.get('snapshot', ''))
        key = finding['fingerprint']
        issue = issues.setdefault(key, {'id': key, 'source': finding['source'], 'title': finding['title'],
            'location': location.get('url') or f"{location.get('path', '')}:{location.get('line', '')}",
            'expected': expected, 'actual': actual, 'status': finding.get('status', 'suspected'),
            'verification': ('模型根据页面观测提出，尚未经独立复现或修复验证。' if finding['source'] == 'guided' else
                             '规则检测发现；确认或修复状态仅以该问题的独立证据为准。'),
            'steps': observation_steps.get(observation_ref, ['检查此位置的源码规则匹配；尚未验证 GUI 复现步骤'
                if finding['source'] == 'static' else '已记录页面异常观测，之前的操作步骤未记录，请核对关联证据']),
            'evidence_refs': [], 'rule_id': finding.get('rule_id')})
        issue['evidence_refs'] = list(dict.fromkeys(issue['evidence_refs'] + references))

    findings = list(issues.values())
    counts = Counter(issue['status'] for issue in findings)
    summary = ('本次记录 ' + str(len(findings)) + ' 项问题：' + '，'.join(
        f'{ISSUE_STATUSES.get(status, status)} {count} 项' for status, count in counts.items()) + '。'
        if findings else '本次未形成有证据的问题记录；这不代表所有页面和功能均无问题。')
    coverage = f"本次记录 {len(urls)} 个页面地址、{len(checked_refs)} 份断言检查记录。结论仅覆盖实际执行的场景。"
    return {'summary': summary, 'issues': findings, 'coverage': coverage, 'tested_urls': urls}


def report_text(report):
    lines = [report.get('summary', label(report.get('outcome', 'INCONCLUSIVE')))]
    for index, issue in enumerate(report.get('issues', []), 1):
        lines.extend(['', f"{index}. [{ISSUE_STATUSES.get(issue['status'], issue['status'])}] {issue['title']}",
            f"位置：{issue['location']}", f"预期：{issue['expected']}", f"实际：{issue['actual']}", '复现步骤：'])
        lines.extend(f'  {step_index}. {step}' for step_index, step in enumerate(issue['steps'], 1))
        lines.append('验证：' + issue['verification'])
        if issue['evidence_refs']:
            lines.append('证据：' + '、'.join(issue['evidence_refs']))
    lines.extend(value for value in (report.get('coverage'), report.get('limits')) if value)
    return '\n'.join(lines)
