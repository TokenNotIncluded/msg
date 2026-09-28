"""Buyer-only in-site delivery of an immutable managed package.

The versioned checkout can prepare a package atomically. A signed acceptance
or an explicit, digest-bound checkout policy controls settlement; merely reading
or sending an email never claims a delivery.
"""
from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import BlobRef, HandlerOutput
from msg.plugins.common import new_id, registration
from msg.plugins.money import CURRENCY_ID, _balance
from msg.market.escrow import EscrowEngine
from msg.market.delivery_targets import validate_target
from msg.plugins.orders import _row as order_row, _subject
from msg.plugins.schemas import IDENTIFIER, obj
from msg.plugins.store import _package_row


MAX_INLINE_BYTES = 1024 * 1024
_COLUMNS = ('id', 'order_id', 'recipient_subject', 'kind', 'payload_refs',
            'manifest', 'package_digest', 'delivery_digest', 'channel',
            'state', 'prepared_at', 'claimed_at', 'receipt')


def _delivery(tx, order_id):
    row = tx.one('''SELECT id,order_id,recipient_subject,kind,payload_refs,
        manifest,package_digest,delivery_digest,channel,state,prepared_at,
        claimed_at,receipt FROM store_deliveries WHERE order_id=?''', (order_id,))
    if row is None:
        return None
    result = dict(zip(_COLUMNS, row))
    result['payload_refs'] = loads(result['payload_refs'])
    result['manifest'] = loads(result['manifest'])
    result['receipt'] = loads(result['receipt']) if result['receipt'] else None
    return result


def _buyer_order(tx, order_id, buyer):
    row = order_row(tx, order_id, buyer)
    require(row['buyer'] == buyer, 'order_not_found')
    return row


async def _verified_delivery(app, tx, order, delivery):
    validate_target(order, delivery)
    require(delivery is not None and
            delivery['recipient_subject'] == order['buyer'] and
            delivery['package_digest'] == order['package_digest'] and
            delivery['channel'] == 'site', 'delivery_mismatch')
    kind, manifest, refs = await _package(app, tx, order)
    body = {'delivery_id': delivery['id'], 'order_id': order['id'],
            'recipient_subject': order['buyer'], 'kind': kind,
            'manifest': manifest, 'payload_refs': refs,
            'package_digest': order['package_digest'], 'channel': 'site'}
    require(delivery['kind'] == kind and delivery['manifest'] == manifest and
            delivery['payload_refs'] == refs and
            delivery['delivery_digest'] == digest(body), 'delivery_mismatch')


async def _package(app, tx, order):
    require(order['package_id'] and order['package_revision'] and
            order['package_digest'], 'delivery_package_mismatch')
    row = await _package_row(tx, order['package_id'])
    require(row is not None and row[1] == order['listing_id'] and
            row[3] == order['seller'] and row[4] == order['package_revision'] and
            row[8] == order['package_digest'] and row[10] == 'managed_instant' and
            row[5] in {'file', 'bundle'}, 'delivery_package_mismatch')
    manifest, refs = loads(row[6]), loads(row[7])
    snapshot = {'seller': row[3], 'listing_id': row[1],
                'listing_revision': row[2], 'revision': row[4], 'kind': row[5],
                'manifest': manifest, 'payload_refs': refs,
                'total_size': row[9], 'delivery_mode': row[10]}
    require(digest(snapshot) == order['package_digest'] and
            row[9] <= MAX_INLINE_BYTES and row[9] >= 0,
            'delivery_package_mismatch')
    require(refs and (row[5] == 'bundle' or len(refs) == 1),
            'delivery_package_mismatch')
    total = 0
    for ref in refs:
        require(isinstance(ref, dict) and isinstance(ref.get('blob'), dict),
                'delivery_package_mismatch')
        blob = decode(BlobRef, ref['blob'])
        require(blob.size >= 0 and blob.size <= MAX_INLINE_BYTES,
                'delivery_package_mismatch')
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest,
                'delivery_package_mismatch')
        total += blob.size
    require(total == row[9], 'delivery_package_mismatch')
    return row[5], manifest, refs


async def _payload(app, delivery):
    files = []
    for ref in delivery['payload_refs']:
        blob = decode(BlobRef, ref['blob'])
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest,
                'delivery_package_mismatch')
        files.append({'id': ref['id'], 'revision': ref['revision'],
                      'media_type': blob.media_type, 'digest': blob.digest,
                      'size': blob.size, 'data': b64(data)})
    return files


async def prepare_managed(app, ctx, tx, order):
    """Prepare inside the caller's transaction; never authenticate or commit here."""
    buyer = order['buyer']
    require(order['state'] == 'funded' and order['delivered_at'] is None,
            'order_not_deliverable')
    validate_target(order)
    require(order['currency_id'] == CURRENCY_ID and
            _balance(tx, order['escrow_subject']) == order['total_price_minor'],
            'escrow_balance_mismatch')
    kind, manifest, refs = await _package(app, tx, order)
    delivery_id = new_id('dlv')
    body = {'delivery_id': delivery_id, 'order_id': order['id'],
            'recipient_subject': buyer, 'kind': kind,
            'manifest': manifest, 'payload_refs': refs,
            'package_digest': order['package_digest'], 'channel': 'site'}
    delivery_digest = digest(body)
    tx.execute('''INSERT INTO store_deliveries
        (id,order_id,recipient_subject,kind,payload_refs,manifest,
         package_digest,delivery_digest,channel,state,prepared_at,claimed_at,receipt)
        VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,NULL,NULL)''',
        (delivery_id, order['id'], buyer, kind, canonical(refs).decode(),
         canonical(manifest).decode(), order['package_digest'],
         delivery_digest, 'site', wire(ctx.now)), write=True)
    changed = tx.execute('''UPDATE store_orders SET state='delivered',delivered_at=?
        WHERE id=? AND buyer=? AND state='funded' AND delivered_at IS NULL''',
        (wire(ctx.now), order['id'], buyer), write=True)
    require(changed.rowcount == 1, 'order_not_deliverable')
    return _delivery(tx, order['id'])


def delivery_summary(delivery):
    return {'delivery_id': delivery['id'], 'order_id': delivery['order_id'],
            'state': delivery['state'], 'channel': delivery['channel'],
            'package_digest': delivery['package_digest'],
            'delivery_digest': delivery['delivery_digest']}


def install(app):
    op, finish = registration(app, 'delivery', ('orders',))

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
