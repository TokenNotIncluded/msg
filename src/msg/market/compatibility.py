"""Versioned read-only Listing/Order adapters for legacy entitlement records.

No synthetic Resource, Delivery, acceptance, receipt or second escrow is created.
Mutation handlers must continue to load their own authoritative record type.
"""

from msg.constants import ROOT_SUBJECT
from msg.core.codec import loads
from msg.core.errors import require
from msg.market.ledger import CURRENCY_ID

LEGACY_POLICY = 'legacy-entitlement-v1'


def offer_listing(offer):
    return {
        'listing_id': offer['offer_id'],
        'listing_revision': offer['price_revision'],
        'source': 'server_offer',
        'mode': 'sale',
        'seller': ROOT_SUBJECT,
        'item_kind': 'entitlement',
        'state': 'active',
        'price_minor': offer['price_minor'],
        'currency_id': CURRENCY_ID,
        'unit': offer['unit'],
        'min_quantity': offer['min_quantity'],
        'max_quantity': offer['max_quantity'],
        'resource_kind': offer['resource_kind'],
        'entitlement_kind': offer['entitlement_kind'],
        'duration_seconds': offer['duration_seconds'],
        'escrow_policy': LEGACY_POLICY,
        'purchase_operation': 'money.redeem',
        'purchase_versions': [1, 2],
        'offer_snapshot': dict(offer),
    }


def purchase_order(tx, purchase):
    """Project actual ledger facts; reject incomplete/unknown legacy records."""
    p, quote = purchase, purchase['offer_snapshot']
    require(p['state'] in {'pending', 'settled', 'refunded'}, 'legacy_purchase_invalid')
    require(
        isinstance(quote, dict)
        and {'currency_id', 'price_minor', 'offer_id', 'price_revision', 'provider_version'}
        <= quote.keys(),
        'legacy_purchase_invalid',
    )
    require(
        quote['currency_id'] == CURRENCY_ID
        and quote['provider_version'] == 1
        and type(quote['price_minor']) is int
        and quote['price_minor'] > 0
        and p['quantity'] * quote['price_minor'] == p['total_minor'],
        'legacy_purchase_invalid',
    )
    account = tx.one(
        'SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=?', (p['escrow_account'],)
    )
    require(account == ('purchase_escrow', None, p['id']), 'legacy_purchase_invalid')
    funding = tx.one(
        """SELECT debit_account,credit_account,amount_minor,committed_at
        FROM money_ledger WHERE id=?""",
        (p['funding_transaction_id'],),
    )
    require(
        funding is not None
        and funding[:3] == (p['subject_id'], p['escrow_account'], p['total_minor']),
        'legacy_purchase_invalid',
    )
    final_at = None
    refs = [p['funding_transaction_id']]
    if p['state'] == 'pending':
        require(
            p['final_transaction_id'] is None and p['entitlement_id'] is None,
            'legacy_purchase_invalid',
        )
    else:
        final = tx.one(
            """SELECT debit_account,credit_account,amount_minor,committed_at
            FROM money_ledger WHERE id=?""",
            (p['final_transaction_id'],),
        )
        recipient = ROOT_SUBJECT if p['state'] == 'settled' else p['subject_id']
        require(
            final is not None and final[:3] == (p['escrow_account'], recipient, p['total_minor']),
            'legacy_purchase_invalid',
        )
        if p['state'] == 'settled':
            entitlement = tx.one(
                """SELECT subject_id,purchase_request_id,redeem_transaction_id
                FROM resource_entitlements WHERE id=?""",
                (p['entitlement_id'],),
            )
            require(
                entitlement == (p['subject_id'], p['request_id'], p['final_transaction_id']),
                'legacy_purchase_invalid',
            )
        else:
            require(p['entitlement_id'] is None, 'legacy_purchase_invalid')
        final_at = final[3]
        refs.append(p['final_transaction_id'])
    return {
        'id': p['id'],
        'source': 'legacy_purchase',
        'buyer': p['subject_id'],
        'seller': ROOT_SUBJECT,
        'listing_id': quote['offer_id'],
        'listing_revision': quote['price_revision'],
        'listing_source': 'server_offer',
        'quantity': p['quantity'],
        'unit_price_minor': quote['price_minor'],
        'total_price_minor': p['total_minor'],
        'currency_id': quote['currency_id'],
        'escrow_account_id': p['escrow_account'],
        'escrow_policy': LEGACY_POLICY,
        'state': 'funded' if p['state'] == 'pending' else p['state'],
        'legacy_state': p['state'],
        'created_at': p['created_at'],
        'funded_at': funding[3],
        'settled_at': final_at if p['state'] == 'settled' else None,
        'refunded_at': final_at if p['state'] == 'refunded' else None,
        'accepted_at': None,
        'delivery_id': None,
        'delivered_at': None,
        'entitlement_id': p['entitlement_id'],
        'receipt_refs': refs,
        'offer_snapshot': dict(quote),
    }


def read_purchase(tx, purchase_id, subject):
    row = tx.one(
        """SELECT id,subject_id,request_id,offer_snapshot,quantity,total_minor,
        escrow_account,state,created_at,expires_at,funding_transaction_id,
        final_transaction_id,entitlement_id,reason FROM money_purchases WHERE id=? AND subject_id=?""",
        (purchase_id, subject),
    )
    require(row is not None, 'purchase_not_found')
    result = dict(
        zip(
            (
                'id',
                'subject_id',
                'request_id',
                'offer_snapshot',
                'quantity',
                'total_minor',
                'escrow_account',
                'state',
                'created_at',
                'expires_at',
                'funding_transaction_id',
                'final_transaction_id',
                'entitlement_id',
                'reason',
            ),
            row,
            strict=True,
        )
    )
    result['offer_snapshot'] = loads(result['offer_snapshot'])
    return result
