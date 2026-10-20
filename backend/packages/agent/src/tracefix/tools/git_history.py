"""仅允许冻结提交及其祖先、授权文件的结构化 Git 历史读取。"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from tracefix.execution.repository import plain_root, run_git
from tracefix.runtime.contracts import Contract, Phase
from tracefix.tools.core import ToolRejected


class GitHistoryInput(Contract):
    action: Literal['log', 'show', 'blame'] = 'log'
    path: str = Field(min_length=1, max_length=1000)
    revision: str | None = None
    limit: int = Field(default=20, ge=1, le=50)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=200, ge=1)


def read_history(engine, state, arguments):
    engine.scopes.assert_current(engine.context)
    snapshot = engine.workspace.require_repository_snapshot()
    project = engine.scopes.projects[state.scope_id]
    root = plain_root(project.root)
    if (snapshot['scope_id'] != state.scope_id or snapshot['repo_id'] != project.repo_id
            or snapshot['source_root'] != str(root)):
        raise ToolRejected('Git 历史源仓库与冻结 Run 身份不一致')
    relative = arguments.path
    if ('\\' in relative or ':' in relative or relative.startswith('/')
            or any(part in {'', '.', '..'} for part in relative.split('/'))
            or any(ord(char) < 32 for char in relative)):
        raise ToolRejected('Git 历史只接受授权文件的规范相对路径')
    engine.workspace.path(relative)
    if relative not in snapshot['files']:
        raise ToolRejected('Git 历史文件不属于冻结可读源码快照')
    frozen = snapshot['commit']
    revision = arguments.revision or frozen
    if not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value)
               for value in (frozen, revision)):
        raise ToolRejected('Git 历史 revision 必须为完整 commit SHA；不接受任意 Git 参数')
    try:
        common = run_git(root, 'merge-base', revision, frozen).decode().strip()
        if common != revision:
            raise ToolRejected('Git 历史仅允许冻结提交及其祖先')
        top = Path(run_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        name = (PurePosixPath(root.relative_to(top).as_posix()) / relative).as_posix()
        literal = ':(literal)' + name
        tree = run_git(top, 'ls-tree', '-z', revision, '--', literal)
        if not tree or tree.split(b' ', 1)[0] not in {b'100644', b'100755'}:
            raise ToolRejected('该历史版本中的目标不是普通源码文件')
        if arguments.action == 'log':
            output = run_git(top, 'log', '--format=%H', '--max-count=' + str(arguments.limit),
                             revision, '--', literal)
        elif arguments.action == 'show':
            output = run_git(top, 'show', '--format=', '--no-renames', revision, '--', literal)
        else:
            if arguments.end_line < arguments.start_line or arguments.end_line - arguments.start_line > 999:
                raise ToolRejected('blame 行范围必须有序且最多 1000 行')
            output = run_git(top, 'blame', '--line-porcelain',
                '-L', f'{arguments.start_line},{arguments.end_line}', revision, '--', name)
    except (RuntimeError, OSError, ValueError) as error:
        if isinstance(error, ToolRejected):
            raise
        raise ToolRejected('冻结 Git 历史读取失败：' + str(error)) from error
    engine.scopes.assert_current(engine.context)
    return {'kind': 'frozen_git_history', 'action': arguments.action, 'path': relative,
            'revision': revision, 'frozen_revision': frozen,
            'output': output.decode('utf-8', errors='replace'), 'verification_evidence': False}


def register_git_history(engine, state, context, bind):
    if (context.get('worker_depth') == 1 or state.mode != 'repair'
            or state.repair_mode != 'hybrid' or state.phase != Phase.DIAGNOSE):
        return

    async def history(arguments, call_id):
        return await asyncio.to_thread(read_history, engine, state, arguments)

    bind('GitHistory', '只读源仓库冻结提交及祖先历史；指定授权相对文件和 log/show/blame。'
         'revision 可省略或为完整祖先 SHA；不接受分支名、任意参数、网络或写操作。',
         GitHistoryInput, history, phases={Phase.DIAGNOSE}, output_limit_tokens=6000)
