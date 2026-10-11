from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from tracefix.runtime.contracts import BrowserAction, TestSpec, digest
from tracefix.execution.platforms import subprocess_options, terminate_tree


VERSION = 'tracefix.playwright-acceptance/1'


def oracle_result(result: dict, case_id: str) -> dict:
    if result.get('status') not in {'pass', 'fail'}:
        raise ValueError('不确定结果不能生成 Oracle 二值评分')
    return {'case_id': case_id, 'passed': result['status'] == 'pass', 'error': result.get('error'),
            'steps': [{'name': item['id'], 'passed': item['status'] == 'pass',
                       'error': item.get('error')} for item in result['cases']],
            'oracle': VERSION, 'final_scoring_only': True}


def acceptance_request(spec: TestSpec, *, url: str, binding: dict, reset_path: str | None,
                       original_plan=None, include_regression=True) -> dict:
    spec = TestSpec.model_validate(spec.model_dump(mode='json') if isinstance(spec, TestSpec) else spec)
    if binding.get('spec_hash') != digest(spec):
        raise ValueError('验收绑定与冻结 TestSpec hash 不一致')
    if any(not isinstance(binding.get(key), str) or not binding[key]
           for key in ('run_id', 'repo_id', 'commit')):
        raise ValueError('验收缺少 Run/repo/revision 绑定')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('验收服务必须是无凭据 HTTP URL')
    plan = None
    if original_plan is not None:
        plan = [BrowserAction.model_validate(action.model_dump(mode='json')
                if isinstance(action, BrowserAction) else action).model_dump(mode='json')
                for action in original_plan]
    normalized_url = url if parsed.path else url + '/'
    return {'schema_version': VERSION, 'url': normalized_url, 'spec': spec.model_dump(mode='json'),
            'spec_canonical': json.dumps(spec.model_dump(mode='json'), ensure_ascii=False, sort_keys=True, separators=(',', ':')),
            'scenario_canonical': {scenario.id: json.dumps(scenario.model_dump(mode='json'),
                ensure_ascii=False, sort_keys=True, separators=(',', ':')) for scenario in spec.behavior_scenarios},
            'binding': dict(binding), 'reset_path': reset_path, 'original_plan': plan,
            'include_regression': include_regression,
            'isolation': {'disposable': True, 'service_url': normalized_url}}


def validate_execution(result: dict, request: dict, directory: Path) -> dict:
    if result.get('schema_version') != VERSION or result.get('binding') != request['binding']:
        raise ValueError('Playwright 执行结果版本或绑定不一致')
    status = result.get('status')
    if status not in {'pass', 'fail', 'inconclusive'} or result.get('passed') is not {
            'pass': True, 'fail': False, 'inconclusive': None}[status]:
        raise ValueError('Playwright 状态与 passed 不一致')
    expected = {scenario['id']: digest(scenario) for scenario in request['spec']['behavior_scenarios']
                if not request.get('case_ids') or scenario['id'] in request['case_ids']}
    cases = result.get('cases')
    if not isinstance(cases, list) or len({case.get('id') for case in cases}) != len(cases):
        raise ValueError('场景结果缺失或重复')
    by_id = {case.get('id'): case for case in cases}
    for case_id, scenario_hash in expected.items():
        case = by_id.get(case_id)
        if not case or case.get('scenario_hash') != scenario_hash:
            raise ValueError('场景结果与冻结断言不一致')
    root = directory.resolve()
    for case in cases:
        case_status = case.get('status')
        if case_status not in {'pass', 'fail', 'inconclusive'}:
            raise ValueError('场景状态无效')
        references = case.get('evidence_refs', [])
        if case_status in {'pass', 'fail'} and not references:
            raise ValueError('确定性场景缺少证据')
        for reference in references:
            path = Path(reference).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('执行证据缺失或逃逸验收目录')
        if case_status == 'fail' and not any(check.get('passed') is False
                and check.get('failure_kind') == 'assertion' for check in case.get('checks', [])):
            raise ValueError('fail 缺少确定性断言失败')
    if status == 'pass' and (not cases or any(case['status'] != 'pass' for case in cases)):
        raise ValueError('通过结果缺少完整场景覆盖')
    return result


