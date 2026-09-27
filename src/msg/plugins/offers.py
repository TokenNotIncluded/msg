"""Server resource offers, closed until a Registry-backed entitlement is usable.

An offer is a business fact, never a grant of authorization.  Merely inserting a
row into ``server_offers`` cannot make a resource purchasable: both the Registry
and an installed deterministic entitlement fulfiller must explicitly opt in.
"""
from __future__ import annotations

from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import registration
from msg.plugins.money import CURRENCY_ID, MAX_MINOR, _owner
from msg.plugins.schemas import IDENTIFIER, obj


# No quota consumer/entitlement fulfiller is installed yet.  Keep this mapping
# empty rather than treating a money transfer as successful resource delivery.
ENTITLEMENT_FULFILLERS = {}
FORBIDDEN_KINDS = frozenset({
    'user', 'organization', 'certificate', 'csr', 'delegation', 'keystore',
    'identity', 'capability', 'admin', 'system', 'priority', 'bank_role',
})


def purchasable(app, resource_kind: str, entitlement_kind: str) -> bool:
    """Check both the published type contract and actual quota enforcement."""
    if resource_kind in FORBIDDEN_KINDS:
        return False
    try:
        spec = app.registry.resource_type(resource_kind, 1)
    except Failure:
        return False
    return (getattr(spec, 'purchasable', False) is True and
            entitlement_kind in ENTITLEMENT_FULFILLERS)


def validate_local_offer(app, *, resource_kind: str, entitlement_kind: str,
                         unit: str, price_minor: int, min_quantity: int,
                         max_quantity: int, duration_seconds: int | None = None):
    """RootAdmin's offer set entrypoint must call this before any DB write.

    The current Registry has no purchasable types, so all sets fail closed.
    Disabling an existing offer remains a local administrative operation.
    """
    require(purchasable(app, resource_kind, entitlement_kind),
            'resource_not_purchasable')
    require(isinstance(unit, str) and 0 < len(unit) <= 64, 'invalid_offer_unit')
    require(type(price_minor) is int and 0 < price_minor <= MAX_MINOR,
            'invalid_offer_price')
    require(type(min_quantity) is int and type(max_quantity) is int and
            0 < min_quantity <= max_quantity and price_minor * max_quantity <= MAX_MINOR,
            'invalid_offer_quantity')
    require(duration_seconds is None or
            (type(duration_seconds) is int and 0 < duration_seconds <= 315360000),
            'invalid_offer_duration')


def _public_offer(row):
    return {'offer_id': row[0], 'resource_kind': row[1], 'unit': row[2],
            'price_minor': row[3], 'min_quantity': row[4], 'max_quantity': row[5],
            'entitlement_kind': row[6], 'duration_seconds': row[7],
            'price_revision': row[8], 'currency_id': CURRENCY_ID}


def install(app):
    op, finish = registration(app, 'offers', ('money',))

    @op('money.offers', obj(), effect='read')
    async def offers(ctx, request, tx):
        rows = tx.rows('''SELECT offer_id,resource_kind,unit,price_minor,min_quantity,
                               max_quantity,entitlement_kind,duration_seconds,price_revision
                        FROM server_offers WHERE enabled=TRUE ORDER BY offer_id''')
        return HandlerOutput(data={'currency_id': CURRENCY_ID,
            'offers': [_public_offer(row) for row in rows
                       if purchasable(app, row[1], row[6])]})

    @op('money.redeem', obj({
        'offer_id': IDENTIFIER,
        'quantity': {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR},
        'currency_id': {'const': CURRENCY_ID},
        'price_revision': IDENTIFIER,
    }, ('offer_id', 'quantity', 'currency_id', 'price_revision')), signature=True)
    async def redeem(ctx, request, tx):
        _owner(ctx)
        args = request.arguments
        row = tx.one('''SELECT offer_id,resource_kind,unit,price_minor,min_quantity,
                              max_quantity,entitlement_kind,duration_seconds,price_revision
                       FROM server_offers WHERE offer_id=? AND enabled=TRUE''',
                     (args['offer_id'],))
        require(row is not None and purchasable(app, row[1], row[6]),
                'offer_not_found')
        # A price change must invalidate a prior quote, even if the numeric
        # price returns to the same value.  No funds are touched before this.
        require(args['price_revision'] == row[8], 'offer_price_changed')
        require(row[4] <= args['quantity'] <= row[5] and
                row[3] * args['quantity'] <= MAX_MINOR, 'invalid_offer_quantity')
        # A future fulfiller must atomically post a redeem ledger entry and
        # deterministic ResourceEntitlement in this same transaction.  There
        # is deliberately no placeholder success or asynchronous fire-and-forget.
        require(False, 'entitlement_provider_unavailable')

    finish()
