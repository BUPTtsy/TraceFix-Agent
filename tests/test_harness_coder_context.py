import json
from unittest.mock import AsyncMock

import pytest

from tracefix.agents.claude_sdk_adapter import ExecutionInspection
from tracefix.runtime import harness as harness_module
from tracefix.runtime.harness import (EVIDENCE_LIMIT, INSTRUCTIONS_LIMIT, cleanup_mountpoints,
    coder_evidence, export_candidate_safely, scrub, worker_supports_toolchain)
from tracefix.runtime.harness_bootstrap import default_worker_capabilities
from tracefix.runtime.smoke import make_engine

from tests.test_harness_graph import create_harness_engine


@pytest.fixture
def tmp_path(tmp_path_factory):
    return tmp_path_factory.mktemp('hc')


IMAGE = 'sha256:' + 'b' * 64


def capabilities_returning(value):
    async def capabilities(worker_image):
        if isinstance(value, BaseException):
            raise value
        return value
    return capabilities


def seed_coder_inputs(engine, harness, *, image_ids=None, inspect_results=None):
    """在 coder 调用前补齐宿主侧证据与项目约束，并按需模拟已解析的 app 镜像。"""
    original = harness.code

    async def code(current, state):
        baseline = current.put(state, {'target': 'unit', 'exit_code': 1,
            'executed_argv': ['node', '--test'], 'output': 'not ok 1 - 断言失败标记 expected true'},
            name='复现_非GUI失败基线')
        card = current.put(state, {'path': 'src/value.ts', 'content': '源码卡片噪音'}, name='诊断源码片段')
        oversized = current.put(state, {'output': '复' * (EVIDENCE_LIMIT * 3)}, name='复现_超长输出')
        instructions = current.artifacts.put(state.scope_id, state.run_id,
            '项目约束标记 token=abcdef123456\n' + '约' * INSTRUCTIONS_LIMIT, 'txt', label='项目约束')
        state = current.changed(state, evidence_refs=list(dict.fromkeys(
            state.evidence_refs + [card, baseline, oversized])), agent_instructions_ref=instructions)
        if image_ids is not None:
            current.runner.resolved_image_ids = image_ids
        if inspect_results is not None:
            current.runner.inspect_results = list(inspect_results)
        return await original(current, state)

    harness.code = code