class PlaywrightAcceptanceRunner:
    def __init__(self, package_root: Path | None = None, *, node_command: str = 'node',
                 timeout_seconds: float = 180, docker_runner=None):
        self.package_root = package_root or Path(__file__).resolve().parents[6] / 'scripts/checks/interface-beta'
        self.node_command = node_command
        self.timeout_seconds = timeout_seconds
        self.docker_runner = docker_runner

    @staticmethod
    def _mount(source: Path, target: str, *, readonly=False) -> str:
        stream = io.StringIO()
        fields = ['type=bind', 'src=' + source.resolve().as_posix(), 'dst=' + target]
        if readonly:
            fields.append('readonly')
        csv.writer(stream, lineterminator='').writerow(fields)
        return stream.getvalue()

    async def _command(self, request, transport, directory):
        if self.docker_runner is None:
            command = shutil.which(self.node_command)
            return ([command, str(self.package_root / 'runner.mjs'), str(transport), str(directory)]
                    if command else None), None
        runner = self.docker_runner
        if (runner.run_id != request['binding']['run_id'] or not runner.resolved_image_ids
                or re.fullmatch(r'[A-Za-z0-9_.-]+', runner.network) is None):
            raise ValueError('Docker 验收必须绑定当前 Run 已启动的冻结镜像和网络')
        inspected = await runner.docker('network', 'inspect', runner.network)
        networks = json.loads(inspected['output'])
        if (len(networks) != 1 or networks[0].get('Internal') is not True
                or networks[0].get('Labels', {}).get('tracefix.run') != runner.run_id):
            raise ValueError('Docker 网络不是当前 Run 的 internal 隔离网络')
        name = 'tf-acceptance-' + uuid.uuid4().hex
        command = ['docker', 'run', '--rm', '--init', '--name', name, '--network', runner.network,
            '--label', 'tracefix.run=' + runner.run_id, '--label', 'tracefix.acceptance=true',
            '--user', '1000:1000', '--cap-drop=ALL', '--security-opt', 'no-new-privileges',
            '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m',
            '--mount', self._mount(self.package_root, '/runner', readonly=True),
            '--mount', self._mount(transport, '/request.json', readonly=True),
            '--mount', self._mount(directory, '/evidence'),
            '--env', 'HOME=/tmp', '--env', 'TRACEFIX_ACCEPTANCE_CONTAINER=1',
            '--entrypoint', 'node', runner.resolved_image_ids['browser'],
            '/runner/runner.mjs', '/request.json', '/evidence']
        return command, name

    async def run(self, spec: TestSpec, *, url: str, artifacts_dir: Path, binding: dict,
                  reset_path: str | None = None, original_plan=None, include_regression=True) -> dict:
        request = acceptance_request(spec, url=url, binding=binding, reset_path=reset_path,
                                     original_plan=original_plan, include_regression=include_regression)
        directory = Path(artifacts_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError('验收产物目录必须为空，防止重用旧证据')
        if self.docker_runner is not None and reset_path is None:
            deadline = time.monotonic() + self.timeout_seconds
            cases = []
            reports = []
            selections = ([] if original_plan is None else ['original']) + (
                ['regression'] if include_regression else []) + [scenario['id'] for scenario in request['spec']['behavior_scenarios']]
            for index, case_id in enumerate(selections):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('确定性验收总时间额度耗尽')
                reset = await self.docker_runner.command('reset')
                if reset.get('passed') is not True:
                    raise ValueError('当前 Run 的受信 reset 命令未成功，拒绝串用场景状态')
                report = await self._run_once({**request, 'case_ids': [case_id]}, directory / f'case-{index}',
                                              timeout_seconds=max(0.1, deadline - time.monotonic()))
                reports.append(report)
                cases.extend(report.get('cases', []))
            status = ('fail' if any(case['status'] == 'fail' for case in cases) else 'pass'
                      if cases and len(cases) == len(selections) and all(case['status'] == 'pass' for case in cases)
                      else 'inconclusive')
            result = {'schema_version': VERSION, 'binding': binding, 'status': status,
                'passed': True if status == 'pass' else False if status == 'fail' else None, 'cases': cases,
                'evidence_refs': [reference for report in reports for reference in report.get('evidence_refs', [])]}
            (directory / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
            (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            result['evidence_refs'].extend([str(directory / 'request.json'), str(directory / 'result.json')])
            return validate_execution(result, request, directory)
        return await self._run_once(request, directory)

    async def _run_once(self, request, directory, *, timeout_seconds=None):
        binding = request['binding']
        directory.mkdir(parents=True, exist_ok=True)
        transport = directory.parent / (directory.name + '.request.json')
        transport.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding='utf-8')
        command, container_name = await self._command(request, transport, directory)
        if not command:
            result = {'schema_version': VERSION, 'status': 'inconclusive', 'passed': None,
                      'binding': binding, 'cases': [], 'evidence_refs': [], 'error': '缺少 Node 执行环境'}
            (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return result
        process = await asyncio.create_subprocess_exec(*command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **subprocess_options())
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_seconds or self.timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await terminate_tree(process)
            if container_name:
                await self.docker_runner.docker('stop', container_name, check=False)
            raise
        (directory / 'bridge-stdout.log').write_bytes(stdout)
        (directory / 'bridge-stderr.log').write_bytes(stderr)
        try:
            result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
            if container_name:
                def host_paths(value):
                    if isinstance(value, str) and value.startswith('/evidence/'):
                        return str(directory / value[len('/evidence/'):])
                    if isinstance(value, list):
                        return [host_paths(item) for item in value]
                    if isinstance(value, dict):
                        return {key: host_paths(item) for key, item in value.items()}
                    return value
                result = host_paths(result)
                (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            return validate_execution(result, request, directory)
        except (OSError, ValueError, TypeError, KeyError) as error:
            result = {'schema_version': VERSION, 'status': 'inconclusive', 'passed': None,
                      'binding': binding, 'cases': [], 'evidence_refs': [],
                      'error': f'Playwright 结果未通过证据校验: {error}', 'exit_code': process.returncode}
            (directory / 'bridge-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return result
