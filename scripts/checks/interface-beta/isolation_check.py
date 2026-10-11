from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from tracefix.config import Profile, load_projects
from tracefix.execution.browser import MCPBrowser
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.scope import ScopeResolver
from tracefix.repository_registry import RepositoryRegistry
from tracefix.rules.resolver import RuleResolver
from tracefix.runtime.contracts import digest


READ_FUNCTION = '''async () => ({
    cookie: document.cookie,
    storage: Object.fromEntries(Object.entries(localStorage)),
    tasks: await (await fetch('/api/tasks')).json(),
    source: await (await fetch('/version')).json()
})'''
WRITE_FUNCTION = '''async () => {
    const marker = __MARKER__;
    document.cookie = 'tracefix_isolation=' + marker + '; path=/; SameSite=Strict';
    localStorage.setItem('tracefix_isolation', marker);
    const response = await fetch('/api/tasks', {method:'POST',
        headers:{'content-type':'application/json'}, body:JSON.stringify({title:marker})});
    if (!response.ok) throw new Error('isolated task create failed: ' + response.status);
    return await response.json();
}'''


def save(path: Path, value) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return digest(path.read_bytes())


def validate_browser_state(initial, refreshed, marker, prior_markers, source_manifest):
    if initial.get('cookie') or initial.get('storage'):
        raise AssertionError('新 Run 浏览器没有空 cookie/localStorage')
    if any(item.get('title', '').startswith('isolation-') for item in initial.get('tasks', [])):
        raise AssertionError('新 Run 测试数据包含前次隔离 marker')
    if refreshed.get('storage', {}).get('tracefix_isolation') != marker:
        raise AssertionError('刷新后 localStorage marker 不匹配')
    if 'tracefix_isolation=' + marker not in refreshed.get('cookie', ''):
        raise AssertionError('刷新后 cookie marker 不匹配')
    if sum(item.get('title') == marker for item in refreshed.get('tasks', [])) != 1:
        raise AssertionError('刷新后未唯一读回本 Run 创建的测试数据')
    serialized = json.dumps(refreshed, ensure_ascii=False)
    if any(previous in serialized for previous in prior_markers):
        raise AssertionError('当前 Run 存在前次 marker')
    if initial.get('source', {}).get('source_manifest') != source_manifest or refreshed.get('source', {}).get('source_manifest') != source_manifest:
        raise AssertionError('应用版本未绑定当前源码快照')


def validate_sequence(records):
    if [record['repo_id'] for record in records] != ['alpha', 'beta', 'alpha']:
        raise AssertionError('隔离验收必须完成 A→B→A')
    for key in ('run_id', 'workspace', 'network', 'volume', 'frozen_registry', 'marker'):
        if len({record[key] for record in records}) != 3:
            raise AssertionError('资源未逐 Run 独占：' + key)
    if records[0]['commit'] != records[2]['commit'] or records[0]['source_files_hash'] != records[2]['source_files_hash']:
        raise AssertionError('A 再次运行源码发生变化')
    if records[0]['commit'] == records[1]['commit'] or records[0]['source_files_hash'] == records[1]['source_files_hash']:
        raise AssertionError('A/B 没有使用不同的冻结源码')
    if records[0]['config_digest'] != records[2]['config_digest'] or records[0]['config_digest'] == records[1]['config_digest']:
        raise AssertionError('仓库执行配置串用或漂移')
    if records[0]['credential_ref'] != records[2]['credential_ref'] or records[0]['credential_ref'] == records[1]['credential_ref']:
        raise AssertionError('凭据引用没有按仓库隔离')
    for record in records:
        if record['status'] != 'pass':
            raise AssertionError('存在未通过的组件 Run')


def mount_source_path(value: str, *, windows=None) -> Path:
    windows = os.name == 'nt' if windows is None else windows
    if windows:
        mapped = re.fullmatch(r'/(?:host_mnt|run/desktop/mnt/host)/([a-zA-Z])/(.+)', value)
        if mapped:
            value = mapped[1] + ':/' + mapped[2]
    return Path(value).resolve()


def validate_mounts(container: dict, workspace: Path, dependency_volume: str):
    mounts = container['Mounts']
    source_mount = next(mount for mount in mounts if mount['Destination'] == '/app')
    volume_mount = next(mount for mount in mounts if mount['Destination'] == '/app/node_modules')
    requested = next(mount for mount in container['HostConfig']['Mounts'] if mount['Target'] == '/app')
    if (source_mount['Type'] != 'bind' or source_mount['RW'] is not False
            or requested['Type'] != 'bind' or requested['ReadOnly'] is not True
            or mount_source_path(requested['Source']) != workspace.resolve()
            or mount_source_path(source_mount['Source']) != workspace.resolve()
            or volume_mount['Type'] != 'volume' or volume_mount['RW'] is not False
            or volume_mount['Name'] != dependency_volume):
        raise AssertionError('应用源码或依赖 volume 挂载身份不一致')


