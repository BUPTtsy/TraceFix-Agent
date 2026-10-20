"""工具动作及相对修复基线的累积候选摘要。"""
from __future__ import annotations

import shlex

from tracefix.execution.workspace import git
from tracefix.runtime.contracts import digest


def diff_fingerprint(workspace, state):
    """比较内容而非 overlay 版本，物化前后保持同一候选的身份。"""
    base = state.patch_base_commit or workspace.head()
    changed = git(workspace.root, 'diff', '--no-ext-diff', '--name-only', '-z', base, '--')
    paths = {path for path in changed.decode('utf-8').split('\0') if path}
    snapshot = workspace.require_repository_snapshot()
    paths.update(relative for relative in snapshot['files']
                 if workspace.staged_content(relative) is not None)
    entries = []
    for relative in sorted(paths):
        path = workspace.path(relative)
        before = git(workspace.root, 'show', f'{base}:{relative}')
        staged = workspace.staged_content(relative)
        after = staged if staged is not None else path.read_bytes()
        if before != after:
            entries.append([relative, digest(before), digest(after)])
    return digest(entries)


def record_tool_action(engine, state, name, arguments, result):
    """仅由成功工具的分发点调用；回执重放不会再次执行该分支。"""
    values = arguments.model_dump(mode='json') if hasattr(arguments, 'model_dump') else dict(arguments)
    values.pop('description', None)
    values.pop('timeout', None)
    for key in ('file_path', 'notebook_path'):
        if key in values:
            values[key] = str(values[key]).replace('\\', '/').removeprefix(
                engine.workspace.root.as_posix().rstrip('/') + '/')
    if name in {'Edit', 'Write', 'NotebookEdit'}:
        value = values.get('file_path') or values['notebook_path']
        path = str(value).removeprefix('/workspace/')
        content = engine.workspace.staged_content(path)
        if content is None:
            content = engine.workspace.path(path).read_bytes()
        fingerprint = digest([name, path, digest(content)])
    elif name == 'Bash':
        command = values.get('command', '')
        try:
            command = shlex.split(command)
        except ValueError:
            pass
        fingerprint = digest([name, command])
    elif name == 'RunTests':
        fingerprint = digest([name, values.get('target', 'unit'), result['diff_fingerprint']])
    else:
        fingerprint = digest([name, values, diff_fingerprint(engine.workspace, state)])
    updated = engine.changed(state,
        action_fingerprints=(state.action_fingerprints + [fingerprint])[-12:],
        loop_no_progress_steps=state.loop_no_progress_steps)
    for field in type(state).model_fields:
        setattr(state, field, getattr(updated, field))
    return fingerprint
