"""Published pickup/payment URLs remain private read-only lifecycle projections."""

import httpx
import pytest
from test_market_delivery import Sender, drain, enable_mail, verified_address
from test_market_lifecycle import buy, market
from test_order_paths import _headers
from test_orders import _intent
from test_service import call, register

from msg.core.codec import loads, unb64, wire
from msg.transports.http import create_app


async def snapshot(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            table: tx.rows(f'SELECT * FROM {table} ORDER BY 1')
            for table in ('store_orders', 'store_deliveries', 'money_ledger', 'events', 'audit')
        }


@pytest.mark.asyncio
async def test_emailed_pickup_url_reads_new_delivery_without_claiming(installed):
    app, root = installed
    enable_mail(app)
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    ok, outsider, _ = await register(app, 'order-outsider')
    await verified_address(app, bk, buyer, 'buyer@example.invalid')
    bought = await buy(app, bk, buyer, listing, email='buyer@example.invalid')
    assert bought.status == 'ok', wire(bought)
    oid = bought.data['order']['id']
    sender = Sender()
    await drain(app, sender)
    assert len(sender.messages) == 1
    message = loads(sender.messages[0]['text'])
    assert message['order_id'] == oid
    before = await snapshot(app)
    args = {'order_id': oid}
    headers = _headers(app, 'delivery.get', args, bk, buyer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get(message['pickup'], headers=headers)
        assert response.status_code == 200, response.text
        delivery = response.json()['delivery']
        assert delivery['order_id'] == oid and delivery['state'] == 'prepared'
        assert delivery['claimed_at'] is None
        assert unb64(delivery['payloads'][0]['data']) == b'delivery-ok\n'
        alias = f'/@order-buyer/orders/{oid}/_delivery'
        assert (await http.get(alias, headers=headers)).json() == response.json()
        head = await http.head(message['pickup'], headers=headers)
        assert head.status_code == 200 and head.headers['etag'] == response.headers['etag']
        for key, subject in ((None, None), (sk, seller), (ok, outsider)):
            auth = {} if key is None else _headers(app, 'delivery.get', args, key, subject)
            denied = await http.get(message['pickup'], headers=auth)
            missing_id = 'ord_' + 'z' * 32
            missing_auth = (
                {}
                if key is None
                else _headers(app, 'delivery.get', {'order_id': missing_id}, key, subject)
            )
            missing = await http.get(f'/_orders/{missing_id}/_delivery', headers=missing_auth)
            assert denied.status_code in {403, 404} and denied.status_code == missing.status_code
    assert await snapshot(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_unfunded_payment_url_has_no_fabricated_receipt_or_payment(installed, cancelled):
    app, root = installed
    sk, seller, bk, buyer, listing, _ = await market(app, root)
    created = await call(app, 'orders.create', _intent(listing), key=bk, subject=buyer)
    assert created.status == 'ok', wire(created)
    oid = created.data['order']['id']
    if cancelled:
        result = await call(
            app, 'orders.cancel', {'order_id': oid}, key=bk, subject=buyer, contract_version=2
        )
        assert result.status == 'ok', wire(result)
    before = await snapshot(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for key, subject in ((bk, buyer), (sk, seller)):
            response = await http.get(
                f'/_orders/{oid}/_payment',
                headers=_headers(app, 'orders.payment', {'order_id': oid}, key, subject),
            )
            assert response.status_code == 200, response.text
            payment = response.json()['payment']
            assert payment['status'] == 'pending'
            assert payment['amount_minor'] == 5_000_000
            assert payment['currency_id'] == 'primary'
            if subject == buyer:
                assert payment['receipt'] is None and payment['receipts'] == []
                assert payment['escrow_balance_minor'] == 0
            else:
                assert 'receipts' not in payment and 'escrow_balance_minor' not in payment
    assert await snapshot(app) == before
