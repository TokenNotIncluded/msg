"""Buyer-only in-site delivery of an immutable managed package.

The versioned checkout can prepare a package atomically. A signed acceptance
or an explicit, digest-bound checkout policy controls settlement; merely reading
or sending an email never claims a delivery.
"""
from __future__ import annotations

from functools import partial

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import BlobRef, HandlerOutput
from msg.plugins.common import new_id, registration
from msg.market.ledger import CURRENCY_ID, balance as _balance, account_requirements
from msg.market.escrow import EscrowEngine
from msg.market.delivery_targets import validate_target
from msg.market.order_records import read_order as order_row, require_signed_subject as _subject
from msg.plugins.schemas import IDENTIFIER, obj
from msg.market.catalog import read_package_record as _package_row


# Compatibility imports; shared implementation has one market owner.
from msg.market.managed_delivery import read_delivery as _delivery
from msg.market.managed_delivery import read_buyer_order as _buyer_order
from msg.market.managed_delivery import verify_managed_delivery as _verified_delivery
from msg.market.managed_delivery import read_managed_package as _package
from msg.market.managed_delivery import read_managed_payloads as _payload
from msg.market.managed_delivery import prepare_managed
from msg.market.managed_delivery import delivery_summary
from msg.market.managed_delivery import MAX_INLINE_BYTES
from msg.market.managed_delivery import _COLUMNS


def install(app):
    op, finish = registration(app, 'delivery', ('orders',))
    # Account authority belongs to the operation contract, not just the handler:
    # the executor must apply it before returning an idempotent cached result.
    op = partial(op, requirements=account_requirements)

    @op('delivery.prepare', obj({'order_id': IDENTIFIER}, ('order_id',)),
        signature=True)
    async def prepare(ctx, request, tx):
        buyer = _subject(ctx)
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        delivery = await prepare_managed(app, ctx, tx, order)
        return HandlerOutput(data={'delivery': delivery_summary(delivery)})

    @op('delivery.get', obj({'order_id': IDENTIFIER}, ('order_id',)), effect='read')
    async def get(ctx, request, tx):
        buyer = ctx.principal.subject
        require(buyer is not None and ctx.principal.actor == buyer,
                'order_not_found')
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        if tx.one('SELECT 1 FROM order_contracts WHERE order_id=?', (order['id'],)):
            from msg.market.delivery import read
            return await read(app, tx, ctx, request)
        delivery = _delivery(tx, order['id'])
        require(delivery is not None, 'delivery_not_found')
        await _verified_delivery(app, tx, order, delivery)
        from msg.market.delivery_notifications import notification_status
        return HandlerOutput(data={'delivery': {
            'delivery_id': delivery['id'], 'order_id': order['id'],
            'recipient_subject': buyer, 'kind': delivery['kind'],
            'manifest': delivery['manifest'], 'package_digest': delivery['package_digest'],
            'delivery_digest': delivery['delivery_digest'], 'channel': 'site',
            'state': delivery['state'], 'prepared_at': delivery['prepared_at'],
            'claimed_at': delivery['claimed_at'],
            'payloads': await _payload(app, delivery),
            'notification': await notification_status(app, tx, order['id'])}})

    @op('delivery.accept', obj({'order_id': IDENTIFIER,
        'delivery_digest': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}},
        ('order_id', 'delivery_digest')), signature=True)
    async def accept(ctx, request, tx):
        if tx.one('SELECT 1 FROM order_contracts WHERE order_id=?', (request.arguments['order_id'],)):
            from msg.market.delivery import accept as market_accept
            return await market_accept(app, tx, ctx, request)
        receipt,decision,delivery = await EscrowEngine(app).settle(
            ctx,request,tx,reason='buyer_accept')
        return HandlerOutput(data={'order_id':request.arguments['order_id'],'state':'settled',
            'delivery_id':delivery['id'],'receipt':receipt,'decision':decision})

    @op('delivery.claim', obj({'order_id': IDENTIFIER,
        'delivery_digest': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}},
        ('order_id', 'delivery_digest')), signature=True)
    async def claim(ctx, request, tx):
        # Checkout settlement is not evidence of receipt by the buyer. This
        # later signed ACK only changes the delivery; it cannot debit escrow.
        buyer = _subject(ctx)
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        delivery = _delivery(tx, order['id'])
        await _verified_delivery(app, tx, order, delivery)
        require(delivery['delivery_digest'] == request.arguments['delivery_digest'],
                'delivery_mismatch')
        await EscrowEngine(app).verify_checkout_release(tx, order, delivery)
        require(delivery['state'] == 'prepared', 'delivery_not_acceptable')
        changed = tx.execute("""UPDATE store_deliveries SET state='claimed',claimed_at=?
            WHERE order_id=? AND recipient_subject=? AND state='prepared'""",
            (wire(ctx.now), order['id'], buyer), write=True)
        require(changed.rowcount == 1, 'delivery_not_acceptable')
        return HandlerOutput(data={'order_id': order['id'],
            'delivery_id': delivery['id'], 'state': 'claimed'})

    @op('delivery.notify', obj({'order_id': IDENTIFIER,
        'enabled': {'type': 'boolean'}}, ('order_id',)), signature=True)
    async def notify(ctx, request, tx):
        from msg.market.delivery_notifications import queue_notification
        buyer = _subject(ctx)
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        await _verified_delivery(app, tx, order, _delivery(tx, order['id']))
        status = await queue_notification(app, ctx, request, tx, order,
                                          enabled=request.arguments.get('enabled', True))
        return HandlerOutput(data={'order_id': order['id'], 'notification': status})

    from msg.market.delivery import install as install_market
    install_market(app, op)
    finish()
