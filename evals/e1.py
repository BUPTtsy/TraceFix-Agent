"""准备、检查及运行冻结的 pipeline/hybrid 对照。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from evals.oracle_bridge import OracleBridge
from evals.runner import FourCellRunner
from tracefix.runtime.contracts import digest


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def implementation_manifests(repository, profile):
    from tracefix.config import Profile
    from tracefix.runtime import contracts, repair_progress
    from tracefix.runtime.diagnosis import DiagnosisDraft
    from tracefix.runtime.engine import Engine
    from tracefix.runtime.recovery import RecoveryController
    from tracefix.runtime.smoke import make_engine
    from tracefix.tools.handlers import build_runtime_tools
    import yaml

    repository = Path(repository).resolve()
    registrations = []
    with tempfile.TemporaryDirectory(prefix='tracefix-tool-manifest-') as temporary:
        engine, state = make_engine(Path(temporary))
        engine.profile = Profile.model_validate(yaml.safe_load(Path(profile).read_text(encoding='utf-8')))
        schemas = ((contracts.Phase.EXPLORE, contracts.TestSpec),
                   (contracts.Phase.EXPLORE, contracts.Decision),
                   (contracts.Phase.REPRODUCE, contracts.ReproductionPlan),
                   (contracts.Phase.DIAGNOSE, DiagnosisDraft),
                   (contracts.Phase.DIAGNOSE, contracts.PatchProposal),
                   (contracts.Phase.DIAGNOSE, contracts.FinishRepair))
        previous_mode = os.environ.get('TRACEFIX_AGENT_MODE')
        os.environ['TRACEFIX_AGENT_MODE'] = 'multi'
        try:
            for phase, schema in schemas:
                state.phase = phase
                state.repair_mode = 'hybrid' if schema is contracts.FinishRepair else 'pipeline'
                runtime = build_runtime_tools(engine, state, schema)
                registrations.append({'phase': phase.value, 'schema': schema.__name__,
                    'repair_mode': state.repair_mode, 'tools': [
                        {'name': spec.name, 'input': spec.parameters, 'description': spec.description,
                         'phases': sorted(item.value for item in spec.phases),
                         'side_effect': spec.side_effect, 'parallel_safe': spec.parallel_safe,
                         'submission': spec.submission, 'timeout_s': spec.timeout_s,
                         'output_limit_tokens': spec.output_limit_tokens, 'aliases': list(spec.aliases)}
                        for spec in runtime.registry.specs]})
        finally:
            if previous_mode is None:
                os.environ.pop('TRACEFIX_AGENT_MODE', None)
            else:
                os.environ['TRACEFIX_AGENT_MODE'] = previous_mode
    package = repository / 'backend/packages/agent/src/tracefix'
    tools = {'registrations': registrations, 'implementation': {
        path.relative_to(repository).as_posix(): digest(path.read_bytes())
        for path in sorted((package / 'tools').glob('*.py'))}}
    recovery = {'budgets': {str(cause): dataclasses.asdict(budget)
                           for cause, budget in RecoveryController().budgets.items()},
        'engine': {name: getattr(Engine, name) for name in dir(Engine) if name.startswith('LOOP_')},
        'repair': {name: getattr(repair_progress, name) for name in (
            'WINDOW', 'STAGNATION_WARNING', 'STAGNATION_SIGNAL', 'DIFF_WARNING', 'DIFF_SIGNAL',
            'SUBMISSION_WARNING', 'SUBMISSION_SIGNAL')},
        'implementation': {name: digest((package / 'runtime' / name).read_bytes())
                           for name in ('engine.py', 'recovery.py', 'repair_loop.py', 'repair_progress.py')}}
    return tools, recovery


def _pinned_profile(profile, output):
    import yaml
    data = yaml.safe_load(Path(profile).read_text(encoding='utf-8'))
    images = {}
    for field in ('image', 'browser_image'):
        completed = subprocess.run(['docker', 'image', 'inspect', data[field]],
                                   capture_output=True, check=True, timeout=30)
        identity = json.loads(completed.stdout)[0]['Id']
        images[field] = identity
        data[field] = identity
    Path(output).write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    return images


def prepare(output, *, source_root, profile, model, tier='smoke', skills_root=None,
            vision_model='', thinking='disabled', effort='provider-default'):
    from evals.freeze import freeze_cases

    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='tracefix-e1-inputs-') as temporary:
        staging = Path(temporary)
        pinned = staging / 'profile.yaml'
        _pinned_profile(profile, pinned)
        skills = Path(skills_root) if skills_root else staging / 'skills'
        skills.mkdir(parents=True, exist_ok=True)
        tools, recovery = implementation_manifests(repository, pinned)
        oracle_sources = {'docker_oracle.py': repository / 'evals/docker_oracle.py',
                          'private_oracle/oracle.mjs': repository / 'evals/private_oracle/oracle.mjs'}
        return freeze_cases(tier=tier, source_root=Path(source_root), profile=pinned,
            skills_root=skills, model=model, tools_manifest=tools,
            recovery_manifest=recovery, output=Path(output), private_assets=oracle_sources,
            vision_model=vision_model, thinking=thinking, effort=effort)


class FrozenOracle(OracleBridge):
    def __init__(self, config, suite, adapter, *, private_root):
        from evals.freeze import verify_frozen_suite
        from evals.isolation import _plain_path

        self.config = config
        self.suite = _plain_path(suite)
        self.adapter = adapter
        private = _plain_path(private_root)
        _require_separate(private, self.suite)
        verify_frozen_suite(self.suite)
        if private.exists():
            raise FileExistsError('Oracle 运行私有目录必须全新创建')
        private.mkdir(parents=True, exist_ok=False)
        for relative in ('docker_oracle.py', 'private_oracle/oracle.mjs', 'cases.json'):
            source = self.suite / 'private' / relative
            destination = private / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        script = private / 'docker_oracle.py'
        super().__init__(private_root=private, command=(sys.executable, str(script)),
                         oracle_script=script, boundary=self, timeout_seconds=600)

    def verify(self, private_root, private_files):
        assets = tuple(self.private_root / relative for relative in
                       ('docker_oracle.py', 'private_oracle/oracle.mjs', 'cases.json'))
        frozen = tuple(path for path in (self.suite / 'private').rglob('*') if path.is_file())
        return self.adapter.boundary().verify(private_root, (*private_files, *assets, *frozen))

    def score(self, binding, candidate_patch, *, verify_candidate):
        import yaml

        request_key = self._request_key(binding)
        root = self.private_root / 'requests' / request_key
        request_path = root / 'request.json'
        self.command = (sys.executable, str(self.oracle_script), '--request', str(request_path))
        if root.exists():
            return super().score(binding, candidate_patch, verify_candidate=verify_candidate)
        root.mkdir(parents=True, exist_ok=False)
        candidate = root / 'candidate.patch'
        shutil.copyfile(candidate_patch, candidate)
        private_cases = json.loads((self.private_root / 'cases.json').read_text(encoding='utf-8'))
        case_entries = private_cases.get('cases', private_cases) if isinstance(private_cases, dict) else private_cases
        case = (case_entries[self.config.case_id] if isinstance(case_entries, dict) else
                next(item for item in case_entries if item.get('case_id', item.get('id')) == self.config.case_id))
        profile = yaml.safe_load(self.config.profile.read_text(encoding='utf-8'))
        request = {'private_root': str(self.private_root),
                   'source_repository': str(self.config.source_root),
                   'source_revision': self.config.source_revision,
                   'case_id': self.config.case_id,
                   'mutations': list(self.config.case_mutations()),
                   'oracle_cases': case['oracle_cases'],
                   'known_bug': self.config.known_bug,
                   'app_image': profile['image'], 'browser_image': profile['browser_image'],
                   'candidate_patch': str(candidate), 'binding': dict(binding),
                   'report_path': str(root / 'report.json')}
        _write_json(request_path, request)
        return super().score(binding, candidate_patch, verify_candidate=verify_candidate)

    @staticmethod
    def _score_report(record, binding):
        OracleBridge._score_report(record, binding)
        if record['status'] != 'scored':
            return
        result = record['oracle_result']
        if (result.get('binding') != binding or result.get('patch_hash') != binding['patch_hash']
                or result.get('source_hash') != binding['source_hash']):
            record.update(status='binding_mismatch', oracle_passed=None)


def _require_separate(root, *protected):
    root = Path(root).resolve()
    if any(root.is_relative_to(Path(other).resolve()) or Path(other).resolve().is_relative_to(root)
           for other in protected):
        raise ValueError('运行输出与冻结资产必须位于互不包含的独立目录')


RUNTIME_PREFLIGHT = '''"""无模型的真实 Docker broker/应用/浏览器/命令验收。"""
import asyncio
import json
from pathlib import Path
import yaml
from evals.docker_worker import worker_owned_source
from tracefix.config import Profile, Project
from tracefix.execution.browser import MCPBrowser
from tracefix.execution.policy import Policy
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.knowledge.scope import ScopeResolver
from tracefix.runtime.contracts import BrowserAction, digest

async def main():
    root = Path('/agent-run')
    source = worker_owned_source(root)
    project = Project(id='preflight', repo_id='preflight', root=source)
    scopes = ScopeResolver({'preflight': project})
    workspace, snapshot = Workspace.export(scopes, scopes.context('preflight'), 'HEAD',
                                           root / 'data/runtime-preflight')
    profile = Profile.model_validate(yaml.safe_load((root / 'inputs/profile.yaml').read_text()))
    runner = DockerRunner(profile, workspace, 'preflight-runtime')
    browser = MCPBrowser(runner.browser_command(), Policy(profile.allowed_origins), timeout=60)
    report = {'status': 'infrastructure_error', 'model_calls': 0}
    try:
        environment = await runner.inspect_images()
        health = await runner.start(digest(snapshot))
        if not health['passed']:
            raise RuntimeError('preflight application health failed')
        await browser.open()
        observation = await browser.action(BrowserAction(kind='navigate', value=profile.url))
        if 'Task board' not in observation['snapshot']:
            raise RuntimeError('preflight browser did not observe Task board')
        actual = await runner.inspect_images(runtime=True)
        if actual != environment:
            raise RuntimeError('preflight runtime image identity drifted')
        commands = {name: await runner.command(name) for name in ('static', 'unit', 'build')}
        if not all(value['passed'] for value in commands.values()):
            raise RuntimeError('preflight static/unit/build failed: ' + json.dumps(commands))
        inspected = await runner.docker('container', 'inspect', runner.name, runner.name + '-browser')
        network = await runner.docker('network', 'inspect', runner.network)
        report.update(status='ready', source_manifest=digest(snapshot), environment_digest=environment,
            runtime_environment_digest=actual, health=health, commands=commands,
            browser={'url': observation['url'], 'snapshot_hash': digest(observation['snapshot']),
                     'task_board_observed': True}, container_evidence=json.loads(inspected['output']),
            network_evidence=json.loads(network['output']))
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        try:
            await browser.close()
        finally:
            await runner.close()
            (root / 'runtime-preflight.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'status': report['status'], 'model_calls': 0}))

asyncio.run(main())
'''


def _runtime_preflight(adapter, output, config):
    public = json.loads((output / 'inputs/config.json').read_text(encoding='utf-8'))
    if (public.get('mutations') or any(public.get(field) for field in
            ('mutation_file', 'mutation_before', 'mutation_after'))
            or public.get('case_id') == config.case_id
            or public.get('source_revision') == config.source_revision
            or public.get('known_bug') is not None):
        raise ValueError('Agent 公开配置泄露评测方缺陷元数据')
    script = output / 'runtime-preflight.py'
    script.write_text(RUNTIME_PREFLIGHT, encoding='utf-8')
    completed = adapter._docker('exec', adapter.worker_id, 'python', '/agent-run/runtime-preflight.py',
                                check=False, timeout=240)
    runtime_path = output / 'runtime-preflight.json'
    report = json.loads(runtime_path.read_text(encoding='utf-8')) if runtime_path.is_file() else {}
    if completed.returncode or report.get('status') != 'ready':
        error = (completed.stdout + completed.stderr).decode('utf-8', errors='replace')[-3000:]
        raise RuntimeError('Docker broker 运行能力验收失败：' + error)
    report['public_config_private_metadata_absent'] = True
    report['broker_audit'] = list(adapter.broker.audit)
    _write_json(runtime_path, report)
    return report


def preflight(suite, output, *, image, exercise_runtime=False):
    from evals.docker_adapter import DockerSessionAdapter
    from evals.freeze import load_frozen_suite, verify_frozen_suite

    suite, output = Path(suite).resolve(), Path(output).resolve()
    _require_separate(output, suite)
    manifest = verify_frozen_suite(suite)
    configs = load_frozen_suite(suite)
    output.mkdir(parents=True, exist_ok=False)
    if exercise_runtime:
        from evals.runner import _copy_source
        _copy_source(configs[0].source_root, configs[0].source_revision, output / 'source')
    else:
        (output / 'source').mkdir()
    adapter = DockerSessionAdapter(image)
    try:
        prepared = adapter.prepare(output, configs[0])
        runtime = _runtime_preflight(adapter, output, configs[0]) if exercise_runtime else None
        proof = adapter.boundary().verify(suite / 'private',
            tuple(path for path in (suite / 'private').rglob('*') if path.is_file()))
        report = {'status': 'ready', 'model_calls': 0, 'suite': str(suite),
                  'manifest_hash': manifest['manifest_hash'], 'cases': [config.case_id for config in configs],
                  'prepared': prepared, 'isolation': proof, 'runtime': runtime}
        _write_json(output / 'preflight.json', report)
        return report
    finally:
        adapter.close()


def run(suite, output, *, image, repeat=3, private_root=None):
    from evals.docker_adapter import DockerSessionAdapter
    from evals.freeze import load_frozen_suite
    from evals.run_matrix import run_matrix

    suite, output = Path(suite).resolve(), Path(output).resolve()
    private_root = Path(private_root).resolve() if private_root else output.with_name(output.name + '-private')
    _require_separate(output, suite)
    _require_separate(private_root, suite, output)
    configs = load_frozen_suite(suite)
    if not (suite / 'private/docker_oracle.py').is_file():
        raise ValueError('私有 Oracle 尚未安装，请先准备并验证 Oracle')

    def factory(config, directory):
        adapter = DockerSessionAdapter(image)
        private = private_root / digest({'case_id': config.case_id, 'seed': config.seed})
        return FourCellRunner(config, directory, adapter=adapter,
                              oracle=FrozenOracle(config, suite, adapter, private_root=private))

    return run_matrix(configs, output, repeat=repeat, runner_factory=factory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--source-root', type=Path, required=True)
    preparation.add_argument('--profile', type=Path, default=Path('profiles/bugboard.yaml'))
    preparation.add_argument('--model', required=True)
    preparation.add_argument('--vision-model', default='')
    preparation.add_argument('--thinking', choices=('enabled', 'disabled'), default='disabled')
    preparation.add_argument('--effort', default='provider-default')
    preparation.add_argument('--tier', choices=('smoke', 'standard'), default='smoke')
    preparation.add_argument('--skills-root', type=Path)
    preparation.add_argument('--output', type=Path, required=True)
    verification = commands.add_parser('verify')
    verification.add_argument('--suite', type=Path, required=True)
    for name in ('preflight', 'run'):
        command = commands.add_parser(name)
        command.add_argument('--suite', type=Path, required=True)
        command.add_argument('--image', required=True)
        command.add_argument('--output', type=Path, required=True)
        if name == 'run':
            command.add_argument('--repeat', type=int, default=3)
            command.add_argument('--private-root', type=Path,
                                 help='独立评测方证据目录，默认位于 output 同级的 output-private')
        else:
            command.add_argument('--exercise-runtime', action='store_true',
                                 help='无模型地验收真实 broker、应用、浏览器与 static/unit/build')
    arguments = vars(parser.parse_args(argv))
    operation = arguments.pop('operation')
    if operation == 'verify':
        from evals.freeze import verify_frozen_suite
        result = verify_frozen_suite(arguments['suite'])
    else:
        result = globals()[operation](**arguments)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
