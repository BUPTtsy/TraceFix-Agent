import asyncio
import builtins
import importlib
import importlib.util
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import tracefix.agents.deepagents_adapter as adapter_module
from tracefix.agents.deepagents_adapter import (
    DeepAgentsBoundaryError,
    DeepAgentsInvestigation,
    DeepAgentsReadonlyAdapter,
)
from tracefix.runtime.worker import SubtaskResult


def make_request(**overrides):
    values = {
        'goal': '调查授权源码并整理证据支持的假设',
        'allowed_files': ('src/app.py', 'src/lib.py'),
        'allowed_evidence_refs': ('evidence:one', 'evidence:two'),
        'source_manifest': 'manifest-current',
        'worker_generation': 7,
    }
    values.update(overrides)
    return DeepAgentsInvestigation(**values)


def make_result(**overrides):
    values = {
        'status': 'completed',
        'summary': '已核对状态流转和现有证据',
        'evidence_refs': ['evidence:one'],
        'files': ['src/app.py'],
        'suggested_experiments': ['读取同版候选实现核对状态流转'],
        'unresolved': [],
        'worker_generation': 7,
        'source_manifest': 'manifest-current',
        'hypotheses': [{
            'id': 'state-transition',
            'summary': '候选实现可能遗漏状态更新',
            'support_refs': ['evidence:one'],
            'counterevidence_refs': ['evidence:two'],
            'candidate_paths': [{
                'path': 'src/app.py',
                'content_version': 'app-content-hash',
            }],
            'prediction': '刷新后状态应与当前输入一致',
            'minimal_probe': '只读核对状态赋值路径',
            'status': 'plausible',
        }],
    }
    values.update(overrides)
    return values


def assert_limited_result(result, request, code, status='rejected'):
    assert isinstance(result, SubtaskResult)
    assert result.status == status
    assert code in result.gaps
    assert result.worker_generation == request.worker_generation
    assert result.source_manifest == request.source_manifest
    assert result.files == []
    assert result.evidence_refs == []
    assert result.support_refs == []
    assert result.counterevidence_refs == []
    assert result.artifact_refs == []
    assert result.hypotheses == []


@pytest.mark.parametrize('phase', ['DIAGNOSE', 'EXPLORE'])
async def test_readonly_success_preserves_structured_context_and_callback(phase):
    calls = []
    callbacks = []
    contents = {'src/app.py': 'state = load()', 'src/lib.py': 'def load(): return 1'}
    request = make_request(phase=phase)

    def read(*, path):
        calls.append(('Read', path))
        return contents[path]

    def grep(*, pattern, path):
        calls.append(('Grep', pattern, path))
        return contents[path] if pattern in contents[path] else ''

    def glob(*, pattern, allowed_files):
        calls.append(('Glob', pattern, allowed_files))
        return list(allowed_files)

    async def runner(**kwargs):
        assert set(kwargs) == {'request', 'tools', 'result_type'}
        assert kwargs['request'] == request
        assert kwargs['result_type'] is SubtaskResult
        tools = kwargs['tools']
        assert set(tools) == {'Read', 'Glob', 'Grep'}
        with pytest.raises(TypeError):
            tools['Read'] = read
        with pytest.raises(ValidationError):
            kwargs['request'].goal = '覆盖调查任务'
        assert await tools['Read'](path='src/app.py') == contents['src/app.py']
        assert await tools['Grep'](pattern='load', path='src/lib.py') == contents['src/lib.py']
        assert await tools['Glob'](pattern='src/app.*') == ['src/app.py']
        return {'structured_response': make_result(), 'messages': [{'content': 'ignored'}]}

    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={'Read': read, 'Glob': glob, 'Grep': grep},
        on_result=callbacks.append, runner=runner,
    ).run(request)
    assert result.status == 'completed'
    assert result.worker_generation == 7
    assert result.source_manifest == 'manifest-current'
    assert result.files == ['src/app.py']
    assert result.evidence_refs == ['evidence:one']
    assert result.hypotheses[0].candidate_paths[0].content_version == 'app-content-hash'
    assert result.hypotheses[0].counterevidence_refs == ['evidence:two']
    assert calls == [('Read', 'src/app.py'), ('Grep', 'load', 'src/lib.py'),
                     ('Glob', 'src/app.*', ('src/app.py',))]
    assert callbacks == [result]
    assert callbacks[0] is not result


