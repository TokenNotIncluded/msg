"""Order and delivery paths are private read projections, not payment endpoints."""

from datetime import timedelta

import httpx
import pytest
from test_orders import _intent, _sale
from test_service import NOW, call, register

from msg.admin.money import apply_money
from msg.core.codec import b64, canonical, wire
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
async def test_order_and_delivery_paths_recheck_buyer_and_never_write(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'order-buyer')
    outsider_key, outsider, _ = await register(app, 'order-outsider')
    listing, _ = await _sale(app, seller_key, seller)
    await apply_money(app, root, action='mint', operator='test-console', amount_minor=5_000_000)
    await apply_money(
        app,
        root,
        action='transfer',
        operator='test-console',
        subject_id=buyer,
        amount_minor=5_000_000,
    )
    bought = await call(app, 'orders.buy', _intent(listing), key=buyer_key, subject=buyer)
    assert bought.status == 'ok', wire(bought)
    order_id = bought.data['order']['id']
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order_id}, key=buyer_key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('store_orders', 'store_deliveries', 'money_ledger', 'events', 'audit')
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        list_path = '/@order-buyer/orders/'
        listed = await http.get(
            list_path, headers=_headers(app, 'orders.list', {}, buyer_key, buyer)
        )
        assert listed.status_code == 200 and listed.json()['orders'][0]['id'] == order_id
        detail_path = f'/@order-buyer/orders/{order_id}'
        detail_headers = _headers(app, 'orders.get', {'order_id': order_id}, buyer_key, buyer)
        detail = await http.get(detail_path, headers=detail_headers)
        assert detail.status_code == 200 and detail.json()['order']['id'] == order_id
        canonical_detail = await http.get(f'/_orders/{order_id}', headers=detail_headers)
        assert canonical_detail.status_code == 200 and canonical_detail.json() == detail.json()
        payment_path = detail_path + '/_payment'
        payment = await http.get(
            payment_path,
            headers=_headers(app, 'orders.payment', {'order_id': order_id}, buyer_key, buyer),
        )
        assert payment.status_code == 200 and payment.json()['payment']['status'] == 'funded'
        delivery_path = detail_path + '/_delivery'
        delivery_headers = _headers(app, 'delivery.get', {'order_id': order_id}, buyer_key, buyer)
        delivery = await http.get(delivery_path, headers=delivery_headers)
        assert delivery.status_code == 200, delivery.text
        assert delivery.json()['delivery']['order_id'] == order_id
        assert delivery.json()['delivery']['payloads']
        canonical_delivery = await http.get(
            f'/_orders/{order_id}/_delivery', headers=delivery_headers
        )
        assert canonical_delivery.status_code == 200
        assert canonical_delivery.json() == delivery.json()
        assert (await http.head(delivery_path, headers=delivery_headers)).headers[
            'etag'
        ] == delivery.headers['etag']
        assert (await http.post(delivery_path, json={})).status_code in {404, 405}
        stranger = await http.get(
            f'/@order-outsider/orders/{order_id}',
            headers=_headers(app, 'orders.get', {'order_id': order_id}, outsider_key, outsider),
        )
        absent = await http.get(
            '/@order-outsider/orders/ord_' + 'a' * 32,
            headers=_headers(
                app, 'orders.get', {'order_id': 'ord_' + 'a' * 32}, outsider_key, outsider
            ),
        )
        assert stranger.status_code == absent.status_code
        cross = await http.get(
            delivery_path,
            headers=_headers(app, 'delivery.get', {'order_id': order_id}, seller_key, seller),
        )
        assert cross.status_code in {403, 404}
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('store_orders', 'store_deliveries', 'money_ledger', 'events', 'audit')
        )
    assert after == before
