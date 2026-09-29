"""Receipt views reject changed effects before any subject lookup."""

from dataclasses import replace

import httpx
import pytest
from read_only_evidence import readonly_evidence
from test_http_effect_order import runtime_check_only

from msg.transports.http import create_app
from msg.transports.http_routes import RouteEffect


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('listing', [False, True])
async def test_receipt_effect_gate_precedes_subject_lookup(installed, monkeypatch, effect, method, listing):
    app, _ = installed
    operation = 'communication.receipt_' + ('list' if listing else 'get')
    suffix = '/receipts' if listing else '/receipts/request0'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                key = (operation, 1)
                patch.setitem(app.registry._operations, key, replace(app.registry._operations[key], effect=effect))
                async with runtime_check_only(app, patch) as runtime:
                    for handle in ('root', 'missing-subject'):
                        response = await http.request(method, '/@' + handle + suffix)
                        assert response.status_code == 405, (method, handle, response.text)
                        if method == 'GET':
                            assert response.json()['error']['code'] == 'effect_mismatch'
                        else:
                            assert response.content == b''
                    assert runtime.await_count == 2


def test_route_effect_keeps_wire_and_diagnostic_spelling():
    for effect in RouteEffect:
        assert effect == effect.value
        assert str(effect) == 'RouteEffect.' + effect.name
        assert f'{effect}' == 'RouteEffect.' + effect.name
