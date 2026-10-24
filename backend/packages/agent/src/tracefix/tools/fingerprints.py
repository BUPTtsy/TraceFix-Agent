"""工具动作及相对修复基线的累积候选摘要。"""
from __future__ import annotations

import shlex

from tracefix.execution.workspace import git
from tracefix.runtime.contracts import digest


def diff_fingerprint(workspace, state):
    """按工作区相对修复基线的实际内容计算候选身份。"""
    base = state.patch_base_commit or workspace.head()
    changed = git(workspace.root, 'diff', '--no-ext-diff', '--name-only', '-z', base, '--')
    paths = {path for path in changed.decode('utf-8').split('\0') if path}
    workspace.require_repository_snapshot()
    entries = []
    for relative in sorted(paths):
        path = workspace.path(relative)
        before = workspace.commit_bytes(base, relative)
        after = path.read_bytes()
        exists = bool(git(workspace.root, 'ls-tree', '-z', base, '--', ':(literal)' + relative))
        if before != after or not exists:
            entries.append([relative, digest(before), digest(after), 'modify' if exists else 'create'])
    return digest(entries)


def record_tool_action(engine, state, name, arguments, result):
    """仅由成功工具的分发点调用；回执重放不会再次执行该分支。"""
    values = arguments.model_dump(mode='json') if hasattr(arguments, 'model_dump') else dict(arguments)
    values.pop('description', None)
    values.pop('timeout', None)
    for key in ('file_path',):
        if key in values:
            values[key] = str(values[key]).replace('\\', '/').removeprefix(
                engine.workspace.root.as_posix().rstrip('/') + '/')
    if name == 'Bash':
        command = values.get('command', '')
        try:
            command = shlex.split(command)
        except ValueError:
            pass
        fingerprint = digest([name, command])
    else:
        fingerprint = digest([name, values, diff_fingerprint(engine.workspace, state)])
    updated = engine.changed(state,
        action_fingerprints=(state.action_fingerprints + [fingerprint])[-12:],
        loop_no_progress_steps=state.loop_no_progress_steps)
    for field in type(state).model_fields:
        setattr(state, field, getattr(updated, field))
    return fingerprint