async def evaluate(browser, function, evidence_path):
    save(evidence_path.with_suffix('.request.json'), {'tool': 'browser_evaluate',
        'function': function, 'function_hash': digest(function.encode())})
    browser.MAP = {**browser.MAP, 'isolation_evaluate': 'browser_evaluate'}
    blocks = await browser.call('isolation_evaluate', {'function': function})
    save(evidence_path, [block.model_dump(mode='json') for block in blocks])
    text = '\n'.join(block.text for block in blocks if block.type == 'text')
    match = re.search(r'### Result\s*\n([\s\S]*?)(?=\n### |\Z)', text)
    if not match:
        raise ValueError('MCP evaluate 未返回可读取的 JSON Result')
    return json.loads(match.group(1))


async def one_run(projects_path, registry, output, repo_id, prior_markers, rules, selected_rule):
    run_id = 'isolation-' + repo_id + '-' + uuid.uuid4().hex[:12]
    directory = output / run_id
    directory.mkdir()
    marker = run_id
    request = {'component_only': True, 'repo_id': repo_id, 'run_id': run_id, 'marker': marker,
               'prior_markers': list(prior_markers), 'projects': str(projects_path), 'selected_rule': selected_rule}
    save(directory / 'request.json', request)
    frozen = registry.freeze(repo_id, run_id)
    scopes = ScopeResolver(load_projects(Path(frozen['projects_path'])), Path(frozen['projects_path']))
    workspace, source = Workspace.export(scopes, scopes.context(repo_id), frozen['commit'], output / 'workspaces' / run_id)
    profile = Profile.model_validate(frozen['repository_config']['profile'])
    source_manifest = digest(source)
    save(directory / 'repository-snapshot.json', source)
    save(directory / 'frozen-config.json', frozen)
    resolver = RuleResolver(rules)
    rule_snapshot, resolved = resolver.resolve_snapshot(run_id=run_id, project_id=repo_id,
        scope_id=repo_id, repo_id=scopes.projects[repo_id].repo_id, phase='EXPLORE',
        url=profile.url, paths=source['files'], rule_ids=[selected_rule] if selected_rule else [])
    save(directory / 'rules.json', rule_snapshot.model_dump(mode='json'))
    if selected_rule and selected_rule not in {rule.id for rule in resolved}:
        raise AssertionError('指定项目规则没有进入当前 Run 快照')
    for rule in resolved:
        if rule.scope.project_ids and repo_id not in rule.scope.project_ids:
            raise AssertionError('规则包含其他仓库的项目限定规则')
    try:
        resolver.load_snapshot_rules(rule_snapshot, project_id='foreign', scope_id='foreign')
    except ValueError:
        foreign_rule_snapshot_rejected = True
    else:
        raise AssertionError('规则快照被错误项目接收')
    runner = DockerRunner(profile, workspace, run_id)
    browser = None
    result = {'repo_id': repo_id, 'run_id': run_id, 'status': 'inconclusive', 'marker': marker,
        'workspace': str(workspace.root), 'network': runner.network, 'volume': runner.dependency_volume,
        'frozen_registry': frozen['projects_path'], 'commit': frozen['commit'],
        'source_files_hash': digest(source['files']), 'source_manifest': source_manifest,
        'config_digest': frozen['config_digest'], 'credential_ref': frozen['repository_config']['credential_ref'],
        'rule_snapshot_hash': rule_snapshot.hash, 'rule_ids': [rule.id for rule in resolved],
        'foreign_rule_snapshot_rejected': foreign_rule_snapshot_rejected}
    try:
        environment_digest = await runner.inspect_images()
        health = await runner.start(source_manifest)
        save(directory / 'health.json', health)
        if health['passed'] is not True:
            raise RuntimeError('隔离应用健康检查失败')
        browser = MCPBrowser(runner.browser_command(), Policy(profile.allowed_origins), timeout=60)
        await browser.open()
        save(directory / 'mcp-tools.json', browser.tools)
        await browser.call('navigate', {'url': profile.url})
        initial = await evaluate(browser, READ_FUNCTION, directory / 'mcp-initial.json')
        save(directory / 'initial.json', initial)
        created = await evaluate(browser, WRITE_FUNCTION.replace('__MARKER__', json.dumps(marker)), directory / 'mcp-write.json')
        save(directory / 'created-task.json', created)
        await browser.call('navigate', {'url': profile.url})
        refreshed = await evaluate(browser, READ_FUNCTION, directory / 'mcp-refreshed.json')
        save(directory / 'refreshed.json', refreshed)
        validate_browser_state(initial, refreshed, marker, prior_markers, source_manifest)
        observation = await browser.observe()
        screenshot = observation.pop('png', None)
        if screenshot:
            (directory / 'screenshot.png').write_bytes(screenshot)
        save(directory / 'observation.json', observation)
        inspect = await runner.docker('container', 'inspect', runner.name, runner.name + '-browser')
        containers = json.loads(inspect['output'])
        save(directory / 'containers.json', containers)
        network = json.loads((await runner.docker('network', 'inspect', runner.network))['output'])[0]
        volume = json.loads((await runner.docker('volume', 'inspect', runner.dependency_volume))['output'])[0]
        save(directory / 'network.json', network)
        save(directory / 'volume.json', volume)
        if not network['Internal'] or network.get('Labels', {}).get('tracefix.run') != run_id or volume.get('Labels', {}).get('tracefix.run') != run_id:
            raise AssertionError('网络或 volume 不属于本 Run')
        for container in containers:
            if container['Config'].get('Labels', {}).get('tracefix.run') != run_id or container['HostConfig']['NetworkMode'] != runner.network:
                raise AssertionError('应用或浏览器容器不属于本 Run')
        validate_mounts(containers[0], workspace.root, runner.dependency_volume)
        if await runner.inspect_images(runtime=True) != environment_digest or await runner.version() != source_manifest:
            raise AssertionError('运行期镜像或源码摘要发生漂移')
        workspace.check_frozen(source)
        result.update(status='pass', environment_digest=environment_digest, browser_state_verified=True,
                      data_verified=True, source_verified=True, resource_identity_verified=True)
    except Exception as error:
        result.update(status='inconclusive' if isinstance(error, (RuntimeError, OSError, TimeoutError)) else 'fail',
                      error=f'{type(error).__name__}: {error}')
    finally:
        if browser:
            await browser.close()
        await runner.close()
    inventory = {path.relative_to(directory).as_posix(): digest(path.read_bytes())
                 for path in directory.rglob('*') if path.is_file()}
    result['evidence_hashes'] = inventory
    save(directory / 'result.json', result)
    return result


