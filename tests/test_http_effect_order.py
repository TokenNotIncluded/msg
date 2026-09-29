"""Ordinary read views reject changed effects before resolving any resource."""
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from msg.transports.http import create_app
from msg.storage.session import RelationalSession
from read_only_evidence import readonly_evidence


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_non_read_views_do_not_resolve_or_execute(installed, monkeypatch, effect, method):
    app, _ = installed
    paths = ('/main', '/missing-resource', '/main/raw', '/main/json',
             '/main/meta', '/main/history', '/_id/missing/raw')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                for operation in ('discovery.get', 'discovery.raw'):
                    key = (operation, 1)
                    patch.setitem(app.registry._operations, key,
                                  replace(app.registry._operations[key], effect=effect))
                lookup = AsyncMock(side_effect=AssertionError('effect gate performed a resource lookup'))
                execute = AsyncMock(side_effect=AssertionError('effect gate executed an operation'))
                # The runtime-generation fence must still read its setting before routing.
                # Block actual resource resolution, not that required safety transaction.
                for name in ('resolve', 'resolve_migrated', 'resource', 'path'):
                    patch.setattr(RelationalSession, name, lookup)
                patch.setattr(app.executor, 'execute', execute)
                for path in paths:
                    response = await http.request(method, path)
                    assert response.status_code == 405, (method, path, response.text)
                    if method == 'GET':
                        assert response.json()['error']['code'] == 'effect_mismatch'
                    else:
                        assert response.content == b''
                lookup.assert_not_called()
                execute.assert_not_called()
