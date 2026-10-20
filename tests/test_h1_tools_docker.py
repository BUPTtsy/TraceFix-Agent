"""真实 Docker 验收，固定目标提交，所有注入仅作用于临时工作区。"""
import os
import subprocess
from pathlib import Path

import pytest

from tracefix.config import load_profile
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import PatchProposal, Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools.handlers import build_runtime_tools


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('TRACEFIX_TEST_LOCAL_DOCKER') != '1', reason='opt-in Docker test')
async def test_real_run_tests_unit_u01_and_bash_typescript(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixed = 'e7a9c04002a2d51a14d37da1f4808c2ee4695c57'
    source = root / 'bugboard' / 'target'
    repository = tmp_path / 'repository'
    subprocess.run(['git', 'clone', '--no-hardlinks', '--quiet', str(source), str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'checkout', '--quiet', '--detach', fixed], check=True)
    engine, state = make_engine(tmp_path / 'harness')
    project = engine.scopes.projects[state.scope_id]
    project.root = repository
    project.allowed_files = ['src/**', 'server/**']
    context = engine.scopes.context(state.scope_id)
    workspace, snapshot = Workspace.export(engine.scopes, context, fixed, tmp_path / 'workspace')
    engine.workspace, engine.source, engine.context = workspace, snapshot, context
    engine.browser.workspace = engine.model.workspace = workspace
    engine.profile = load_profile(root / 'profiles' / 'bugboard.yaml')
    engine.profile.source_commit = fixed
    state.source_manifest = digest(snapshot)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, snapshot)
    state.mode, state.phase = 'repair', Phase.DIAGNOSE
    state.patch_base_commit = workspace.head()
    engine.store.save(state)
    runner = engine.runner = DockerRunner(engine.profile, workspace, state.run_id)
    baseline = (workspace.root / 'server/tasks.mjs').read_bytes()
    try:
        prepared = await runner.start(state.source_manifest)
        assert prepared['passed'], prepared['output']
        pipeline = build_runtime_tools(engine, state, PatchProposal).pipeline()
        clean = await pipeline.execute('RunTests', {'target': 'unit'}, 'clean-unit')
        assert not clean.is_error, clean.error
        assert clean.result['passed'], clean.result
        assert clean.result['summary'] == {'passed': 4, 'failed': 0, 'skipped': 0, 'unparsed': False}
        mutated = baseline.decode().replace('return title.trim();', 'return title;')
        assert mutated != baseline.decode()
        edit = await pipeline.execute('Write', {
            'file_path': str(workspace.root / 'server/tasks.mjs'), 'content': mutated}, 'inject-u01')
        assert not edit.is_error, edit.error
        broken = await pipeline.execute('RunTests', {'target': 'unit'}, 'u01-unit')
        assert not broken.is_error, broken.error
        assert not broken.result['passed'] and broken.result['summary']['failed'] == 1
        assert any(case['name'] == 'title validation rejects blank input' and case['status'] == 'failed'
                   for case in broken.result['cases'])
        assert broken.result['failure_signatures']
        assert clean.result['diff_fingerprint'] != broken.result['diff_fingerprint']
        static = await pipeline.execute('Bash', {
            'command': 'node node_modules/typescript/bin/tsc --noEmit',
            'description': '验证只读依赖卷可供 TypeScript 使用'}, 'typescript')
        assert not static.is_error, static.error
        assert static.result['passed'], static.result
        assert (workspace.root / 'server/tasks.mjs').read_bytes() == baseline
        assert workspace.staged_content('server/tasks.mjs') == mutated.encode()
        assert state.phase == Phase.DIAGNOSE and not state.validation_refs
    finally:
        await runner.close()
