"""Buyer-only in-site delivery of an immutable managed package.

Preparing and accepting are explicit signed operations. The latter is the only
path here that releases escrow, after rechecking the order and delivery facts.
"""
from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import BlobRef, HandlerOutput
from msg.plugins.common import new_id, registration
from msg.plugins.money import CURRENCY_ID, _balance
from msg.market.escrow import EscrowEngine
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


def install(app):
    op, finish = registration(app, 'delivery', ('orders',))

    @op('delivery.prepare', obj({'order_id': IDENTIFIER}, ('order_id',)),
        signature=True)
    async def prepare(ctx, request, tx):
        buyer = _subject(ctx)
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        require(order['state'] == 'funded' and order['delivered_at'] is None,
                'order_not_deliverable')
        target = order['delivery_target']
        require(target == {'subject_id': buyer, 'channel': 'site'},
                'delivery_recipient_mismatch')
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
        return HandlerOutput(data={'delivery': {'delivery_id': delivery_id,
            'order_id': order['id'], 'state': 'prepared', 'channel': 'site',
            'package_digest': order['package_digest'],
            'delivery_digest': delivery_digest}})

    @op('delivery.get', obj({'order_id': IDENTIFIER}, ('order_id',)), effect='read')
    async def get(ctx, request, tx):
        buyer = ctx.principal.subject
        require(buyer is not None and ctx.principal.actor == buyer,
                'order_not_found')
        order = _buyer_order(tx, request.arguments['order_id'], buyer)
        delivery = _delivery(tx, order['id'])
        require(delivery is not None, 'delivery_not_found')
        await _verified_delivery(app, tx, order, delivery)
        return HandlerOutput(data={'delivery': {
            'delivery_id': delivery['id'], 'order_id': order['id'],
            'recipient_subject': buyer, 'kind': delivery['kind'],
            'manifest': delivery['manifest'], 'package_digest': delivery['package_digest'],
            'delivery_digest': delivery['delivery_digest'], 'channel': 'site',
            'state': delivery['state'], 'prepared_at': delivery['prepared_at'],
            'claimed_at': delivery['claimed_at'],
            'payloads': await _payload(app, delivery)}})

    @op('delivery.accept', obj({'order_id': IDENTIFIER,
        'delivery_digest': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}},
        ('order_id', 'delivery_digest')), signature=True)
    async def accept(ctx, request, tx):
        receipt,decision,delivery = await EscrowEngine(app).settle(
            ctx,request,tx,reason='buyer_accept')
        return HandlerOutput(data={'order_id':request.arguments['order_id'],'state':'settled',
            'delivery_id':delivery['id'],'receipt':receipt,'decision':decision})

    finish()