@pytest.mark.parametrize('name', [
    'Write', 'Edit', 'Bash', 'Git', 'BrowserNavigate', 'DatabaseQuery', 'mcp.dynamic', 'write_file',
])
def test_constructor_rejects_unsafe_tool_names(name):
    with pytest.raises(DeepAgentsBoundaryError):
        DeepAgentsReadonlyAdapter(readonly_tools={name: lambda **kwargs: None})


def test_constructor_rejects_noncallable_tool():
    with pytest.raises(DeepAgentsBoundaryError):
        DeepAgentsReadonlyAdapter(readonly_tools={'Read': object()})


@pytest.mark.parametrize('name', ['Write', 'Bash', 'BrowserNavigate', 'DatabaseQuery', 'mcp.dynamic'])
async def test_runner_cannot_hide_denied_tool_access(name):
    request = make_request()

    async def runner(*, tools, **kwargs):
        with pytest.raises(DeepAgentsBoundaryError):
            tools[name]
        return make_result()

    result = await DeepAgentsReadonlyAdapter(readonly_tools={}, runner=runner).run(request)
    assert_limited_result(result, request, 'deepagents_tool_denied')


@pytest.mark.parametrize('path', [
    '', '.', 'src/./app.py', '..', '../secret.py', 'src/../app.py', '/src/app.py',
    'C:/src/app.py', 'src\\app.py', 'src/app.py:stream', 'src/*.py', 'src/app?.py',
    'src/[a]pp.py', 'src/app\x00.py', 'src/app\n.py', 'src/app\x7f.py',
])
def test_request_rejects_noncanonical_file_paths(path):
    with pytest.raises(ValidationError):
        make_request(allowed_files=(path,))


@pytest.mark.parametrize('phase', ['PATCH', 'REVIEW', 'PREPARE', 'FINALIZE'])
def test_request_rejects_unsupported_phase(phase):
    with pytest.raises(ValidationError):
        make_request(phase=phase)


def test_request_is_frozen_and_rejects_extra_fields():
    request = make_request()
    assert isinstance(request.allowed_files, tuple)
    assert isinstance(request.allowed_evidence_refs, tuple)
    with pytest.raises(ValidationError):
        request.source_manifest = 'changed'
    with pytest.raises(ValidationError):
        make_request(workspace='unauthorized')


@pytest.mark.parametrize('tool', ['Read', 'Grep'])
@pytest.mark.parametrize('path', ['src/secret.py', '../secret.py', '/src/app.py', 'src\\app.py'])
async def test_file_scope_denial_precedes_injected_callable(tool, path):
    request = make_request()
    calls = []

    def injected(**kwargs):
        calls.append(kwargs)
        return 'secret'

    async def runner(*, tools, **kwargs):
        arguments = {'path': path}
        if tool == 'Grep':
            arguments['pattern'] = 'secret'
        with pytest.raises(DeepAgentsBoundaryError):
            await tools[tool](**arguments)
        return make_result()

    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={tool: injected}, runner=runner,
    ).run(request)
    assert calls == []
    assert_limited_result(result, request, 'deepagents_scope_denied')


async def test_glob_rejects_injected_output_outside_prefiltered_scope():
    request = make_request()
    calls = []

    def glob(*, pattern, allowed_files):
        calls.append((pattern, allowed_files))
        return ['src/app.py', 'src/lib.py']

    async def runner(*, tools, **kwargs):
        await tools['Glob'](pattern='src/app.*')
        return make_result()

    result = await DeepAgentsReadonlyAdapter(readonly_tools={'Glob': glob}, runner=runner).run(request)
    assert calls == [('src/app.*', ('src/app.py',))]
    assert_limited_result(result, request, 'deepagents_scope_denied')


