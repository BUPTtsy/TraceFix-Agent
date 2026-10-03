import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

from tracefix.cli.main import Session
from tracefix.execution.workspace import Workspace
from tracefix.runtime.contracts import digest
from tracefix.runtime.smoke import FakeBrowser, FakeRunner, make_engine


@pytest.fixture
def json_completion_transport(monkeypatch):
    monkeypatch.setenv('TRACEFIX_STREAM', 'false')


@pytest.fixture
def cli_session(tmp_path, monkeypatch):
    monkeypatch.delenv('TRACEFIX_CONSOLE_RUN_ID', raising=False)
    monkeypatch.delenv('TRACEFIX_DATABASE_URL', raising=False)
    registry = tmp_path / 'profiles'
    registry.mkdir()
    projects = []
    for name in ('alpha', 'beta'):
        target = tmp_path / name
        (target / '.git').mkdir(parents=True)
        projects.append({'id': name, 'root': str(target), 'repo_id': name + '-repo'})
        (registry / (name + '.yaml')).write_text(yaml.safe_dump({'project': name, 'source_commit': 'HEAD',
            'commands': {command: ['node', name] for command in ('start', 'reset', 'static', 'unit', 'build')}}), encoding='utf-8')
    (registry / 'projects.yaml').write_text(yaml.safe_dump({'projects': projects}), encoding='utf-8')
    args = SimpleNamespace(project='alpha', projects=str(registry / 'projects.yaml'), profile=None,
                           console_db=str(tmp_path / 'console.sqlite3'), data=str(tmp_path / 'data'),
                           goal='', mode='test', plain=True, url=None, spec=None, skills='skills', run=False, command=None)
    session = Session(args, None, None)
    output = io.StringIO()
    session.render.console = Console(file=output, no_color=True, markup=False, width=120)
    session.output = output
    return session


@pytest.fixture
def engine_session(cli_session, tmp_path, monkeypatch):
    session = cli_session
    engine, state = make_engine(tmp_path / 'engine', bugfree=True)
    registry = Path(session.args.projects)
    registry.write_text(yaml.safe_dump({'projects': [{'id': 'b', 'repo_id': 'ci',
        'root': str(engine.scopes.projects[state.scope_id].root)}]}), encoding='utf-8')
    profile = registry.parent / 'b.yaml'
    profile.write_text(yaml.safe_dump(engine.profile.model_dump(mode='json')), encoding='utf-8')
    session.select_project('b')
    session.args.data = str(tmp_path / 'engine')
    destination = Path(session.args.data) / 'workspaces' / state.run_id
    workspace, source = Workspace.export(session.scopes, session.ctx, engine.source['commit'], destination)
    engine.workspace, engine.source = workspace, source
    engine.browser.workspace = engine.model.workspace = workspace
    state.source_manifest = digest(source)
    state.repo_snapshot_ref = engine.artifacts.put(state.scope_id, state.run_id, source)
    engine.runner.source = state.source_manifest
    session.store, session.saver, session.artifacts = engine.store, engine.graph.checkpointer, engine.artifacts
    session.render.artifacts = session.artifacts
    session.begin_console_run(state)
    session.run_id, session.engine = state.run_id, engine
    engine.notify = session.notify
    monkeypatch.setenv('TRACEFIX_API_KEY', 'fake-ci')
    monkeypatch.setattr('tracefix.cli.main.DockerRunner', lambda *args: FakeRunner(state.source_manifest))
    monkeypatch.setattr(FakeRunner, 'browser_command', lambda self: [], raising=False)
    monkeypatch.setattr('tracefix.cli.main.MCPBrowser', lambda *args: FakeBrowser(engine.workspace, bugfree=True))
    monkeypatch.setattr('tracefix.cli.main.Gateway', lambda *args, **kwargs: engine.model)
    monkeypatch.setattr('tracefix.cli.main.BrowserPolicyRouter', lambda model, student: model)
    return session, engine, state
