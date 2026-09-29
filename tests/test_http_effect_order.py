"""Ordinary read views reject changed effects before resolving any resource."""

from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from read_only_evidence import readonly_evidence

from msg.security.quarantine import RUNTIME_GENERATION
from msg.transports.http import create_app


@asynccontextmanager
async def runtime_check_only(app, patch):
    """Keep the real database-backed freshness guard; forbid business queries.

    Blocking every transaction also blocks the outer HTTP recovery guard. This
    narrow observer instead delegates only that guard's generation read to the
    actual session. Resource lookups, other settings, SQL and writes still fail.
    """
    transaction = app.metadata.transaction
    settings_read = []
    transactions = []
    attempts = []

    class RuntimeSession:
        def __init__(self, session):
            self._session = session

        def setting(self, key, default=None):
            settings_read.append(key)
            assert key == RUNTIME_GENERATION, 'effect gate read a business setting'
            return self._session.setting(key, default)

        def __getattr__(self, name):
            attempts.append(name)
            raise AssertionError('effect gate attempted a business query: ' + name)

    @asynccontextmanager
    async def guarded_transaction(*, write):
        transactions.append(write)
        assert write is False, 'effect gate opened a write transaction'
        async with transaction(write=False) as session:
            yield RuntimeSession(session)

    runtime = AsyncMock(wraps=app.executor.require_current_runtime)
    execute = AsyncMock(side_effect=AssertionError('effect gate executed an operation'))
    patch.setattr(app.metadata, 'transaction', guarded_transaction)
    patch.setattr(app.executor, 'require_current_runtime', runtime)
    patch.setattr(app.executor, 'execute', execute)
    yield runtime
    assert transactions == [False] * runtime.await_count
    assert settings_read == [RUNTIME_GENERATION] * runtime.await_count
    assert attempts == [], 'effect gate attempted a business query'
    execute.assert_not_called()


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_non_read_views_do_not_resolve_or_execute(installed, monkeypatch, effect, method):
    app, _ = installed
    paths = (
        '/main',
        '/missing-resource',
        '/main/raw',
        '/main/json',
        '/main/meta',
        '/main/history',
        '/_id/missing/raw',
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                for operation in ('discovery.get', 'discovery.raw'):
                    key = (operation, 1)
                    patch.setitem(
                        app.registry._operations,
                        key,
                        replace(app.registry._operations[key], effect=effect),
                    )
                async with runtime_check_only(app, patch) as runtime:
                    for path in paths:
                        response = await http.request(method, path)
                        assert response.status_code == 405, (method, path, response.text)
                        if method == 'GET':
                            assert response.json()['error']['code'] == 'effect_mismatch'
                        else:
                            assert response.content == b''
                    assert runtime.await_count == len(paths)


@pytest.mark.parametrize('effect', ['transaction', 'external'])
async def test_stale_runtime_is_checked_before_the_public_effect_gate(
    installed, monkeypatch, effect
):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(RUNTIME_GENERATION, 'effect-order-new-generation')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                for operation in ('discovery.get', 'discovery.raw'):
                    key = (operation, 1)
                    patch.setitem(
                        app.registry._operations,
                        key,
                        replace(app.registry._operations[key], effect=effect),
                    )
                operation_lookup = Mock(
                    side_effect=AssertionError('stale runtime reached the route effect gate')
                )
                patch.setattr(app.registry, 'operation', operation_lookup)
                async with runtime_check_only(app, patch) as runtime:
                    for path in ('/main', '/_id/missing/raw'):
                        response = await http.get(path)
                        assert response.status_code == 503
                        assert response.json()['error']['code'] == 'recovery_runtime_stale'
                    assert runtime.await_count == 2
                operation_lookup.assert_not_called()
