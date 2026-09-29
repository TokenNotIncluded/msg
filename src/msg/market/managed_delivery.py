"""Shared bounded managed-package verification and local preparation.

These preserve the managed checkout contracts; versioned manual/service delivery,
arbitration and operation registration retain their existing entrypoints.
"""

from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import require
from msg.core.models import BlobRef
from msg.market.catalog import read_package_record
from msg.market.delivery_targets import validate_target
from msg.market.ledger import CURRENCY_ID, balance
from msg.market.order_records import read_order
from msg.plugins.common import new_id

MAX_INLINE_BYTES = 1024 * 1024
_COLUMNS = (
    'id',
    'order_id',
    'recipient_subject',
    'kind',
    'payload_refs',
    'manifest',
    'package_digest',
    'delivery_digest',
    'channel',
    'state',
    'prepared_at',
    'claimed_at',
    'receipt',
)


def read_delivery(tx, order_id):
    row = tx.one(
        """SELECT id,order_id,recipient_subject,kind,payload_refs,
        manifest,package_digest,delivery_digest,channel,state,prepared_at,
        claimed_at,receipt FROM store_deliveries WHERE order_id=?""",
        (order_id,),
    )
    if row is None:
        return None
    result = dict(zip(_COLUMNS, row, strict=True))
    result['payload_refs'] = loads(result['payload_refs'])
    result['manifest'] = loads(result['manifest'])
    result['receipt'] = loads(result['receipt']) if result['receipt'] else None
    return result


def read_buyer_order(tx, order_id, buyer):
    row = read_order(tx, order_id, buyer)
    require(row['buyer'] == buyer, 'order_not_found')
    return row


async def verify_managed_delivery(app, tx, order, delivery):
    validate_target(order, delivery)
    require(
        delivery is not None
        and delivery['recipient_subject'] == order['buyer']
        and delivery['package_digest'] == order['package_digest']
        and delivery['channel'] == 'site',
        'delivery_mismatch',
    )
    kind, manifest, refs = await read_managed_package(app, tx, order)
    body = {
        'delivery_id': delivery['id'],
        'order_id': order['id'],
        'recipient_subject': order['buyer'],
        'kind': kind,
        'manifest': manifest,
        'payload_refs': refs,
        'package_digest': order['package_digest'],
        'channel': 'site',
    }
    require(
        delivery['kind'] == kind
        and delivery['manifest'] == manifest
        and delivery['payload_refs'] == refs
        and delivery['delivery_digest'] == digest(body),
        'delivery_mismatch',
    )


async def read_managed_package(app, tx, order):
    require(
        order['package_id'] and order['package_revision'] and order['package_digest'],
        'delivery_package_mismatch',
    )
    row = await read_package_record(tx, order['package_id'])
    require(
        row is not None
        and row[1] == order['listing_id']
        and row[3] == order['seller']
        and row[4] == order['package_revision']
        and row[8] == order['package_digest']
        and row[10] == 'managed_instant'
        and row[5] in {'file', 'bundle'},
        'delivery_package_mismatch',
    )
    manifest, refs = loads(row[6]), loads(row[7])
    snapshot = {
        'seller': row[3],
        'listing_id': row[1],
        'listing_revision': row[2],
        'revision': row[4],
        'kind': row[5],
        'manifest': manifest,
        'payload_refs': refs,
        'total_size': row[9],
        'delivery_mode': row[10],
    }
    require(
        digest(snapshot) == order['package_digest'] and row[9] <= MAX_INLINE_BYTES and row[9] >= 0,
        'delivery_package_mismatch',
    )
    require(refs and (row[5] == 'bundle' or len(refs) == 1), 'delivery_package_mismatch')
    total = 0
    for ref in refs:
        require(
            isinstance(ref, dict) and isinstance(ref.get('blob'), dict), 'delivery_package_mismatch'
        )
        blob = decode(BlobRef, ref['blob'])
        require(blob.size >= 0 and blob.size <= MAX_INLINE_BYTES, 'delivery_package_mismatch')
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest, 'delivery_package_mismatch')
        total += blob.size
    require(total == row[9], 'delivery_package_mismatch')
    return row[5], manifest, refs


async def read_managed_payloads(app, delivery):
    files = []
    for ref in delivery['payload_refs']:
        blob = decode(BlobRef, ref['blob'])
        data = await app.contents.read_bytes(blob, limit=MAX_INLINE_BYTES)
        require(len(data) == blob.size and digest(data) == blob.digest, 'delivery_package_mismatch')
        files.append({
            'id': ref['id'],
            'revision': ref['revision'],
            'media_type': blob.media_type,
            'digest': blob.digest,
            'size': blob.size,
            'data': b64(data),
        })
    return files


async def prepare_managed(app, ctx, tx, order):
    """Prepare inside the caller's transaction; never authenticate or commit here."""
    buyer = order['buyer']
    require(order['state'] == 'funded' and order['delivered_at'] is None, 'order_not_deliverable')
    validate_target(order)
    require(
        order['currency_id'] == CURRENCY_ID
        and balance(tx, order['escrow_subject']) == order['total_price_minor'],
        'escrow_balance_mismatch',
    )
    kind, manifest, refs = await read_managed_package(app, tx, order)
    delivery_id = new_id('dlv')
    body = {
        'delivery_id': delivery_id,
        'order_id': order['id'],
        'recipient_subject': buyer,
        'kind': kind,
        'manifest': manifest,
        'payload_refs': refs,
        'package_digest': order['package_digest'],
        'channel': 'site',
    }
    delivery_digest = digest(body)
    tx.execute(
        """INSERT INTO store_deliveries
        (id,order_id,recipient_subject,kind,payload_refs,manifest,
         package_digest,delivery_digest,channel,state,prepared_at,claimed_at,receipt)
        VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,NULL,NULL)""",
        (
            delivery_id,
            order['id'],
            buyer,
            kind,
            canonical(refs).decode(),
            canonical(manifest).decode(),
            order['package_digest'],
            delivery_digest,
            'site',
            wire(ctx.now),
        ),
        write=True,
    )
    changed = tx.execute(
        """UPDATE store_orders SET state='delivered',delivered_at=?
        WHERE id=? AND buyer=? AND state='funded' AND delivered_at IS NULL""",
        (wire(ctx.now), order['id'], buyer),
        write=True,
    )
    require(changed.rowcount == 1, 'order_not_deliverable')
    return read_delivery(tx, order['id'])


def delivery_summary(delivery):
    return {
        'delivery_id': delivery['id'],
        'order_id': delivery['order_id'],
        'state': delivery['state'],
        'channel': delivery['channel'],
        'package_digest': delivery['package_digest'],
        'delivery_digest': delivery['delivery_digest'],
    }
