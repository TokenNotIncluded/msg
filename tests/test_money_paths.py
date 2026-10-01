"""Public money projections and private account paths never execute writes."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.http import create_app


def _headers(app, operation, args, key, subject):
    packet = request_for(
        operation,
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    return {'X-Msg-Request': b64(canonical(packet))}


@pytest.mark.asyncio
async def test_money_paths_are_read_only_and_balance_is_subject_private(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'money-path-alice')
    bob_key, bob, _ = await register(app, 'money-path-bob')
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'money_accounts', 'events', 'audit')
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for path in ('/_money', '/_money/banks', '/_money/offers'):
            response = await http.get(path)
            assert response.status_code == 200, response.text
            head = await http.head(path)
            assert head.status_code == 200 and head.headers['etag'] == response.headers['etag']
            assert (await http.post(path, json={})).status_code in {404, 405}
        balance_headers = _headers(app, 'money.balance', {}, alice_key, alice)
        short = await http.get('/@money-path-alice/bal', headers=balance_headers)
        long = await http.get('/@money-path-alice/balance', headers=balance_headers)
        assert short.status_code == long.status_code == 200
        assert short.json() == long.json()
        assert short.json()['balance_minor'] == 0
        assert (await http.get('/@money-path-bob/bal', headers=balance_headers)).status_code == 403
        ledger = await http.get(
            '/@money-path-alice/ledger', headers=_headers(app, 'money.ledger', {}, alice_key, alice)
        )
        assert ledger.status_code == 200 and ledger.json()['items'] == []
        assert (await http.post('/@money-path-alice/ledger', json={})).status_code in {404, 405}
        assert (
            await http.get(
                '/@money-path-alice/bal', headers=_headers(app, 'money.balance', {}, bob_key, bob)
            )
        ).status_code == 403
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('money_ledger', 'money_accounts', 'events', 'audit')
        )
    assert after == before


@pytest.mark.asyncio
async def test_wallet_html_and_raw_keep_private_subject_authorization(installed):
    app, _ = installed
    key, user, _ = await register(app, 'wallet-view-user')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        headers = {**_headers(app, 'money.balance', {}, key, user), 'Accept': 'text/html'}
        balance = await http.get('/@wallet-view-user/bal', headers=headers)
        assert balance.status_code == 200, balance.text
        assert 'id="msg-transfer-compose"' in balance.text
        assert 'data-server="' + app.settings.service_url + '"' in balance.text
        assert 'money transfer' in balance.text
        denied = await http.get('/@wallet-view-user/bal', headers={'Accept': 'text/html'})
        assert denied.status_code in {400, 403} and 'msg-transfer-compose' not in denied.text
        raw = await http.get('/@wallet-view-user/bal?format=raw', headers=headers)
        assert raw.status_code == 200 and raw.headers['content-type'].startswith('text/plain')
        assert '<form' not in raw.text and 'msg money transfer' in raw.text
        ledger = await http.get(
            '/@wallet-view-user/ledger',
            headers={**_headers(app, 'money.ledger', {}, key, user), 'Accept': 'text/html'},
        )
        assert ledger.status_code == 200 and 'Transactions' in ledger.text