async def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('组件隔离证据目录必须为空')
    private = json.loads(args.deployment.read_text(encoding='utf-8'))
    api = f'http://127.0.0.1:{private["apiPort"]}/api/v1/rules'
    with urlopen(Request(api, headers={'Authorization': 'Bearer ' + private['controlToken']}), timeout=10) as response:
        rules = json.loads(response.read())
    if not isinstance(rules, list):
        raise ValueError('规则接口没有返回规则列表')
    save(output / 'rules-response.json', rules)
    registry = RepositoryRegistry(args.projects, authorized_roots=args.repository_root, data_root=output / 'registry')
    records = []
    for repo_id in ('alpha', 'beta', 'alpha'):
        record = await one_run(args.projects, registry, output, repo_id,
            [previous['marker'] for previous in records], rules,
            args.alpha_rule if repo_id == 'alpha' else args.beta_rule)
        records.append(record)
        save(output / 'results.json', records)
        if record['status'] != 'pass':
            break
    error = None
    try:
        validate_sequence(records)
    except AssertionError as failure:
        error = str(failure)
    result = {'component_only': True, 'status': 'fail' if error else 'pass', 'passed': not error,
        'sequence': [record['repo_id'] for record in records], 'records': records, 'error': error,
        'project_specific_rules_verified': bool(args.alpha_rule and args.beta_rule),
        'not_verified': ['真实模型与官方 GitHub MCP 凭据闭环', '补丁与 repair result 的跨仓库隔离'] +
            ([] if args.alpha_rule and args.beta_rule else ['项目专属规则选择；当前仅验证现有规则快照身份与错误项目拒绝'])}
    save(output / 'result.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--projects', type=Path, required=True)
    parser.add_argument('--deployment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repository-root', type=Path, action='append', required=True)
    parser.add_argument('--alpha-rule')
    parser.add_argument('--beta-rule')
    result = asyncio.run(run(parser.parse_args()))
    print(json.dumps({'status': result['status'], 'sequence': result['sequence'], 'error': result['error'],
                      'not_verified': result['not_verified']}, ensure_ascii=True))
    raise SystemExit(0 if result['passed'] else 1)
