"""Managed package delivery and escrow release use the real signed executor."""

import asyncio

import pytest
from test_orders import _intent, _sale
from test_service import call, register

from msg.admin.money import apply_money
from msg.core.codec import b64, canonical, digest, wire
from msg.security.crypto import Ed25519Signer


async def _funded(installed):
    app, root = installed
    seller_key, seller, _ = await register(app, 'order-seller')
    buyer_key, buyer, _ = await register(app, 'delivery-buyer')
    listing, package = await _sale(app, seller_key, seller)
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
    return app, (seller_key, seller), (buyer_key, buyer), bought.data['order'], package


@pytest.mark.asyncio
async def test_buyer_gets_fixed_package_and_signed_accept_releases_escrow_once(installed):
    app, seller_identity, buyer_identity, order, package = await _funded(installed)
    seller_key, seller = seller_identity
    buyer_key, buyer = buyer_identity
    order_id = order['id']
    absent = await call(app, 'delivery.get', {'order_id': order_id}, key=buyer_key, subject=buyer)
    assert absent.error.code == 'delivery_not_found'
    prepared = await call(
        app,
        'delivery.prepare',
        {'order_id': order_id},
        key=buyer_key,
        subject=buyer,
        rid='prepare-once',
    )
    assert prepared.status == 'ok', wire(prepared)
    repeated = await call(
        app,
        'delivery.prepare',
        {'order_id': order_id},
        key=buyer_key,
        subject=buyer,
        rid='prepare-once',
    )
    assert repeated.status == 'ok' and repeated.replayed
    body = (
        await call(app, 'delivery.get', {'order_id': order_id}, key=buyer_key, subject=buyer)
    ).data['delivery']
    assert body['package_digest'] == package['digest']
    assert body['delivery_digest'] == prepared.data['delivery']['delivery_digest']
    assert body['manifest']['text'] == 'msg.lmm.best store selftest'
    assert len(body['payloads']) == 1
    assert body['payloads'][0]['data'] == b64(b'delivery-ok\n')
    assert digest(b'delivery-ok\n') == body['payloads'][0]['digest']
    assert (
        await call(
            app,
            'delivery.accept',
            {'order_id': order_id, 'delivery_digest': 'sha256:' + '0' * 64},
            key=buyer_key,
            subject=buyer,
        )
    ).error.code == 'delivery_mismatch'
    assert (
        await call(
            app,
            'delivery.accept',
            {'order_id': order_id, 'delivery_digest': body['delivery_digest']},
            key=seller_key,
            subject=seller,
        )
    ).error.code == 'order_not_found'
    args = {'order_id': order_id, 'delivery_digest': body['delivery_digest']}
    settled = await call(
        app, 'delivery.accept', args, key=buyer_key, subject=buyer, rid='accept-once'
    )
    assert settled.status == 'ok', wire(settled)
    assert settled.data['state'] == 'settled'
    replay = await call(
        app, 'delivery.accept', args, key=buyer_key, subject=buyer, rid='accept-once'
    )
    assert replay.status == 'ok' and replay.replayed
    duplicate = await call(
        app, 'delivery.accept', args, key=buyer_key, subject=buyer, rid='accept-twice'
    )
    assert duplicate.error.code == 'delivery_not_acceptable'
    assert (
        await call(app, 'orders.cancel', {'order_id': order_id}, key=buyer_key, subject=buyer)
    ).error.code == 'order_not_cancellable'
    assert (await call(app, 'money.balance', {}, key=seller_key, subject=seller)).data[
        'balance_minor'
    ] == 5_000_000
    assert (await call(app, 'money.balance', {}, key=buyer_key, subject=buyer)).data[
        'balance_minor'
    ] == 0
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?',
                ('order_release:' + order_id,),
            )[0]
            == 1
        )
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'settled'
        assert (
            tx.one('SELECT state FROM store_deliveries WHERE order_id=?', (order_id,))[0]
            == 'claimed'
        )


@pytest.mark.asyncio
async def test_private_delivery_and_wrong_recipient_are_fail_closed(installed):
    app, seller_identity, buyer_identity, order, _ = await _funded(installed)
    seller_key, seller = seller_identity
    buyer_key, buyer = buyer_identity
    outsider_key, outsider, _ = await register(app, 'delivery-outsider')
    order_id = order['id']
    for key, subject in ((seller_key, seller), (outsider_key, outsider), (None, None)):
        for op in ('delivery.get', 'delivery.prepare'):
            hidden = await call(app, op, {'order_id': order_id}, key=key, subject=subject)
            missing = await call(app, op, {'order_id': 'ord_' + 'a' * 32}, key=key, subject=subject)
            if key is not None or op == 'delivery.get':
                assert hidden.error.code == missing.error.code == 'order_not_found'
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE store_orders SET delivery_target=? WHERE id=?',
            (canonical({'subject_id': outsider, 'channel': 'site'}).decode(), order_id),
            write=True,
        )
    denied = await call(
        app, 'delivery.prepare', {'order_id': order_id}, key=buyer_key, subject=buyer
    )
    assert denied.error.code == 'delivery_recipient_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order_id,))[0] == 'funded'
        assert tx.one('SELECT COUNT(*) FROM store_deliveries')[0] == 0


