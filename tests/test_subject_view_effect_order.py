"""Every published subject read view fences effects before hosting or lookup."""

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from read_only_evidence import readonly_evidence
from test_http_effect_order import runtime_check_only

from msg.extensions import hosting
from msg.security.quarantine import RUNTIME_GENERATION
from msg.transports.http import create_app

# Independent public URL inventory, including archived permanent aliases.
ORDER = 'ord_' + 'a' * 32
VIEWS = (
    ('orders', 'orders.list'),
    ('orders/json', 'orders.list'),
    ('orders/' + ORDER, 'orders.get'),
    ('orders/' + ORDER + '/_payment/json', 'orders.payment'),
    ('orders/' + ORDER + '/_delivery', 'delivery.get'),
    ('bal', 'money.balance'),
    ('balance/json', 'money.balance'),
    ('ledger', 'money.ledger'),
    ('public-balance', 'money.public_balance'),
    ('public-ledger/json', 'money.public_ledger'),
    ('handoffs', 'communication.handoff_list'),
    ('handoffs/item0/json', 'communication.handoff_get'),
    ('leases', 'communication.lease_list'),
    ('leases/item0', 'communication.lease_get'),
    ('requests', 'communication.request_list'),
    ('requests/item0', 'communication.request_get'),
    ('offers', 'communication.offer_list'),
    ('offers/item0', 'communication.offer_get'),
    ('checkpoints', 'communication.checkpoint_list'),
    ('checkpoints/item0', 'communication.checkpoint_get'),
    ('proposals', 'communication.proposal_list'),
    ('proposals/item0', 'communication.proposal_get'),
    ('watches', 'communication.watch_list'),
    ('watches/item0', 'communication.watch_get'),
    ('pk', 'identity.identity_key_get'),
    ('pubkey', 'identity.identity_key_get'),
    ('k', 'identity.identity_key_list'),
    ('keys', 'identity.identity_key_list'),
    ('k/key0/json', 'identity.identity_key_get'),
    ('keys/key0', 'identity.identity_key_get'),
    ('ek', 'identity.encryption_key_get'),
    ('encryption-key', 'identity.encryption_key_get'),
    ('e', 'identity.encryption_key_list'),
    ('encryption-keys', 'identity.encryption_key_list'),
    ('e/key0', 'identity.encryption_key_get'),
    ('encryption-keys/key0/json', 'identity.encryption_key_get'),
    ('cert/cert0', 'cert.get'),
    ('certificates/cert0/json', 'cert.get'),
    ('ach', 'achievement.list'),
    ('achievements', 'achievement.list'),
    ('in', 'communication.inbox'),
    ('inbox', 'communication.inbox'),
    ('out', 'communication.outbox'),
    ('outbox', 'communication.outbox'),
    ('dm', 'communication.dm_list'),
    ('following', 'communication.following'),
    ('receipts', 'communication.receipt_list'),
    ('receipts/json', 'communication.receipt_list'),
    ('receipts/request0', 'communication.receipt_get'),
    ('receipts/request0/json', 'communication.receipt_get'),
    ('cert', 'discovery.get'),
    ('certificates/json', 'discovery.get'),
    ('cert/history', 'discovery.get'),
    ('ssh', 'discovery.get'),
    ('ssh-keys/json', 'discovery.get'),
    ('ssh/key0/json', 'discovery.get'),
    ('ssh-keys/key0', 'discovery.get'),
    ('ks', 'discovery.get'),
    ('keystore/json', 'discovery.get'),
    ('ks/item0/raw', 'discovery.raw'),
    ('keystore/item0/meta', 'discovery.get'),
    ('ordinary-resource/json', 'discovery.get'),
    ('ordinary-resource/raw', 'discovery.raw'),
)


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_subject_views_reject_effects_before_any_business_probe(
    installed, monkeypatch, effect, method
):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                for operation in {operation for _, operation in VIEWS}:
                    key = (operation, 1)
                    assert app.registry._operations[key].effect == 'read', operation
                    patch.setitem(
                        app.registry._operations,
                        key,
                        replace(app.registry._operations[key], effect=effect),
                    )
                probe = AsyncMock(side_effect=AssertionError('effect gate probed hosting'))
                patch.setattr(hosting, 'serve_hosted', probe)
                async with runtime_check_only(app, patch) as runtime:
                    for suffix, _ in VIEWS:
                        for handle in ('root', 'missing-subject'):
                            path = '/@' + handle + '/' + suffix
                            response = await http.request(method, path)
                            assert response.status_code == 405, (path, response.text)
                            if method == 'GET':
                                assert response.json()['error']['code'] == 'effect_mismatch'
                            else:
                                assert response.content == b''
                    assert runtime.await_count == len(VIEWS) * 2
                probe.assert_not_called()


@pytest.mark.parametrize('effect', ['transaction', 'external'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_organization_views_reject_effects_before_hosting(
    installed, monkeypatch, effect, method
):
    app, _ = installed
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
                probe = AsyncMock(side_effect=AssertionError('effect gate probed hosting'))
                patch.setattr(hosting, 'serve_hosted', probe)
                async with runtime_check_only(app, patch) as runtime:
                    for path in ('/&root/site', '/&missing/site/json', '/&missing/site/raw'):
                        response = await http.request(method, path)
                        assert response.status_code == 405, (path, response.text)
                        if method == 'GET':
                            assert response.json()['error']['code'] == 'effect_mismatch'
                        else:
                            assert response.content == b''
                    assert runtime.await_count == 3
                probe.assert_not_called()


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_stale_runtime_precedes_all_subject_view_metadata(installed, monkeypatch, method):
    app, _ = installed
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(RUNTIME_GENERATION, 'subject-views-new-generation')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            with monkeypatch.context() as patch:
                lookup = Mock(side_effect=AssertionError('stale runtime read route metadata'))
                probe = AsyncMock(side_effect=AssertionError('stale runtime probed hosting'))
                patch.setattr(app.registry, 'operation', lookup)
                patch.setattr(hosting, 'serve_hosted', probe)
                async with runtime_check_only(app, patch) as runtime:
                    for suffix, _ in VIEWS:
                        response = await http.request(method, '/@root/' + suffix)
                        assert response.status_code == 503, (suffix, response.text)
                        if method == 'GET':
                            assert response.json()['error']['code'] == 'recovery_runtime_stale'
                        else:
                            assert response.content == b''
                    assert runtime.await_count == len(VIEWS)
                lookup.assert_not_called()
                probe.assert_not_called()
