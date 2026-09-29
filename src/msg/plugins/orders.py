"""Signed sale checkout, funded escrow, and pre-delivery buyer cancellation.

Delivery, seller release and arbitration need their own validated state
transitions before escrow may be debited for any other reason.
"""

from __future__ import annotations

from functools import partial

from msg.core.codec import canonical, digest, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput, ResourceTypeSpec
from msg.market.catalog import (
    read_listing as _listing,
    read_listing_body as _body,
    read_package_record as _package_row,
)
from msg.market.escrow import EscrowEngine as EscrowEngine, validate_policy as validate_policy
from msg.market.ledger import (
    CURRENCY_ID as CURRENCY_ID,
    MAX_MINOR as MAX_MINOR,
    account_requirements as account_requirements,
    balance as _balance,
    post_transfer as _post_transfer,
)
from msg.market.order_records import (
    _COLUMNS as _COLUMNS,
    legacy_order_view as _view,
    new_order_id as _order_id,
    read_order as _row,
    require_order_viewer as _viewer,
)

# Compatibility imports; shared implementation has one market owner.
from msg.market.order_records import require_signed_subject as _subject
from msg.plugins.common import registration
from msg.plugins.schemas import IDENTIFIER, obj


def install(app):
    op, finish = registration(app, 'orders', ('store', 'money'))
    from msg.market.order_resources import operation_boundary

    op = operation_boundary(app, op)
    # Account authority belongs to the operation contract, not just the handler:
    # the executor must apply it before returning an idempotent cached result.
    op = partial(op, requirements=account_requirements)
    amount = {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR}
    quantity = {'type': 'integer', 'minimum': 1, 'maximum': 10**9}

    @op(
        'orders.buy',
        obj(
            {
                'listing_id': IDENTIFIER,
                'listing_revision': IDENTIFIER,
                'quantity': quantity,
                'currency_id': {'const': CURRENCY_ID},
                'total_price_minor': amount,
                'package_digest': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'},
                'auto_accept': {'type': 'boolean'},
                'email': {'type': 'string', 'minLength': 3, 'maxLength': 254},
            },
            (
                'listing_id',
                'listing_revision',
                'quantity',
                'currency_id',
                'total_price_minor',
                'package_digest',
            ),
        ),
        signature=True,
        version=2,
    )
    @op(
        'orders.buy',
        obj(
            {
                'listing_id': IDENTIFIER,
                'listing_revision': IDENTIFIER,
                'quantity': quantity,
                'currency_id': {'const': CURRENCY_ID},
                'total_price_minor': amount,
            },
            ('listing_id', 'listing_revision', 'quantity', 'currency_id', 'total_price_minor'),
        ),
        signature=True,
    )
    async def buy(ctx, request, tx):
        buyer = _subject(ctx)
        args = request.arguments
        instant = request.contract_version == 2
        if instant:
            require('delivery' in app.settings.server.plugins, 'delivery_disabled')
            if 'email' in args:
                from msg.market.delivery_targets import validate_address

                validate_address(args['email'])
        listing = await _listing(app, ctx, request, tx, args['listing_id'])
        body, _ = await _body(app, tx, listing)
        require(
            body['mode'] == 'sale'
            and body['state'] == 'active'
            and (body['expires_at'] is None or parse_time(body['expires_at']) > ctx.now),
            'listing_not_available',
        )
        require(
            body['delivery_mode'] == 'managed_instant' and body['item_kind'] in {'file', 'bundle'},
            'order_delivery_mode_unsupported',
        )
        policy = validate_policy(body['escrow_policy'], body['dispute_policy'])
        if not instant:
            require(body['escrow_policy'] == 'escrow-v1', 'escrow_policy_requires_v2')
        if args.get('auto_accept'):
            require('checkout_accept' in policy['reasons'], 'escrow_reason_unsupported')
        require(args['listing_revision'] == listing.revision, 'listing_revision_conflict')
        require(body['currency_id'] == args['currency_id'] == CURRENCY_ID, 'unsupported_currency')
        requested = args['quantity']
        require(requested <= body['quantity'], 'quantity_unavailable')
        sold = tx.one(
            """SELECT COALESCE(SUM(quantity),0) FROM store_orders
            WHERE listing_id=? AND state NOT IN ('cancelled','refunded')""",
            (listing.id,),
        )[0]
        require(int(sold) + requested <= body['quantity'], 'quantity_unavailable')
        total = body['price_minor'] * requested
        require(0 < total <= MAX_MINOR, 'money_overflow')
        require(args['total_price_minor'] == total, 'price_changed')
        require(_balance(tx, buyer) >= total, 'insufficient_funds')
        package_id = body['package_ref']
        package_revision = package_digest = None
        if body['delivery_mode'] == 'managed_instant':
            # The package remains private; checkout sees only its locked digest.
            package = await _package_row(tx, package_id) if package_id else None
            require(
                package is not None
                and package[1] == listing.id
                and package[3] == listing.owner
                and package[5] == body['item_kind']
                and package[10] == body['delivery_mode'],
                'package_not_available',
            )
            require(0 <= package[9] <= 1024 * 1024, 'delivery_package_too_large')
            package_revision, package_digest = package[4], package[8]
        if instant:
            require(args['package_digest'] == package_digest, 'delivery_package_mismatch')
        # Site ownership is separate from an optional, buyer-owned email
        # endpoint. SMTP never changes this authoritative target.
        target = {'subject_id': buyer, 'channel': 'site'}
        order_id = _order_id()
        escrow = 'esc_' + order_id[4:]
        seller = listing.owner
        require(seller != buyer, 'self_purchase_forbidden')
        # The signed orders.buy request is the buyer's PaymentIntent. Its
        # payload digest covers the quoted revision, currency, quantity and
        # total; the server-generated order ID is not falsely attributed to
        # the buyer's signature.
        # Escrow is a LedgerAccount, never an Identity/Subject. It has no
        # Resource, key, credential, signer or network authentication path.
        tx.execute(
            """INSERT INTO ledger_accounts(id,kind,subject_id,source_id)
            VALUES (?,'order_escrow',NULL,?)""",
            (escrow, order_id),
            write=True,
        )
        receipt = _post_transfer(
            tx,
            sender=buyer,
            recipient=escrow,
            amount=total,
            actor=buyer,
            request_id=request.request_id,
            now=ctx.now,
            receipt_signer=app.receipt_signer,
            reference='order_fund:' + order_id,
        )
        receipt_id = receipt['body']['transaction_id']
        tx.execute(
            """INSERT INTO store_orders
            (id,buyer,seller,listing_id,listing_revision,package_id,
             package_revision,package_digest,quantity,unit_price_minor,
             total_price_minor,currency_id,escrow_subject,escrow_policy,
             dispute_policy,terms_digest,delivery_target,payment_intent_digest,
             payment_transaction_id,state,created_at,funded_at,delivered_at,
             settled_at,receipt_refs)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id,
                buyer,
                seller,
                listing.id,
                listing.revision,
                package_id,
                package_revision,
                package_digest,
                requested,
                body['price_minor'],
                total,
                CURRENCY_ID,
                escrow,
                body['escrow_policy'],
                body['dispute_policy'],
                digest(body['terms']),
                canonical(target).decode(),
                request.payload_digest,
                receipt_id,
                'funded',
                wire(ctx.now),
                wire(ctx.now),
                None,
                None,
                canonical([receipt_id]).decode(),
            ),
            write=True,
        )
        data = {'payment': receipt}
        if instant:
            from msg.market.delivery_notifications import initialize_notification
            from msg.market.managed_delivery import delivery_summary, prepare_managed

            order = _row(tx, order_id, buyer)
            delivery = await prepare_managed(app, ctx, tx, order)
            data['delivery'] = delivery_summary(delivery)
            if args.get('auto_accept'):
                released, decision, _ = await EscrowEngine(app).settle(
                    ctx, request, tx, reason='checkout_accept', order_id=order_id
                )
                data.update(settlement=released, decision=decision)
            data['notification'] = await initialize_notification(
                app, ctx, request, tx, _row(tx, order_id, buyer), args.get('email')
            )
        data['order'] = _view(_row(tx, order_id, buyer), buyer)
        return HandlerOutput(data=data)

    @op('orders.cancel', obj({'order_id': IDENTIFIER}, ('order_id',)), signature=True)
    async def cancel(ctx, request, tx):
        buyer = _subject(ctx)
        refund, decision, _ = await EscrowEngine(app).settle(
            ctx, request, tx, reason='buyer_cancel'
        )
        return HandlerOutput(
            data={
                'order': _view(_row(tx, request.arguments['order_id'], buyer), buyer),
                'refund': refund,
                'decision': decision,
            }
        )

    @op('orders.get', obj({'order_id': IDENTIFIER}, ('order_id',)), effect='read')
    async def get(ctx, request, tx):
        viewer = _viewer(ctx)
        from msg.market.orders import view

        return HandlerOutput(
            data={'order': view(tx, _row(tx, request.arguments['order_id'], viewer), viewer)}
        )

    @op(
        'orders.get',
        obj(
            {'order_id': IDENTIFIER, 'source': {'enum': ['store_order', 'legacy_purchase']}},
            ('order_id',),
        ),
        effect='read',
        version=2,
    )
    async def get_compatible(ctx, request, tx):
        if request.arguments.get('source', 'store_order') == 'store_order':
            return await get(ctx, request, tx)
        from msg.core.errors import Failure
        from msg.market.compatibility import purchase_order
        from msg.market.compatibility import read_purchase as _purchase

        try:
            purchase = _purchase(tx, request.arguments['order_id'], _viewer(ctx))
        except Failure as exc:
            if exc.code == 'purchase_not_found':
                raise Failure('order_not_found') from None
            raise
        return HandlerOutput(data={'order': purchase_order(tx, purchase)})

    @op(
        'orders.list',
        obj({
            'role': {'enum': ['buy', 'sell']},
            'status': {'enum': ['open', 'completed', 'disputed']},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        }),
        effect='read',
    )
    async def list_orders(ctx, request, tx):
        viewer = _viewer(ctx)
        args = request.arguments
        role = args.get('role')
        status = args.get('status')
        limit = args.get('limit', 50)
        where = '(buyer=? OR seller=?)'
        values = [viewer, viewer]
        if role:
            where = 'buyer=?' if role == 'buy' else 'seller=?'
            values = [viewer]
        if status:
            where += (
                " AND state IN ('created','funded','delivered','accepted')"
                if status == 'open'
                else " AND state IN ('settled','cancelled','refunded')"
                if status == 'completed'
                else " AND state='disputed'"
            )
        rows = tx.rows(
            'SELECT id FROM store_orders WHERE '
            + where
            + ' ORDER BY created_at DESC,id DESC LIMIT ?',
            (*values, limit),
        )
        return HandlerOutput(
            data={'orders': [_view(_row(tx, id, viewer), viewer) for (id,) in rows]}
        )

    @op(
        'orders.list',
        obj({
            'role': {'enum': ['buy', 'sell']},
            'status': {'enum': ['open', 'completed', 'disputed']},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        }),
        effect='read',
        version=2,
    )
    async def list_compatible(ctx, request, tx):
        viewer = _viewer(ctx)
        native = await list_orders(ctx, request, tx)
        projected = [{**row, 'source': 'store_order'} for row in native.data['orders']]
        args = request.arguments
        limit = args.get('limit', 50)
        if args.get('role') != 'sell' and args.get('status') != 'disputed':
            from msg.market.compatibility import purchase_order
            from msg.market.compatibility import read_purchase as _purchase

            where, values = 'subject_id=?', [viewer]
            if args.get('status'):
                where += (
                    " AND state='pending'"
                    if args['status'] == 'open'
                    else " AND state IN ('settled','refunded')"
                )
            rows = tx.rows(
                'SELECT id FROM money_purchases WHERE '
                + where
                + ' ORDER BY created_at DESC,id DESC LIMIT ?',
                (*values, limit),
            )
            projected.extend(purchase_order(tx, _purchase(tx, id_, viewer)) for (id_,) in rows)
        projected.sort(key=lambda row: (row['created_at'], row['id']), reverse=True)
        return HandlerOutput(data={'orders': projected[:limit]})

    @op('orders.payment', obj({'order_id': IDENTIFIER}, ('order_id',)), effect='read')
    async def payment(ctx, request, tx):
        viewer = _viewer(ctx)
        row = _row(tx, request.arguments['order_id'], viewer)
        if tx.one('SELECT 1 FROM order_contracts WHERE order_id=?', (row['id'],)):
            from msg.market.policy import contract

            contract(tx, row['id'])
        result = {
            'order_id': row['id'],
            'status': _view(row, viewer)['payment_status'],
            'amount_minor': row['total_price_minor'],
            'currency_id': row['currency_id'],
        }
        if viewer == row['buyer']:
            receipts = []
            for transaction_id in row['receipt_refs']:
                ledger = tx.one('SELECT receipt FROM money_ledger WHERE id=?', (transaction_id,))
                require(ledger is not None, 'order_not_found')
                receipts.append(loads(ledger[0]))
            result['receipt'] = receipts[0] if receipts else None
            result['receipts'] = receipts
            result['escrow_balance_minor'] = _balance(tx, row['escrow_subject'])
        return HandlerOutput(data={'payment': result})

    from msg.market.orders import install as install_market

    install_market(app, op)
    from msg.market.arbitration import install as install_arbitration

    install_arbitration(app, op)
    finish(
        tuple(
            ResourceTypeSpec(
                name=name,
                version=1,
                container=container,
                content_schema=None,
                operations=frozenset(),
                relations=frozenset(),
            )
            for name, container in (('order', False), ('order_collection', True))
        )
    )
