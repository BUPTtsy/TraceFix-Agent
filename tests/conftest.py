import io
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

from tracefix.cli.main import Session


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
