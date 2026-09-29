"""Read effects reject resource lookup without suppressing recovery checks."""
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from msg.security.quarantine import RUNTIME_GENERATION
from msg.transports.http import create_app
from read_only_evidence import readonly_evidence


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('stale', [False, True])
async def test_non_read_views_do_not_resolve_or_execute(installed, monkeypatch, effect, method, stale):
    app, _ = installed
    paths = ('/main', '/missing-resource', '/main/raw', '/main/json',
             '/main/meta', '/main/history', '/_id/missing/raw')
    if stale:
        async with app.metadata.transaction(write=True) as tx:
            tx.set_setting(RUNTIME_GENERATION, 'effect-order-new-generation')
    transaction = app.metadata.transaction
    queries = []

    @asynccontextmanager
    async def only_runtime_transaction(*, write):
        assert write is False, 'effect gate attempted a write transaction'
        async with transaction(write=False) as tx:
            execute_query = tx.execute

            def only_runtime_query(sql, parameters=(), *, write=False):
                observed = (sql, tuple(parameters), write)
                queries.append(observed)
                assert observed == (
                    'SELECT value FROM settings WHERE key=?', (RUNTIME_GENERATION,), False
                ), 'effect gate performed a resource or business query'
                return execute_query(sql, parameters, write=write)

            with monkeypatch.context() as session_patch:
                session_patch.setattr(tx, 'execute', only_runtime_query)
                yield tx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                for operation in ('discovery.get', 'discovery.raw'):
                    key = (operation, 1)
                    patch.setitem(app.registry._operations, key,
                                  replace(app.registry._operations[key], effect=effect))
                execute = AsyncMock(side_effect=AssertionError('effect gate executed an operation'))
                patch.setattr(app.metadata, 'transaction', only_runtime_transaction)
                patch.setattr(app.executor, 'execute', execute)
                for path in paths:
                    response = await http.request(method, path)
                    assert response.status_code == (503 if stale else 405), (method, path, response.text)
                    if method == 'GET':
                        assert response.json()['error']['code'] == (
                            'recovery_runtime_stale' if stale else 'effect_mismatch')
                    else:
                        assert response.content == b''
                assert len(queries) == len(paths), 'every request must check the real runtime generation'
                execute.assert_not_called()