@pytest.mark.parametrize('field', [
    'files', 'evidence_refs', 'support_refs', 'counterevidence_refs', 'artifact_refs',
    'hypothesis_support_refs', 'hypothesis_counterevidence_refs', 'candidate_path',
])
async def test_result_rejects_all_out_of_scope_paths_and_references(field):
    request = make_request()
    payload = make_result()
    if field == 'candidate_path':
        payload['hypotheses'][0]['candidate_paths'][0]['path'] = 'src/secret.py'
    elif field.startswith('hypothesis_'):
        payload['hypotheses'][0][field.removeprefix('hypothesis_')] = ['evidence:secret']
    else:
        payload[field] = ['src/secret.py' if field == 'files' else 'evidence:secret']
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: payload,
    ).run(request)
    assert_limited_result(result, request, 'deepagents_scope_denied')


@pytest.mark.parametrize('field,value', [
    ('worker_generation', 6), ('source_manifest', 'manifest-old'),
])
async def test_result_rejects_explicit_version_mismatch(field, value):
    request = make_request()
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: make_result(**{field: value}),
    ).run(request)
    assert_limited_result(result, request, 'deepagents_version_mismatch')


async def test_result_fills_missing_snapshot_versions():
    payload = make_result()
    payload.pop('worker_generation')
    payload.pop('source_manifest')
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: payload,
    ).run(make_request())
    assert result.status == 'completed'
    assert result.worker_generation == 7
    assert result.source_manifest == 'manifest-current'


@pytest.mark.parametrize('payload', [
    '未结构化的结论',
    {'messages': [{'content': '未输出调查 DTO'}]},
    {'structured_response': None, 'messages': []},
    {'summary': '缺少必要引用和文件字段'},
    make_result(parent_state={'phase': 'PATCH'}),
])
async def test_invalid_runner_result_returns_validated_limited_callback(payload):
    request = make_request()
    callbacks = []
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: payload, on_result=callbacks.append,
    ).run(request)
    assert_limited_result(result, request, 'deepagents_invalid_result')
    assert callbacks == [result]
    assert callbacks[0] is not result


async def test_async_tools_and_runner_are_awaited():
    calls = []

    async def read(*, path):
        calls.append(path)
        return 'state = load()'

    async def runner(*, tools, **kwargs):
        assert await tools['Read'](path='src/app.py') == 'state = load()'
        return SubtaskResult.model_validate(make_result())

    result = await DeepAgentsReadonlyAdapter(readonly_tools={'Read': read}, runner=runner).run(make_request())
    assert result.status == 'completed'
    assert calls == ['src/app.py']


@pytest.mark.parametrize('async_callback', [False, True])
async def test_callback_receives_deep_copy(async_callback):
    captured = []

    def mutate(result):
        captured.append(result)
        result.files.append('src/secret.py')
        result.hypotheses[0].support_refs.append('evidence:secret')

    async def mutate_async(result):
        mutate(result)

    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: SubtaskResult.model_validate(make_result()),
        on_result=mutate_async if async_callback else mutate,
    ).run(make_request())
    assert len(captured) == 1
    assert result.files == ['src/app.py']
    assert result.hypotheses[0].support_refs == ['evidence:one']


async def test_callback_error_propagates_to_host():
    failure = RuntimeError('宿主结果回调失败')

    def callback(result):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await DeepAgentsReadonlyAdapter(
            readonly_tools={}, runner=lambda **kwargs: make_result(), on_result=callback,
        ).run(make_request())
    assert caught.value is failure


@pytest.mark.parametrize('failure', [asyncio.CancelledError(), TimeoutError('宿主超时')])
async def test_cancellation_and_timeout_propagate_without_callback(failure):
    callbacks = []

    async def runner(**kwargs):
        raise failure

    with pytest.raises(type(failure)) as caught:
        await DeepAgentsReadonlyAdapter(
            readonly_tools={}, runner=runner, on_result=callbacks.append,
        ).run(make_request())
    assert caught.value is failure
    assert callbacks == []


async def test_runner_failure_returns_recognizable_limited_result():
    request = make_request()

    def runner(**kwargs):
        raise RuntimeError('fake runner failure')

    result = await DeepAgentsReadonlyAdapter(readonly_tools={}, runner=runner).run(request)
    assert_limited_result(result, request, 'deepagents_runner_failed', status='failed')