async def test_coder_prompt_injects_evidence_instructions_and_skills_within_budget(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    seed_coder_inputs(engine, harness)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    initializer = next(request for request in executor.requests if request.role == 'initializer')
    assert initializer.toolchain_image == ''
    coder = next(request for request in executor.requests if request.role == 'coder')
    assert coder.toolchain_image == '' and 'toolchain_image' not in coder.model_dump()
    payload = json.loads(coder.prompt)
    find = lambda word: next(item for item in payload['evidence'] if word in item['label'])
    assert '断言失败标记' in find('复现_非GUI失败基线')['content']
    assert all(item['ref'] for item in payload['evidence'])
    assert not any('源码片段' in item['label'] for item in payload['evidence'])
    assert '源码卡片噪音' not in coder.prompt
    assert any('诊断源码片段' in ref for ref in payload['evidence_skipped'])
    long = find('复现_超长输出')
    assert long['truncated'] is True and long['original_length'] > EVIDENCE_LIMIT * 3
    assert len(long['content']) < EVIDENCE_LIMIT + 50
    # 复现类证据优先于其它证据排序。
    assert '复现' in payload['evidence'][0]['label']
    project = payload['project_instructions']
    assert project['content'].startswith('项目约束标记') and project['truncated'] is True
    assert 'abcdef123456' not in coder.prompt
    assert 'skills' in payload and 'skill_index' in payload and 'skill_unavailable' in payload
    assert sum(len(skill['content']) for skill in payload['skills']) <= 12000 + 50 * len(payload['skills'])
    assert 'self_test_commands' not in payload
    assert '不可信' in payload['instruction'] and 'Read/Edit' in payload['instruction']


@pytest.mark.parametrize('case', ['matched', 'drifted', 'invalid', 'missing',
                                  'no_capabilities', 'without_toolchain', 'capabilities_error'])
async def test_coder_toolchain_image_requires_capability_and_frozen_environment_match(tmp_path, case):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    image_ids = {'invalid': {'app': 'sha256:short'}, 'missing': {}}.get(case, {'app': IMAGE})
    harness.worker_capabilities = {
        'no_capabilities': None,
        'without_toolchain': capabilities_returning(frozenset({'structured_output'})),
        'capabilities_error': capabilities_returning(RuntimeError('worker 镜像无法探测')),
    }.get(case, capabilities_returning(frozenset({'toolchain'})))
    seed_coder_inputs(engine, harness, image_ids=image_ids,
                      inspect_results=['drifted-environment'] if case == 'drifted' else None)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    initializer = next(request for request in executor.requests if request.role == 'initializer')
    assert initializer.toolchain_image == ''
    coder = next(request for request in executor.requests if request.role == 'coder')
    payload = json.loads(coder.prompt)
    if case == 'matched':
        assert coder.toolchain_image == IMAGE
        expected = [command for kind, command in engine.profile.commands.items()
                    if kind not in {'start', 'reset', 'prepare'}]
        assert payload['self_test_commands'] == expected and expected
        assert 'npm install' in payload['instruction']
    else:
        assert coder.toolchain_image == '' and 'toolchain_image' not in coder.model_dump()
        assert 'self_test_commands' not in payload


async def test_worker_capability_probe_degrades_on_absent_error_or_missing_flag():
    assert await worker_supports_toolchain(None, 'img') is False
    assert await worker_supports_toolchain(capabilities_returning(frozenset()), 'img') is False
    assert await worker_supports_toolchain(capabilities_returning(None), 'img') is False
    assert await worker_supports_toolchain(capabilities_returning(OSError('x')), 'img') is False
    assert await worker_supports_toolchain(capabilities_returning(frozenset({'toolchain'})), 'img') is True
    seen = []
    assert await worker_supports_toolchain(lambda image: seen.append(image) or {'toolchain'}, 'img')
    assert seen == ['img']


def test_bootstrap_only_probes_default_runner(monkeypatch):
    from tracefix.execution import agent_runner

    probe = capabilities_returning(frozenset({'toolchain'}))
    monkeypatch.setattr(agent_runner, 'worker_capabilities', probe, raising=False)
    assert default_worker_capabilities(None) is probe
    assert default_worker_capabilities(lambda *args, **kwargs: None) is None
    monkeypatch.delattr(agent_runner, 'worker_capabilities', raising=False)
    assert default_worker_capabilities(None) is None


async def test_frozen_invocation_reentry_does_not_rebuild_prompt(tmp_path):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    payload = json.dumps({'approved_commands': engine.profile.commands, 'acceptance_refs': [state.test_spec_ref]})
    calls = []

    async def first():
        calls.append('first')
        return payload, ''

    def second():
        calls.append('second')
        return 'changed transient runtime view', IMAGE

    state, execution, result = await harness._execute(engine, state, 'initializer', first)
    again_state, again, again_result = await harness._execute(engine, state, 'initializer', second)
    assert calls == ['first']
    assert (again.execution_id, again.input_hash, again_result) == (execution.execution_id,
                                                                     execution.input_hash, result)
    assert len(executor.requests) == 1


async def test_coder_reentry_skips_context_and_toolchain_recomputation(tmp_path, monkeypatch):
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    counts = {'context': 0, 'toolchain': 0}
    original_context, original_toolchain = harness_module.coder_context, harness_module.toolchain_image

    def counted_context(*args, **kwargs):
        counts['context'] += 1
        return original_context(*args, **kwargs)

    async def counted_toolchain(*args, **kwargs):
        counts['toolchain'] += 1
        return await original_toolchain(*args, **kwargs)

    monkeypatch.setattr(harness_module, 'coder_context', counted_context)
    monkeypatch.setattr(harness_module, 'toolchain_image', counted_toolchain)
    original_execute = harness._execute

    async def reenter(current, active, role, prompt, **kwargs):
        first = await original_execute(current, active, role, prompt, **kwargs)
        if role != 'coder':
            return first
        # 同一逻辑 invocation 立即重入：必须复用冻结结果且不再重算上下文。
        second = await original_execute(current, first[0], role, prompt, **kwargs)
        assert second[1].execution_id == first[1].execution_id
        return second

    monkeypatch.setattr(harness, '_execute', reenter)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    coders = [request for request in executor.requests if request.role == 'coder']
    assert len(coders) == 2
    assert counts == {'context': 2, 'toolchain': 2}


async def test_code_passes_sdk_credential_as_forbidden_candidate_value(tmp_path, monkeypatch):
    from tracefix.execution import candidate_export

    monkeypatch.setenv('TEST_ANTHROPIC_KEY', 'sk-ant-forbidden-credential')
    engine, state, harness, executor = await create_harness_engine(tmp_path)
    original = candidate_export.export_candidate
    observed = []

    def export(*args, forbidden_values=(), **kwargs):
        observed.append(list(forbidden_values))
        return export_candidate_safely(original, *args, forbidden_values=forbidden_values, **kwargs)

    monkeypatch.setattr(candidate_export, 'export_candidate', export)
    output = await engine.run(state)
    assert harness.job(state).status == 'COMPLETED', output['data'].get('error')
    assert observed and all(values == ['sk-ant-forbidden-credential'] for values in observed)


def test_export_adapter_only_passes_forbidden_values_when_supported():
    def legacy(*args, **kwargs):
        return kwargs

    def modern(*args, forbidden_values=(), **kwargs):
        return {**kwargs, 'forbidden_values': forbidden_values}

    assert export_candidate_safely(legacy, summary='s', forbidden_values=['secret-value']) == {'summary': 's'}
    assert export_candidate_safely(modern, summary='s', forbidden_values=['secret-value'])['forbidden_values'] \
        == ['secret-value']
    assert export_candidate_safely(modern, forbidden_values=[])['forbidden_values'] == []


@pytest.mark.parametrize('raw, secret', [
    ('Cookie: sid=abc123secret; theme=dark', 'abc123secret'),
    ('set-cookie: session=zzz999secret; Path=/', 'zzz999secret'),
    ('Authorization: Basic dXNlcjpwYXNzd29yZA==', 'dXNlcjpwYXNzd29yZA'),
    ('{"headers": [["Cookie", "sid=abc123secret"]]}', 'abc123secret'),
    ('{"h": ["Set-Cookie","token=abc123secret; HttpOnly"]}', 'abc123secret'),
    ("('Authorization', 'Bearer abc123secret')", 'abc123secret'),
    ('{"Proxy-Authorization": "Basic abc123secret"}', 'abc123secret'),
    ('"{\\"Cookie\\": \\"sid=abc123secret\\"}"', 'abc123secret'),
    ('clone https://user:p4ssw0rd@github.com/org/repo.git', 'p4ssw0rd'),
    ('push to http://oauth2:abc123secret@gitlab.example/x', 'abc123secret'),
    ('ghp_' + 'A' * 36, 'A' * 36),
    ('gho_' + 'b' * 36, 'b' * 36),
    ('github_pat_' + 'C1' * 20, 'C1' * 20),
    ('xoxb-1234567890-abcdefghij', '1234567890-abcdefghij'),
    ('xoxp-1234567890-abcdefghij', '1234567890-abcdefghij'),
    ('jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N', 'dozjgNryP4J3jVmNHl0w5N'),
    ('aws AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
    ('GET /cb?code=1&access_token=abc123secret&x=1', 'abc123secret'),
    ('GET /p?token=abc123secret', 'abc123secret'),
    ('GET /p?a=1&session=abc123secret#frag', 'abc123secret'),
    ('GET /p?sid=abc123secret', 'abc123secret'),
    ('GET /p?api_key=abc123secret', 'abc123secret'),
])
def test_scrub_removes_secret_shapes(raw, secret):
    cleaned = scrub(raw)
    assert secret not in cleaned and '[REDACTED]' in cleaned


def test_scrub_keeps_ordinary_text_and_json_shape():
    text = 'not ok 1 - expected true; see https://github.com/org/repo/issues?page=2 and Cookie banner'
    assert scrub(text) == text
    value = json.dumps({'headers': [['Cookie', 'sid=abc123secret'], ['Accept', 'text/html']]})
    assert json.loads(scrub(value)) == {'headers': [['Cookie', '[REDACTED]'], ['Accept', 'text/html']]}


def test_coder_evidence_records_unreadable_refs_without_failing(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    good = engine.put(state, {'output': '观察输出'}, name='页面观察')
    index = engine.put(state, {'files': []}, name='文件索引')
    state = state.model_copy(update={'evidence_refs': [index, '9999_复现_缺失.json', good]})
    result = coder_evidence(engine, state)
    assert [item['ref'] for item in result['evidence']] == [good]
    assert result['evidence_unavailable'][0]['ref'] == '9999_复现_缺失.json'
    assert result['evidence_skipped'] == [index]


def test_coder_evidence_priority_newest_first_and_skips_tool_records(tmp_path):
    engine, state = make_engine(tmp_path / 'engine')
    put = lambda label, value: engine.artifacts.put(state.scope_id, state.run_id, value, label=label)
    old_check = put('探索_逐项检查结果', {'output': '旧检查'})
    tool = put('探索_代码工具检查证据', {'output': 'Read 记录'})
    structure = put('探索_代码结构检查证据', {'output': '结构'})
    manifest = put('探索_上下文组装清单', {'files': []})
    full = put('探索_工具完整结果', {'output': '完整'})
    short = put('探索_工具结果', {'output': '短'})
    observe = put('探索_页面观察', {'output': '观察'})
    clue = put('诊断_混合修复起点源码线索', {'output': 'value.ts 第 3 行', 'cookie': 'x'})
    rollback = put('修复_代码回退摘要', {'output': '回退摘要 https://u:pw123456@h/x'})
    old_repro = put('探索_复现_旧', {'output': '旧复现'})
    new_repro = put('验证_复现_新', {'output': '新复现'})
    new_check = put('验证_逐项检查结果', {'output': '新检查'})
    state = state.model_copy(update={'evidence_refs': [old_check, tool, structure, manifest, full,
        short, observe, clue, rollback, old_repro, new_repro], 'initial_check_result_refs': [new_check]})
    result = coder_evidence(engine, state)
    order = [item['ref'] for item in result['evidence']]
    # 复现（新在前）→ 线索 → 回退 → 观察 → 检查（新在前）。
    assert order == [new_repro, old_repro, clue, rollback, observe, new_check, old_check]
    assert set(result['evidence_skipped']) == {tool, structure, manifest, full, short}
    assert 'pw123456' not in json.dumps(result, ensure_ascii=False)


async def test_cleanup_mountpoints_handles_sync_async_absent_and_propagates_errors():
    calls = []

    class SyncRunner:
        def cleanup_mountpoints(self):
            calls.append('sync')

    class AsyncRunner:
        async def cleanup_mountpoints(self):
            calls.append('async')

    class Broken:
        def cleanup_mountpoints(self):
            raise OSError('挂载点清理失败')

    await cleanup_mountpoints(SyncRunner())
    await cleanup_mountpoints(AsyncRunner())
    await cleanup_mountpoints(object())
    assert calls == ['sync', 'async']
    with pytest.raises(OSError, match='挂载点清理失败'):
        await cleanup_mountpoints(Broken())


async def test_stop_cleans_mountpoints_before_freezing_snapshot(tmp_path, monkeypatch):
    from tracefix.execution.candidate_export import SDKWorkspace

    engine, state, harness, executor = await create_harness_engine(tmp_path)
    executor.inspect = AsyncMock(return_value=ExecutionInspection(
        execution_id='checked', status='STOPPED', active=False))
    state, execution, result = await harness._execute(engine, state, 'initializer',
        json.dumps({'approved_commands': engine.profile.commands,
                    'acceptance_refs': [state.test_spec_ref]}))
    order = []
    executor.cleanup_mountpoints = lambda: order.append('cleanup')
    original_freeze = SDKWorkspace.freeze

    def freeze(self, *args, **kwargs):
        order.append('freeze')
        return original_freeze(self, *args, **kwargs)

    monkeypatch.setattr(SDKWorkspace, 'freeze', freeze)
    job = harness.job(state)
    harness.store.request_stop(job.job_id, job.revision, scope_id=state.scope_id)
    stopped = await harness.stop(engine, state)
    assert stopped.status == 'STOPPED'
    assert order[:2] == ['cleanup', 'freeze']
