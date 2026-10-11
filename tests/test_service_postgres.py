import os
from dataclasses import replace
from uuid import uuid4

import pytest

from tracefix.cli.main import Session
from tracefix.service.run import execute_request
from tracefix.storage.checkpoints import postgres_checkpointer
from tracefix.storage.store import PostgresStore
from tests.test_service_runtime import service_fixture


async def test_service_with_real_isolated_postgres_and_langgraph_checkpoint(service_fixture):
    dsn = os.environ.get('TRACEFIX_BETA_STORAGE_DSN')
    if not dsn:
        pytest.skip('未提供独立 PostgreSQL 验收 schema DSN')
    fixture = service_fixture
    store = PostgresStore(dsn)
    try:
        schema = store.conn.execute('SELECT current_schema() AS schema').fetchone()['schema']
        assert schema.startswith('beta_runtime_mcp_'), '仅允许本任务独立测试 schema'
    finally:
        store.close()

    class RealStorageSession(fixture.factory):
        async def ensure_runtime(self):
            await Session.ensure_runtime(self)

    request = fixture.request('storage_' + uuid4().hex)
    result = await execute_request(request, replace(fixture.settings, database_url=dsn),
                                   session_factory=RealStorageSession)
    assert result['run_status'] == 'COMPLETED' and result['report_ref'], result
    store = PostgresStore(dsn)
    try:
        state = store.load(request.run_id, request.repo_id)
        assert state.report_ref == result['report_ref']
        assert store.trace(state.run_id, state.scope_id)[-1]['type'] == 'run.finished'
    finally:
        store.close()
    async with postgres_checkpointer(dsn) as saver:
        checkpoint = await saver.aget_tuple({'configurable': {'thread_id': request.run_id}})
        assert checkpoint is not None
    assert fixture.runners[-1].closed and fixture.browsers[-1].closed