def test_module_import_does_not_import_optional_deepagents(monkeypatch):
    original_import = builtins.__import__
    original_import_module = importlib.import_module

    def guarded_import(name, *args, **kwargs):
        assert not (name == 'deepagents' or name.startswith('deepagents.'))
        return original_import(name, *args, **kwargs)

    def guarded_import_module(name, *args, **kwargs):
        assert not (name == 'deepagents' or name.startswith('deepagents.'))
        return original_import_module(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location(adapter_module.__name__, adapter_module.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(importlib, 'import_module', guarded_import_module)
    spec.loader.exec_module(module)
    assert module.DeepAgentsReadonlyAdapter is not None


async def test_injected_runner_does_not_import_optional_deepagents(monkeypatch):
    def denied_import(*args, **kwargs):
        pytest.fail('fake runner 不应导入 DeepAgents')

    monkeypatch.setattr(importlib, 'import_module', denied_import)
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: make_result(),
    ).run(make_request())
    assert result.status == 'completed'


async def test_missing_dependency_returns_recognizable_limited_result(monkeypatch):
    imports = []
    callbacks = []
    request = make_request()

    def missing_import(name):
        imports.append(name)
        raise ModuleNotFoundError("No module named 'deepagents'", name='deepagents')

    monkeypatch.setattr(importlib, 'import_module', missing_import)
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, model=object(), on_result=callbacks.append,
    ).run(request)
    assert imports == ['deepagents.middleware.patch_tool_calls']
    assert_limited_result(result, request, 'deepagents_unavailable', status='failed')
    assert callbacks == [result]


@pytest.mark.parametrize('incompatible', [False, True])
async def test_default_runner_uses_only_readonly_deepagents_components(monkeypatch, incompatible):
    captured = {}
    imported = []
    model = object()
    request = make_request()

    class FakeMiddleware:
        pass

    class FakeStrategy:
        def __init__(self, *, schema, handle_errors):
            assert schema is SubtaskResult
            assert handle_errors is False

    class FakeStructuredTool:
        @staticmethod
        def from_function(*, coroutine, name, description):
            return SimpleNamespace(coroutine=coroutine, name=name, description=description)

    class FakeAgent:
        async def ainvoke(self, value):
            captured['input'] = value
            tools = {tool.name: tool.coroutine for tool in captured['factory']['tools']}
            assert await tools['Read'](path='src/app.py') == 'memory source'
            return {'structured_response': make_result(), 'messages': []}

    def create_agent(**options):
        captured['factory'] = options
        if incompatible:
            raise TypeError('fake incompatible API')
        return FakeAgent()

    modules = {
        'deepagents.middleware.patch_tool_calls': SimpleNamespace(PatchToolCallsMiddleware=FakeMiddleware),
        'langchain.agents': SimpleNamespace(create_agent=create_agent),
        'langchain.agents.structured_output': SimpleNamespace(ToolStrategy=FakeStrategy),
        'langchain_core.tools': SimpleNamespace(StructuredTool=FakeStructuredTool),
    }

    def import_module(name):
        imported.append(name)
        return modules[name]

    monkeypatch.setattr(importlib, 'import_module', import_module)
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={'Read': lambda *, path: 'memory source'}, model=model,
    ).run(request)

    assert imported == list(modules)
    options = captured['factory']
    assert set(options) == {'model', 'tools', 'system_prompt', 'middleware', 'response_format',
                            'checkpointer', 'store', 'interrupt_before', 'interrupt_after'}
    assert options['model'] is model
    assert [tool.name for tool in options['tools']] == ['Read']
    assert len(options['middleware']) == 1
    assert isinstance(options['middleware'][0], FakeMiddleware)
    assert all(options[key] is None for key in ('checkpointer', 'store', 'interrupt_before', 'interrupt_after'))
    if incompatible:
        assert_limited_result(result, request, 'deepagents_unavailable', status='failed')
        assert 'input' not in captured
    else:
        assert result.status == 'completed'
        assert set(captured['input']) == {'messages'}
        assert request.source_manifest in captured['input']['messages'][0]['content']


async def test_constructed_dto_cannot_bypass_output_validation():
    request = make_request()
    invalid = SubtaskResult.model_construct(**make_result(status='invalid-status', hypotheses=[]))
    result = await DeepAgentsReadonlyAdapter(
        readonly_tools={}, runner=lambda **kwargs: invalid,
    ).run(request)
    assert_limited_result(result, request, 'deepagents_invalid_result')
