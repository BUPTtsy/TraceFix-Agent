"""T10 四格运行适配器。

该模块只负责实验边界、运行时开关和评测账本；修复循环、记忆和 Oracle
仍由已有 runtime/evals 模块提供。默认命令适配器适合真实 CLI，测试可注入
一个明确返回运行结果的 callable。
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import tarfile
import io
import tempfile
import time
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import AblationGroup, EvaluationConfig, GROUPS
from tracefix.runtime.contracts import digest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(root), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _copy_source(source: Path, revision: str, destination: Path) -> str:
    """Export the requested commit instead of copying a potentially dirty tree."""
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.run(['git', '-C', str(source), 'archive', revision],
                             capture_output=True, check=False)
    if archive.returncode:
        raise RuntimeError(f'源码导出失败：{archive.stderr.decode(errors="replace")[-1000:]}')
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode='r:*') as stream:
        stream.extractall(destination, filter='data')
    subprocess.run(['git', '-C', str(destination), 'init', '-q'], check=True,
                   capture_output=True)
    subprocess.run(['git', '-C', str(destination), 'config', 'user.email', 'tracefix-eval@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(destination), 'config', 'user.name', 'TraceFix evaluator'], check=True)
    subprocess.run(['git', '-C', str(destination), 'add', '.'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(destination), 'commit', '-qm', 'frozen evaluator source'], check=True)
    return _git(source, 'rev-parse', revision)


def _tree_hash(root: Path) -> str:
    files = {path.relative_to(root).as_posix(): _sha256(path)
             for path in sorted(root.rglob('*')) if path.is_file()}
    return digest(files)


@dataclass(frozen=True)
class RunBinding:
    group_id: str
    run_id: str
    case_id: str
    family: str
    source_revision: str
    source_hash: str
    environment_digest: str
    seed: int
    spec_hash: str
    protocol_hash: str
    model: str
    effort: str
    tools_hash: str
    skill_hash: str
    initial_experience_hash: str
    recovery_rules_hash: str
    patch_hash: str | None = None

    def as_dict(self):
        return {key: value for key, value in self.__dict__.items()}


class CommandAdapter:
    """Run an audited command in one isolated group directory."""

    def __call__(self, *, command, cwd, env, timeout, binding, config, group):
        if not command:
            return {'status': 'inconclusive', 'outcome': 'INFRA_FAILURE',
                    'infra_failure': True, 'error': '未配置修复命令'}
        process = subprocess.run(list(command), cwd=cwd, env=env, timeout=timeout,
                                 capture_output=True, text=True)
        result = {'status': 'completed' if process.returncode == 0 else 'failed',
                  'exit_code': process.returncode,
                  'stdout_tail': process.stdout[-4000:], 'stderr_tail': process.stderr[-4000:]}
        parsed = None
        for line in reversed(process.stdout.splitlines()):
            candidate = line.strip()
            if candidate.startswith('BATCH_RESULT:'):
                candidate = candidate.split(':', 1)[1].strip()
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed = value
                break
        if parsed is not None:
            result.update(parsed)
            if parsed.get('outcome') == 'FIX_VERIFIED':
                result['internal_success'] = True
        else:
            result.update({'outcome': 'INFRA_FAILURE' if process.returncode else 'inconclusive',
                           'infra_failure': process.returncode != 0})
        return result


def instrument_memory_calls(memory, trace: list[dict[str, Any]]):
    """Record calls to the existing memory implementation without changing it."""
    if memory is None:
        return
    for name in ('note', 'candidate', 'save_job_memory', 'job_memory', 'retrieve', 'search'):
        function = getattr(memory, name, None)
        if not callable(function) or getattr(function, '_tracefix_eval_wrapped', False):
            continue
        def wrapped(*args, __function=function, __name=name, **kwargs):
            trace.append({'event': 'memory.' + __name,
                          'layer': kwargs.get('layer') or (args[3] if len(args) > 3 else None),
                          'cross_run': os.getenv('TRACEFIX_CROSS_RUN_MEMORY', 'on')})
            return __function(*args, **kwargs)
        wrapped._tracefix_eval_wrapped = True
        setattr(memory, name, wrapped)


class SessionAdapter:
    """窄 CLI adapter：实际复用 Session/Engine，不复制修复循环。

    Host Session 无法证明 held-out Oracle 隔离，因此 held-out 调用明确返回
    ``isolation_error``；公开开发运行可由调用方提供真实数据库/API 配置。
    """

    def __init__(self, *, held_out=False):
        self.held_out = held_out

    def __call__(self, *, source, root, env, config, binding, **_kwargs):
        if self.held_out:
            return {'status': 'isolation_error', 'outcome': 'INFRA_FAILURE',
                    'infra_failure': True, 'isolation_error': 'Host Session cannot expose held-out Oracle'}
        if not env.get('TRACEFIX_DATABASE_URL') or not env.get('TRACEFIX_API_KEY'):
            return {'status': 'inconclusive', 'outcome': 'INFRA_FAILURE', 'infra_failure': True,
                    'error': 'TRACEFIX_DATABASE_URL/API_KEY 未配置'}
        from tracefix.cli.main import Session
        from types import SimpleNamespace
        import yaml
        archive_head = _git(source, 'rev-parse', 'HEAD')
        registry = Path(root) / 'registry'
        registry.mkdir()
        profile_data = yaml.safe_load(config.profile.read_text(encoding='utf-8'))
        profile_data['project'] = config.project
        profile_data['source_commit'] = archive_head
        profile_path = registry / 'profile.yaml'
        profile_path.write_text(yaml.safe_dump(profile_data, allow_unicode=True), encoding='utf-8')
        project_path = registry / 'projects.yaml'
        project_path.write_text(yaml.safe_dump({'projects': [{'id': config.project,
            'repo_id': f'eval-{binding["group_id"]}', 'root': str(source),
            'allowed_files': list(config.allowed_files)}]}), encoding='utf-8')
        args = SimpleNamespace(project=config.project, projects=str(project_path), profile=str(profile_path),
            console_db=str(Path(root) / 'data' / 'console.sqlite3'), data=str(Path(root) / 'data'),
            goal=config.goal, mode='repair', plain=True, url=None, spec=str(config.spec),
            skills=str(config.skills_root), run=True, command=None, continue_run=None,
            instruction='', execution_mode='batch', parent_run=None, rule=None)
        session = Session(args, None, None)

        async def execute():
            await session.create()
            trace = []

            instrument_memory_calls(session.engine.memory, trace)
            while session.task:
                task = session.task
                await task
                if session.task is task:
                    break
            state = session.state()
            report = session.artifacts.json(state.scope_id, state.run_id, state.report_ref) if state.report_ref else {}
            candidate = report.get('patch_diff_ref')
            candidate_path = Path(root) / 'candidate.patch'
            if candidate:
                candidate_path.write_bytes(session.artifacts.read(state.scope_id, state.run_id, candidate))
            events = session.store.trace(state.run_id, state.scope_id)
            for event in events:
                payload = event.get('payload') or {}
                if event['type'] in {'model.called', 'model.started'}:
                    trace.append({'event': 'model_call', 'model_agent_id': event.get('run_id') or
                                  payload.get('model_revision') or payload.get('model') or 'unknown',
                                  'source': 'runtime_audit'})
                elif event['type'] in {'subtask.started', 'subtask.completed'}:
                    trace.append({'event': event['type'], 'model_agent_id': payload.get('child_run_id'),
                                  'source': 'runtime_audit'})
                elif event['type'].startswith('memory.') or event['type'].startswith('knowledge.'):
                    trace.append({'event': event['type'], 'layer': payload.get('layer'),
                                  'cross_run': payload.get('cross_run')})
            return {'status': 'completed', 'outcome': str(state.outcome) if state.outcome else None,
                    'internal_success': str(state.outcome) == 'FIX_VERIFIED',
                    'candidate_patch': str(candidate_path) if candidate_path.is_file() else None,
                    'materialized_patch_hash': digest(session.engine.workspace.diff(state.patch_base_commit or 'HEAD').encode()),
                    'verify_candidate': lambda: digest(session.engine.workspace.diff(state.patch_base_commit or 'HEAD').encode()),
                    'trace': trace, 'trace_origin': 'runtime_audit', 'gui_real': True,
                    'usage': state.budget.model_dump(mode='json'), 'run_id': state.run_id}
        previous = {key: os.environ.get(key) for key in env
                    if key.startswith('TRACEFIX_')}
        try:
            os.environ.update({key: str(value) for key, value in env.items() if key.startswith('TRACEFIX_')})
            return asyncio.run(execute())
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class FourCellRunner:
    """以每个 case×group 为单位创建全新源码、数据和运行时边界。"""

    def __init__(self, config: EvaluationConfig, root: Path, *, adapter=None,
                 oracle=None, env: Mapping[str, str] | None = None):
        self.config = config
        self.root = Path(root).resolve()
        self.adapter = adapter or SessionAdapter(held_out=config.held_out)
        self.oracle = oracle
        self.base_env = dict(env or {})
        self.rows: list[dict[str, Any]] = []
        self._fixed_inputs = config.fixed_inputs()

    def _group_dir(self, group: AblationGroup) -> Path:
        path = self.root / self.config.case_id / group.name
        if path.exists():
            raise FileExistsError(f'实验组目录已存在，拒绝复用：{path}')
        path.mkdir(parents=True)
        for name in ('data', 'artifacts', 'cache', 'browser', 'session'):
            (path / name).mkdir()
        return path

    def _environment(self, group, path, fixed):
        env = os.environ.copy()
        env.update(self.base_env)
        env.update({
            'TRACEFIX_AGENT_MODE': group.agent_mode,
            'TRACEFIX_CROSS_RUN_MEMORY': 'on' if group.cross_run_memory else 'off',
            'TRACEFIX_RUN_ID': f'eval-{self.config.case_id}-{group.name}',
            'TRACEFIX_EVAL_GROUP': group.name,
            'TRACEFIX_EVAL_SEED': str(self.config.seed),
            'TRACEFIX_DATA': str(path / 'data'),
            'TRACEFIX_MEMORY_DB': str(path / 'data' / 'memory.sqlite3'),
            'TRACEFIX_BROWSER_PROFILE': str(path / 'browser'),
            'TRACEFIX_SESSION_ROOT': str(path / 'session'),
            'TRACEFIX_CACHE_ROOT': str(path / 'cache'),
            'TRACEFIX_SOURCE_REVISION': self.config.source_revision,
            'TRACEFIX_SKILLS_ROOT': str(self.config.skills_root),
            'TRACEFIX_INITIAL_EXPERIENCE': str(self.config.initial_experience),
            # held-out final Oracle is frozen; public L0/L1 remains runtime-owned.
            'TRACEFIX_HELD_OUT': '1' if self.config.held_out else '0',
            'TRACEFIX_MEMORY_FROZEN': '1' if self.config.held_out else '0',
            'TRACEFIX_EVAL_FIXED_INPUTS': json.dumps(fixed, sort_keys=True),
        })
        return env

    def _invoke(self, kwargs):
        if hasattr(self.adapter, 'run'):
            function = self.adapter.run
        else:
            function = self.adapter
        signature = inspect.signature(function)
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD
               for parameter in signature.parameters.values()):
            return function(**kwargs)
        accepted = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return function(**accepted)

    @staticmethod
    def _trace_summary(result, group):
        trace = result.get('trace') if isinstance(result, dict) else None
        if not isinstance(trace, list) or result.get('trace_origin') != 'runtime_audit' or result.get('gui_real') is not True:
            return {'evidence_kind': 'inconclusive', 'model_agent_ids': [],
                    'delegations': [], 'memory_calls': [], 'trace_proven': False}
        if not isinstance(trace, list):
            return {'evidence_kind': 'inconclusive', 'model_agent_ids': [],
                    'delegations': [], 'memory_calls': [], 'trace_proven': False}
        models = sorted({str(item.get('model_agent_id')) for item in trace
                         if item.get('event') in {'model_call', 'model.called'} and item.get('model_agent_id')})
        delegations = [item for item in trace if item.get('event') in {'delegation', 'subtask.started'}]
        memory = [item for item in trace if str(item.get('event', '')).startswith('memory.')]
        layers = [item.get('layer') for item in memory if item.get('layer')]
        off_clean = group.cross_run_memory or all(layer in {'L0', 'L1', None} for layer in layers)
        proven = bool(models) and off_clean and (group.agent_mode == 'multi' or not delegations)
        if group.agent_mode == 'single':
            proven = proven and len(models) == 1
        if group.agent_mode == 'multi':
            proven = proven and bool(delegations) and len(models) >= 2
        return {'evidence_kind': 'real' if proven else 'inconclusive',
                'model_agent_ids': models, 'delegations': delegations,
                'memory_calls': memory, 'trace_proven': proven}

    def run_group(self, group: AblationGroup) -> dict[str, Any]:
        fixed = self.config.fixed_inputs()
        if fixed != self._fixed_inputs:
            raise RuntimeError('固定评测输入在分组间发生漂移')
        path = self._group_dir(group)
        run_id = f'eval-{self.config.case_id}-{group.name}'
        source = path / 'source'
        revision = _copy_source(self.config.source_root, self.config.source_revision, source)
        if revision != self.config.source_revision:
            raise RuntimeError('源码导出 revision 与固定输入不一致')
        if self.config.mutation_file:
            target = source / self.config.mutation_file
            if target.is_symlink() or not target.is_file():
                raise ValueError('缺陷注入目标必须是源码内普通文件')
            content = target.read_text(encoding='utf-8')
            if content.count(self.config.mutation_before) != 1:
                raise ValueError('缺陷注入 before 片段不存在')
            target.write_text(content.replace(self.config.mutation_before, self.config.mutation_after, 1), encoding='utf-8')
        source_hash = _tree_hash(source)
        environment_digest = fixed['environment_digest']
        binding = RunBinding(group.name, run_id, self.config.case_id, self.config.family,
            revision, source_hash, environment_digest,
            self.config.seed, fixed['spec_hash'], fixed['protocol_hash'], self.config.model,
            self.config.effort, self.config.tools_hash, fixed['skills_hash'],
            fixed['initial_experience_hash'], self.config.recovery_rules_hash)
        env = self._environment(group, path, fixed)
        started = time.monotonic()
        try:
            with self._environment_context(env):
                value = self._invoke({'command': self.config.command, 'cwd': str(source),
                                      'env': env, 'timeout': self.config.timeout_seconds,
                                      'binding': binding.as_dict(), 'config': self.config,
                                      'group': group, 'source': source, 'root': path}) or {}
            if inspect.isawaitable(value):
                raise TypeError('runner adapter 必须是同步边界；异步调用请在外层显式驱动')
            result = dict(value)
        except Exception as error:
            result = {'status': 'inconclusive', 'outcome': 'INFRA_FAILURE',
                      'infra_failure': True, 'error': str(error)}
        summary = self._trace_summary(result, group)
        candidate = Path(result['candidate_patch']) if result.get('candidate_patch') else None
        if candidate and not candidate.is_absolute():
            candidate = (source / candidate).resolve()
        if candidate and (not candidate.is_file() or not candidate.is_relative_to(path)):
            result.update(status='inconclusive', outcome='INFRA_FAILURE', infra_failure=True,
                          error='candidate patch escapes isolated group directory')
            candidate = None
        patch_hash = _sha256(candidate) if candidate and candidate.is_file() else result.get('patch_hash')
        materialized_hash = result.get('materialized_patch_hash')
        verify_candidate = result.get('verify_candidate')
        if callable(verify_candidate):
            materialized_hash = verify_candidate()
        if patch_hash and result.get('patch_hash') and patch_hash != result['patch_hash']:
            result.update(status='inconclusive', outcome='INFRA_FAILURE', infra_failure=True,
                          error='candidate patch hash mismatch')
        if patch_hash and materialized_hash and patch_hash != materialized_hash:
            result.update(status='inconclusive', outcome='INFRA_FAILURE', infra_failure=True,
                          error='materialized workspace patch hash mismatch')
        binding = RunBinding(**{**binding.as_dict(), 'patch_hash': patch_hash})
        oracle_result = None
        if self.oracle and candidate and candidate.is_file() and not result.get('infra_failure'):
            oracle_result = self.oracle.score(binding.as_dict(), candidate,
                verify_candidate=verify_candidate if callable(verify_candidate)
                else lambda: materialized_hash)
            oracle_result = self._read_oracle_record(binding, oracle_result)
        isolation_proven = all((oracle_result or {}).get(field, {}).get('real_isolation') is True
                               for field in ('boundary', 'boundary_after'))
        summary['oracle_isolation_proven'] = isolation_proven
        if not isolation_proven or result.get('infra_failure'):
            summary['evidence_kind'] = 'inconclusive'
        row = {'attempted': True, 'configuration': group.name, 'case_id': self.config.case_id,
               'family': self.config.family, 'run_id': run_id, 'agent_mode': group.agent_mode,
               'cross_run_memory': group.cross_run_memory, 'outcome': result.get('outcome'),
               'status': result.get('status'), 'patch_hash': patch_hash,
               'oracle_patch_hash': (oracle_result or {}).get('candidate_patch_hash')
                   or (oracle_result or {}).get('materialized_patch_hash'),
               'oracle_passed': (oracle_result or {}).get('oracle_passed'),
               'oracle_status': (oracle_result or {}).get('status'),
               'infra_failure': bool(result.get('infra_failure')),
               'usage': result.get('usage'), 'duration_seconds': time.monotonic() - started,
               'recovery_attempted': bool(result.get('recovery_attempted')),
               'recovery_effective': bool(result.get('recovery_effective')),
               'internal_success': bool(result.get('internal_success')),
               'evidence_kind': summary['evidence_kind'], 'trace_summary': summary,
               'binding': binding.as_dict(), 'materialized_patch_hash': materialized_hash}
        self.rows.append(row)
        return row

    @staticmethod
    @contextlib.contextmanager
    def _environment_context(env):
        previous = {key: os.environ.get(key) for key in env if key.startswith('TRACEFIX_')}
        try:
            os.environ.update({key: str(value) for key, value in previous.items()
                               if value is not None})
            os.environ.update({key: str(value) for key, value in env.items()
                               if key.startswith('TRACEFIX_')})
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _read_oracle_record(self, binding, acknowledgement):
        """Read score only from evaluator-private Oracle ledger."""
        ledger = getattr(self.oracle, 'private_root', None)
        ledger = Path(ledger) / 'ledger.jsonl' if ledger else None
        if not ledger or not ledger.is_file():
            return acknowledgement
        key = getattr(self.oracle, '_request_key', lambda value: None)(binding.as_dict())
        records = [json.loads(line) for line in ledger.read_text(encoding='utf-8').splitlines() if line.strip()]
        return next((record for record in reversed(records) if record.get('request_key') == key), acknowledgement)

    def run_all(self, groups=GROUPS):
        for group in groups:
            self.run_group(group)
        return list(self.rows)

    def write_jsonl(self, path):
        Path(path).write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in self.rows) + '\n',
                              encoding='utf-8')
