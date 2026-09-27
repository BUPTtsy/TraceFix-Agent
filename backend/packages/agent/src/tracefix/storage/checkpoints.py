"""Windows keeps Proactor for MCP; synchronous psycopg runs in I/O threads."""
import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from tracefix.execution.platforms import is_windows


class ThreadedPostgresSaver(PostgresSaver):
    # Reuse the official schema, serializer, SQL and lock. This is an I/O adapter,
    # not a second scheduler/database, and creates no extra asyncio event loop.
    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        rows = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for row in rows:
            yield row

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        await asyncio.to_thread(self.delete_thread, thread_id)


@asynccontextmanager
async def postgres_checkpointer(dsn):
    if is_windows():
        with ThreadedPostgresSaver.from_conn_string(dsn) as saver:
            await asyncio.to_thread(saver.setup)
            yield saver
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            yield saver
