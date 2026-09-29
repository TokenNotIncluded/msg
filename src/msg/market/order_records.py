"""Shared order identity, authorized records and legacy-compatible projections.

These functions own record access, not operation registration or settlement.
"""
from __future__ import annotations
import base64
import os
from msg.core.codec import loads
from msg.core.errors import require

_COLUMNS = ('id', 'buyer', 'seller', 'listing_id', 'listing_revision',
            'package_id', 'package_revision', 'package_digest', 'quantity',
            'unit_price_minor', 'total_price_minor', 'currency_id',
            'escrow_subject', 'escrow_policy', 'dispute_policy', 'terms_digest',
            'delivery_target', 'payment_intent_digest', 'payment_transaction_id',
            'state', 'created_at', 'funded_at', 'delivered_at', 'settled_at',
            'receipt_refs')


def require_signed_subject(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject and
            ctx.principal.method == 'signature', 'signature_required')
    return subject


def require_order_viewer(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject,
            'order_not_found')
    return subject


def new_order_id():
    # 160 random bits, unguessable even if an attacker sees other order IDs.
    return 'ord_' + base64.b32encode(os.urandom(20)).decode('ascii').rstrip('=').lower()


def read_order(tx, order_id, viewer):
    row = tx.one('''SELECT id,buyer,seller,listing_id,listing_revision,
        package_id,package_revision,package_digest,quantity,unit_price_minor,
        total_price_minor,currency_id,escrow_subject,escrow_policy,
        dispute_policy,terms_digest,delivery_target,payment_intent_digest,
        payment_transaction_id,state,created_at,funded_at,delivered_at,
        settled_at,receipt_refs FROM store_orders WHERE id=?''', (order_id,))
    # A valid ID is not an access grant. Keep nonexistent and unauthorized alike.
    require(row is not None and viewer in row[1:3], 'order_not_found')
    from msg.market.order_resources import source_metadata
    source_metadata(tx, order_id)
    result = dict(zip(_COLUMNS, row))
    result['delivery_target'] = loads(result['delivery_target'])
    result['receipt_refs'] = loads(result['receipt_refs'])
    return result


def legacy_order_view(row, viewer):
    keys = ('id', 'buyer', 'seller', 'listing_id', 'listing_revision',
            'package_revision', 'package_digest', 'quantity',
            'unit_price_minor', 'total_price_minor', 'currency_id',
            'escrow_policy', 'dispute_policy', 'terms_digest', 'state',
            'created_at', 'funded_at', 'delivered_at', 'settled_at')
    result = {key: row[key] for key in keys}
    result['payment_status'] = ('refunded' if row['state'] == 'refunded' else
                                'funded' if row['payment_transaction_id'] else
                                'pending')
    result['delivery_channel'] = row['delivery_target']['channel']
    if viewer == row['buyer']:
        result['delivery_target'] = row['delivery_target']
        result['payment_intent_digest'] = row['payment_intent_digest']
        result['receipt_refs'] = row['receipt_refs']
    return result