@pytest.mark.asyncio
async def test_package_tamper_cannot_mark_delivered_or_release(installed):
    app, _, buyer_identity, order, _ = await _funded(installed)
    buyer_key, buyer = buyer_identity
    async with app.metadata.transaction(write=True) as tx:
        package_id = tx.one('SELECT package_id FROM store_orders WHERE id=?', (order['id'],))[0]
        tx.execute(
            'UPDATE store_packages SET manifest=? WHERE id=?',
            (canonical({'text': 'tampered'}).decode(), package_id),
            write=True,
        )
    denied = await call(
        app, 'delivery.prepare', {'order_id': order['id']}, key=buyer_key, subject=buyer
    )
    assert denied.error.code == 'delivery_package_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],))[0] == 'funded'
        assert tx.one('SELECT COUNT(*) FROM store_deliveries')[0] == 0
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?',
                ('order_release:' + order['id'],),
            )[0]
            == 0
        )


@pytest.mark.asyncio
async def test_concurrent_accept_has_one_release(installed):
    app, _, buyer_identity, order, _ = await _funded(installed)
    buyer_key, buyer = buyer_identity
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order['id']}, key=buyer_key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    args = {
        'order_id': order['id'],
        'delivery_digest': prepared.data['delivery']['delivery_digest'],
    }

    async def accept(rid):
        return await call(app, 'delivery.accept', args, key=buyer_key, subject=buyer, rid=rid)

    results = await asyncio.gather(accept('accept-race-a'), accept('accept-race-b'))
    assert sorted(result.status for result in results) == ['error', 'ok']
    assert (
        next(result for result in results if result.status == 'error').error.code
        == 'delivery_not_acceptable'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?',
                ('order_release:' + order['id'],),
            )[0]
            == 1
        )


@pytest.mark.asyncio
async def test_delivered_order_cannot_refund_and_corrupt_payload_cannot_settle(installed):
    app, _, buyer_identity, order, _ = await _funded(installed)
    buyer_key, buyer = buyer_identity
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order['id']}, key=buyer_key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    assert (
        await call(app, 'orders.cancel', {'order_id': order['id']}, key=buyer_key, subject=buyer)
    ).error.code == 'order_not_cancellable'
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE store_deliveries SET payload_refs=? WHERE order_id=?',
            (canonical([]).decode(), order['id']),
            write=True,
        )
    failed_get = await call(
        app, 'delivery.get', {'order_id': order['id']}, key=buyer_key, subject=buyer
    )
    assert failed_get.error.code == 'delivery_mismatch'
    failed_accept = await call(
        app,
        'delivery.accept',
        {'order_id': order['id'], 'delivery_digest': prepared.data['delivery']['delivery_digest']},
        key=buyer_key,
        subject=buyer,
    )
    assert failed_accept.error.code == 'delivery_mismatch'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],))[0] == 'delivered'
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?',
                ('order_release:' + order['id'],),
            )[0]
            == 0
        )


@pytest.mark.asyncio
async def test_revoked_buyer_key_cannot_accept(installed):
    app, _, buyer_identity, order, _ = await _funded(installed)
    buyer_key, buyer = buyer_identity
    prepared = await call(
        app, 'delivery.prepare', {'order_id': order['id']}, key=buyer_key, subject=buyer
    )
    assert prepared.status == 'ok', wire(prepared)
    replacement = Ed25519Signer.generate()
    public = b64(replacement.public_key)
    proof = replacement.sign(
        canonical({'subject_id': buyer, 'public_key': public}), purpose='key-add'
    )
    added = await call(
        app,
        'identity.key_add',
        {'public_key': public, 'possession_proof': wire(proof), 'ceiling': []},
        key=buyer_key,
        subject=buyer,
    )
    assert added.status == 'ok', wire(added)
    revoked = await call(
        app, 'identity.key_revoke', {'key_id': buyer_key.key_id}, key=buyer_key, subject=buyer
    )
    assert revoked.status == 'ok', wire(revoked)
    denied = await call(
        app,
        'delivery.accept',
        {'order_id': order['id'], 'delivery_digest': prepared.data['delivery']['delivery_digest']},
        key=buyer_key,
        subject=buyer,
    )
    assert denied.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT state FROM store_orders WHERE id=?', (order['id'],))[0] == 'delivered'
        assert (
            tx.one(
                'SELECT COUNT(*) FROM money_ledger WHERE reference=?',
                ('order_release:' + order['id'],),
            )[0]
            == 0
        )
