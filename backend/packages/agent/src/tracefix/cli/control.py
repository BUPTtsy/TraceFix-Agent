import asyncio
import sqlite3


async def watch_stop_requests(session, run_id, process_control_id, *, interval=0.3):
    while True:
        try:
            record = await asyncio.to_thread(session.documents.run, run_id)
        except (OSError, sqlite3.Error) as error:
            session.render.text(f'读取停止请求失败：{error}')
        else:
            if (record.get('processControlId') == process_control_id and record.get('stopRequestedAt')
                    and session.engine and session.run_id):
                await session.stop()
                return
        await asyncio.sleep(interval)


async def close_watcher(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
