"""Web 控制台旧运行接口的内部 JSON worker。"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
from contextlib import AsyncExitStack
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.messages import error_message
from tracefix.runtime.contracts import RunStatus
from tracefix.service.session import Session
from tracefix.storage.artifacts import sanitize


MAX_REQUEST_BYTES = 2_097_152


class WebRunRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    project: str = Field(min_length=1)
    goal: str = Field(default='', max_length=4000)
    mode: Literal['test', 'repair'] = 'test'
    projects: str | None = None
    profile: str | None = None
    data: str | None = None
    console_db: str | None = None
    spec: str | None = None
    continue_run: str | None = None
    instruction: str = Field(default='', max_length=4000)
    parent_run: str | None = None
    rule: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_execution(self):
        if self.continue_run:
            if not self.instruction.strip() or self.parent_run:
                raise ValueError('继续执行需要指令，且不能同时派生任务')
        elif not self.goal.strip():
            raise ValueError('测试目标不能为空')
        return self

    def arguments(self):
        values = self.model_dump()
        values.update(projects=self.projects or os.getenv('TRACEFIX_PROJECTS', 'profiles/projects.yaml'),
            data=self.data or os.getenv('TRACEFIX_DATA', '.tracefix'),
            console_db=self.console_db or os.getenv('TRACEFIX_CONSOLE_DB'),
            url=None, execution_mode='batch', skills='backend/skills')
        return SimpleNamespace(**values)


async def watch_stop_requests(session, run_id, process_control_id, *, interval=0.3):
    while True:
        try:
            record = await asyncio.to_thread(session.documents.run, run_id)
        except (OSError, sqlite3.Error):
            record = {}
        if (record.get('id') == run_id and record.get('projectId') == session.scope
                and record.get('processControlId') == process_control_id
                and record.get('stopRequestedAt') and session.engine and session.run_id):
            await session.stop()
            return
        await asyncio.sleep(interval)


async def close_task(task):
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def wait_for_run(session, *, interval=0.3):
    while session.task:
        task = session.task
        await task
        if session.task is not task:
            continue
        if not os.getenv('TRACEFIX_CONSOLE_RUN_ID') or session.state().run_status != RunStatus.PAUSED:
            break
        handled = {entry.id for entry in session.state().guidance}
        while session.state().run_status == RunStatus.PAUSED:
            if session.task is not task:
                break
            entries = session.engine.guidance_status(session.state())
            pending = [entry for entry in entries if entry.status == 'queued' and
                (entry.level == 'retarget' and entry.confirmed_at or
                 entry.level != 'retarget' and entry.id not in handled)]
            if pending:
                handled.update(entry.id for entry in pending)
                session.task = asyncio.create_task(session.drive(resume=(
                    'retarget' if any(entry.level == 'retarget' for entry in pending) else 'resume')))
                break
            await asyncio.sleep(interval)


async def application(request):
    async with AsyncExitStack() as resources:
        session = Session(request.arguments(), None, None)
        session.runtime_stack = resources
        process_control_id = os.getenv('TRACEFIX_PROCESS_CONTROL_ID')
        console_run_id = os.getenv('TRACEFIX_CONSOLE_RUN_ID')
        watcher = None
        if process_control_id and console_run_id:
            watcher = asyncio.create_task(watch_stop_requests(session, console_run_id, process_control_id))
        current = asyncio.current_task()
        def cancel(signum, frame):
            session.stop_requested = True
            current.cancel()
        previous = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
            resources.callback(signal.signal, signum, previous[signum])
        try:
            if request.continue_run:
                await session.continue_task(request.continue_run, request.instruction)
            else:
                await session.create()
            await wait_for_run(session)
        except asyncio.CancelledError:
            if session.engine and session.run_id:
                await session.stop()
            return 130
        else:
            state = session.state()
            print(json.dumps({'run_id': state.run_id, 'run_status': str(state.run_status),
                'outcome': str(state.outcome) if state.outcome else None, 'report_ref': state.report_ref},
                ensure_ascii=False), flush=True)
            return 0 if state.run_status in {RunStatus.COMPLETED, RunStatus.WAITING_APPROVAL} else 1
        finally:
            # 在 AsyncExitStack 关闭浏览器和数据库之前停止所有后台访问。
            if watcher is not None:
                await close_task(watcher)
            if session.task and not session.task.done():
                await close_task(session.task)



def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError('Web 运行请求过大')
        request = WebRunRequest.model_validate_json(raw)
        return asyncio.run(application(request))
    except (ValueError, RuntimeError, PermissionError, OSError) as error:
        print(json.dumps({'error': sanitize(error_message(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
