from types import SimpleNamespace

import pytest

from tracefix.model.gateway import ModelResult
from tracefix.runtime.contracts import BrowserAction, Decision, RunStatus
from tracefix.runtime.smoke import PNG, make_engine
from tracefix.tools.handlers import build_runtime_tools
from tracefix.workers.contracts import WorkerTask
from tracefix.workers.runtime import IsolatedGuiScout


class RecordingModel:
    supports_tool_executor = False
    supports_context_assembler = False
    vision_model = ''

    def __init__(self, name):
        self.name = name
        self.calls = []

    async def generate(self, schema, context, *, on_attempt=None, on_response=None,
                       on_usage=None, **kwargs):
        self.calls.append((schema, context, kwargs))
        exchange = on_attempt(self.name, {'json': {'messages': []}}, 1) if on_attempt else None
        usage = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}
        if on_usage:
            on_usage(usage)
        if on_response:
            on_response(exchange, {'http_status': 200, 'body': {'choices': []}})
        return ModelResult(Decision(action=BrowserAction(kind='finish'), summary=self.name),
                           usage, self.name, 'stop')


@pytest.mark.asyncio
async def test_model_call_routes_worker_depth_without_mutating_supervisor(tmp_path):
    engine, state = make_engine(tmp_path)
    engine.store.save(state)
    supervisor = RecordingModel('supervisor')
    worker = RecordingModel('worker')
    engine.model = supervisor
    engine.worker_model = worker

    await engine.model_call(state, Decision, {'worker_depth': 1})
    await engine.model_call(state, Decision, {})

    assert [model.name for model in (supervisor, worker) if model.calls] == ['supervisor', 'worker']
    assert len(worker.calls) == 1
    assert len(supervisor.calls) == 1
    assert engine.model is supervisor


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed_tools,shell_mode,expected_local_tools', [
    (['browser'], 'disabled', set()),
    (['browser', 'Read'], 'disabled', {'Read'}),
    (['browser', 'shell.readonly'], 'readonly', {'Bash'}),
    ([], 'disabled', set()),
])
async def test_gui_scout_constructs_child_engine_with_worker_model(
        tmp_path, monkeypatch, allowed_tools, shell_mode, expected_local_tools):
    parent_engine, parent = make_engine(tmp_path)
    worker_model = RecordingModel('worker')
    parent_engine.worker_model = worker_model
    parent.test_spec_ref = None
    parent.agent_instructions_ref = None
    parent_engine.store.save(parent)

    class Sandbox:
        started = True
        runner = SimpleNamespace(run_id='child-run', actual_digest='child-environment')
        browser = SimpleNamespace(action=lambda self, action: None)

        async def close(self):
            return None

    async def browser_action(action):
        return {'id': 'observation', 'url': action.value, 'snapshot': 'snapshot',
                'console': 'console', 'network': 'network', 'png': PNG}

    sandbox = Sandbox()
    sandbox.browser.action = browser_action

    class Factory:
        def create(self, **kwargs):
            return sandbox

    captured = []
    captured_tools = []

    class ChildEngine:
        def __init__(self, *args, **kwargs):
            captured.append(args[8])
            self.store = parent_engine.store
            self.artifacts = parent_engine.artifacts
            self.source = parent_engine.source
            self.workspace = parent_engine.workspace
            self.profile = parent_engine.profile
            self.memory = None
            self.rule_resolver = None

        async def capture(self, child, raw):
            return 'observation.json'

        async def run(self, child):
            assert self.worker_model is worker_model
            assert self.worker_allowed_tools == allowed_tools
            assert self.worker_shell_mode == shell_mode
            runtime = build_runtime_tools(self, child, Decision)
            captured_tools.append({spec.name for spec in runtime.registry.specs})
            child.run_status = RunStatus.COMPLETED
            self.store.save(child)

    monkeypatch.setattr('tracefix.runtime.engine.Engine', ChildEngine)
    task = WorkerTask(run_id=parent.run_id, phase='DISCOVER', role='gui-scout',
                      goal='收集当前页面的授权探索证据',
                      prompt='只执行授权浏览器探索并返回可核验的页面证据。',
                      tools=allowed_tools, shell_mode=shell_mode, source_revision=parent.revision,
                      metadata={'child_run_id': 'child-run', 'url': parent.url,
                                'source_manifest': parent.source_manifest})

    await IsolatedGuiScout(parent_engine, parent, factory=Factory())(task, sandbox)

    assert captured == [worker_model]
    assert captured_tools == [expected_local_tools]
    assert parent_engine.worker_model is worker_model
