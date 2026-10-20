"""显式开启的真实模型 + Docker 内层修复冒烟，所有缺陷注入仅位于临时克隆。"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from tracefix.config import load_profile
from tracefix.execution.repository import safe_index_files
from tracefix.execution.runner import DockerRunner
from tracefix.execution.workspace import Workspace, commit_workspace
from tracefix.model.gateway import Gateway
from tracefix.runtime.contracts import FinishRepair, Phase, RunState, digest
from tracefix.runtime.repair_progress import calibrate_repair_traces, replay_repair_events
from tracefix.runtime.smoke import make_engine
from tracefix.tools.handlers import build_runtime_tools


@pytest.mark.skipif(os.getenv('TRACEFIX_TEST_HYBRID_LIVE') != '1', reason='opt-in real model and Docker test')
async def test_real_model_repairs_u01_through_overlay_and_run_tests(tmp_path):
    root = Path(__file__).resolve().parents[1]
    from dotenv import load_dotenv
    load_dotenv(root / '.env')
    assert os.getenv('TRACEFIX_API_KEY'), '真实模型冒烟需要配置 API key'
    fixed = 'e7a9c04002a2d51a14d37da1f4808c2ee4695c57'
    repository = tmp_path / 'repository'
    subprocess.run(['git', 'clone', '--no-hardlinks', '--quiet', str(root / 'bugboard/target'),
                    str(repository)], check=True)
    subprocess.run(['git', '-C', str(repository), 'checkout', '--quiet', '--detach', fixed], check=True)
    source = repository / 'server/tasks.mjs'
    original = source.read_bytes()
    broken = original.replace(b'return title.trim();', b'return title;')
    assert broken != original
    source.write_bytes(broken)
    safe_index_files(repository, ['server/tasks.mjs'])
    commit_workspace(repository, '注入混合修复冒烟的标题校验缺陷')
    engine, state = make_engine(tmp_path / 'harness')
    project = engine.scopes.projects[state.scope_id]
    project.root, project.allowed_files = repository, ['src/**', 'server/**']
    context = engine.scopes.context(state.scope_id)
    workspace, snapshot = Workspace.export(engine.scopes, context, 'HEAD', tmp_path / 'workspace')
    engine.workspace, engine.source, engine.context = workspace, snapshot, context
    engine.browser.workspace = workspace
    engine.profile = load_profile(root / 'profiles/bugboard.yaml')
    engine.profile.source_commit = snapshot['commit']
    engine.model = Gateway(max_output_tokens=4096, max_tool_rounds=12, timeout=120)
    state.source_manifest = digest(snapshot)
    state.repo_snapshot_ref = engine.put(state, snapshot)
    state.phase, state.repair_mode, state.repair_mode_requested = Phase.DIAGNOSE, 'hybrid', 'hybrid'
    state.patch_base_commit = workspace.head()
    state.goal = ('修复 BugBoard 标题校验：空白标题必须被拒绝，合法标题需去除首尾空白。'
                  '先 Read 定位并 RunTests(unit) 观测失败，再编辑实现并 RunTests(unit) 验证；'
                  '仅修改 server/tasks.mjs，不改测试，成功后 FinishRepair 提交。')
    engine.store.save(state)
    runner = engine.runner = DockerRunner(engine.profile, workspace, state.run_id)
    try:
        prepared = await runner.start(state.source_manifest)
        assert prepared['passed'], prepared['output']
        pipeline = build_runtime_tools(engine, state, FinishRepair).pipeline()
        failed = await pipeline.execute('RunTests', {'target': 'unit'}, 'baseline-u01')
        assert not failed.is_error and not failed.result['passed'], failed
        state.evidence_refs = [failed.result['result_ref']]
        # 本冒烟从已知失败的单元测试起点进入内循环，GUI 复现和 Finding 门禁另有专项。
        state.reproduced = state.source_aligned = True
        state.failure_signatures = failed.result['failure_signatures']
        engine.store.save(state)
        state = RunState(**(await engine.repair_loop(state, None))['data'])
        assert state.phase == Phase.PATCH, state.error
        proposal = engine.get(state, state.patch_ref)
        assert [edit['path'] for edit in proposal['edits']] == ['server/tasks.mjs']
        assert workspace.path('server/tasks.mjs').read_bytes() == broken
        checks = build_runtime_tools(engine, state.model_copy(update={'phase': Phase.DIAGNOSE}), FinishRepair).pipeline()
        passed = await checks.execute('RunTests', {'target': 'unit'}, 'independent-overlay-unit')
        assert not passed.is_error and passed.result['passed'], passed
        assert state.repair_segment == 1 and not state.validation_refs
        summary = {'kind': 'real_model_docker_smoke', 'run_id': state.run_id,
            'scope_id': state.scope_id, 'source_manifest': state.source_manifest,
            'baseline_failed': failed.result['summary'], 'overlay_passed': passed.result['summary'],
            'repair_segment': state.repair_segment, 'patch_files': [edit['path'] for edit in proposal['edits']],
            'repair': state.loop_evidence.get('repair'), 'model_exchange_refs': state.model_exchange_refs}
        records = engine.store.trace(state.run_id, state.scope_id)
        replayed = replay_repair_events(records, artifact_read=lambda ref: engine.get(state, ref))
        assert not replayed['incomplete'], replayed['incomplete']
        assert not any(item['signals'] for item in replayed['assessments'])
        summary['calibration'] = calibrate_repair_traces([
            {'source': 'real', 'expected_loop': False, 'records': records}],
            artifact_read=lambda ref: engine.get(state, ref))
        (tmp_path / 'trace.jsonl').write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n'
                                                    for record in records), encoding='utf-8')
        (tmp_path / 'live-smoke.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        await runner.close()
