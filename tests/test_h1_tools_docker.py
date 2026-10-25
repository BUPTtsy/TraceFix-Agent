"""真实 Docker 验收，固定目标提交，所有注入仅作用于临时工作区。"""
import os
import subprocess
from pathlib import Path

import pytest

from tracefix.config import load_profile
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace
from tracefix.execution.candidate_export import SDKWorkspace, export_candidate, materialize_candidate
from tracefix.runtime.contracts import Phase, digest
from tracefix.runtime.smoke import make_engine
from tracefix.tools.core import ToolProtocolError
from tracefix.tools.handlers import build_runtime_tools
from bugboard.scripts.prepare_target import prepare_target


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv('TRACEFIX_TEST_LOCAL_DOCKER') != '1', reason='opt-in Docker test')
async def test_real_docker_verifies_exported_sdk_candidate_and_typescript(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixed = 'e7a9c04002a2d51a14d37da1f4808c2ee4695c57'
    source = prepare_target(check_cases=True)
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
        pipeline = build_runtime_tools(engine, state, None).pipeline()
        with pytest.raises(ToolProtocolError):
            await pipeline.execute('Write', {
                'file_path': str(workspace.root / 'server/tasks.mjs'), 'content': 'forbidden'}, 'host-write')
        clean = await runner.command('unit')
        assert clean['passed'] and clean['exit_code'] == 0, clean
        assert '# pass 4' in clean['output'] and '# fail 0' in clean['output']
        mutated = baseline.decode().replace('return title.trim();', 'return title;')
        assert mutated != baseline.decode()
        sdk = SDKWorkspace.create(workspace, tmp_path / 'sdk')
        (sdk.source / 'server/tasks.mjs').write_bytes(mutated.encode('utf-8'))
        proposal = export_candidate(sdk, workspace, summary='U01 候选', evidence_refs=[state.repo_snapshot_ref])
        candidate = materialize_candidate(sdk, proposal, tmp_path / 'candidate')
        candidate.apply(proposal)
        await runner.close()
        runner = DockerRunner(engine.profile, candidate, state.run_id + '-candidate')
        prepared = await runner.start(digest(candidate.require_repository_snapshot()))
        assert prepared['passed'], prepared['output']
        broken = await runner.command('unit')
        assert not broken['passed'] and broken['exit_code'] != 0, broken
        assert '# fail 1' in broken['output']
        assert 'not ok' in broken['output'] and 'title validation rejects blank input' in broken['output']
        static = await runner.command('static')
        assert static['passed'] and static['exit_code'] == 0, static
        assert (workspace.root / 'server/tasks.mjs').read_bytes() == baseline
        assert candidate.read('server/tasks.mjs') == mutated
        assert candidate.diff() and not workspace.diff()
        assert state.phase == Phase.DIAGNOSE and not state.validation_refs
    finally:
        await runner.close()
