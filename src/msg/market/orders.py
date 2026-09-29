"""Versioned checkout, preserving published orders.buy@1/@2/@3 semantics."""
from __future__ import annotations

from datetime import timedelta

from msg.core.codec import canonical, digest, parse_time, wire
from msg.core.errors import require
from msg.core.models import HandlerOutput
from msg.market.escrow import settle, transition
from msg.market.policy import contract, snapshot
from msg.market.targets import (
    enqueue_notification, notification_view, save_target, target_for, validate_target,
)
from msg.market.ledger import CURRENCY_ID, MAX_MINOR, post_transfer as _post_transfer
from msg.market.order_records import new_order_id as _order_id, read_order as _row, require_signed_subject as _subject, legacy_order_view as _view, require_order_viewer as _viewer
from msg.plugins.schemas import IDENTIFIER, obj
from msg.market.catalog import read_listing_body as _body, read_listing as _listing, read_package_record as _package_row

HASH = {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'}
ORDER = obj({'order_id': IDENTIFIER}, ('order_id',))
INTENT = {
    'listing_id': IDENTIFIER, 'listing_revision': IDENTIFIER,
    'quantity': {'type': 'integer', 'minimum': 1, 'maximum': 10**9},
    'currency_id': {'const': CURRENCY_ID},
    'total_price_minor': {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR},
    'email': {'type': 'string', 'minLength': 3, 'maxLength': 254},
}
INTENT_REQUIRED = ('listing_id','listing_revision','quantity','currency_id','total_price_minor')


def view(tx, order, viewer):
    email = notification_view(tx, order)
    projected = {**order, 'delivery_target': {**order['delivery_target'], 'email': email}}
    result = _view(projected, viewer)
    row = tx.one('SELECT digest FROM order_contracts WHERE order_id=?', (order['id'],))
    if row:
        result['order_digest'] = row[0]
        locked = contract(tx, order['id'])
        result['contract_version'] = locked['version']
        result['delivery_mode'] = locked['listing']['delivery_mode']
        result['policy_digest'] = locked['policy']['policy_digest']
        if locked['version'] == 5:
            from msg.core.codec import loads
            fact = tx.one('SELECT body FROM order_settlements WHERE order_id=?', (order['id'],))
            result['settlement_policy'] = locked['settlement_policy']
            result['entitlement_id'] = loads(fact[0])['entitlement_id'] if fact else None
        # Seller needs the *public encryption subkey*, not buyer's email.
        if locked['recipient_key']:
            result['recipient_key'] = locked['recipient_key']
    result['email_status'] = email['state']
    return result


async def create(app, tx, ctx, request, *, version=3):
    buyer, args = _subject(ctx), request.arguments
    listing = await _listing(app, ctx, request, tx, args['listing_id'])
    body, revision = await _body(app, tx, listing)
    require(body['mode'] == 'sale' and body['state'] == 'active' and
            (body['expires_at'] is None or parse_time(body['expires_at']) > ctx.now),
            'listing_not_available')
    require(args['listing_revision'] == listing.revision, 'listing_revision_conflict')
    require(body['escrow_policy'] == 'escrow-v1', 'escrow_policy_unknown')
    policy = snapshot(tx, body['dispute_policy'])
    require(body['currency_id'] == args['currency_id'] == CURRENCY_ID, 'unsupported_currency')
    total = body['price_minor'] * args['quantity']
    require(0 < total <= MAX_MINOR, 'money_overflow')
    require(total == args['total_price_minor'], 'price_changed')
    # Inventory is a query projection too: validate all Resource-backed orders
    # for this immutable listing before using its quantity/state index.
    from msg.market.order_resources import verify_source
    for (existing_id,) in tx.rows("SELECT order_id FROM order_contracts WHERE "
            "body::jsonb->>'listing_id'=? AND body::jsonb->>'resource_model'='1'", (listing.id,)):
        await verify_source(app, tx, existing_id)
    sold = tx.one('''SELECT COALESCE(SUM(quantity),0) FROM store_orders
        WHERE listing_id=? AND state NOT IN ('cancelled','refunded')''', (listing.id,))[0]
    require(sold + args['quantity'] <= body['quantity'], 'quantity_unavailable')
    require(listing.owner != buyer, 'self_purchase_forbidden')
    require(body['item_kind'] != 'secret' or body['delivery_mode'] == 'sealed_manual',
            'secret_requires_buyer_encryption')
    package_id = package_revision = package_digest = None
    if body['delivery_mode'] == 'managed_instant':
        require(body['item_kind'] in {'file','bundle','text'}, 'order_delivery_mode_unsupported')
        package = await _package_row(tx, body['package_ref'])
        require(package is not None and package[1] == listing.id and package[3] == listing.owner and
                package[5] == body['item_kind'] and package[10] == 'managed_instant',
                'package_not_available')
        package_id, package_revision, package_digest = package[0], package[4], package[8]
    recipient_key = None
    if body['delivery_mode'] == 'sealed_manual':
        row = tx.one('''SELECT key_id,recipient,public_key FROM encryption_subkeys
            WHERE subject=? AND is_primary=1 AND retired_at IS NULL''', (buyer,))
        require(row is not None, 'delivery_recipient_key_required')
        recipient_key = {'key_id': row[0], 'recipient': row[1], 'fingerprint': digest(row[2])}
    target = await target_for(app, tx, buyer, args.get('email'))
    order_id, now = _order_id(), wire(ctx.now)
    if version == 4:
        from msg.market.order_resources import begin_new
        begin_new(tx, order_id)
    escrow = 'esc_' + order_id[4:]
    tx.execute("INSERT INTO ledger_accounts(id,kind,subject_id,source_id) VALUES (?,'order_escrow',NULL,?)",
               (escrow, order_id), write=True)
    tx.execute('''INSERT INTO store_orders
        (id,buyer,seller,listing_id,listing_revision,package_id,package_revision,package_digest,
         quantity,unit_price_minor,total_price_minor,currency_id,escrow_subject,escrow_policy,
         dispute_policy,terms_digest,delivery_target,payment_intent_digest,payment_transaction_id,
         state,created_at,funded_at,receipt_refs)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'created',?,NULL,'[]')''',
        (order_id,buyer,listing.owner,listing.id,listing.revision,package_id,package_revision,
         package_digest,args['quantity'],body['price_minor'],total,CURRENCY_ID,escrow,
         body['escrow_policy'],body['dispute_policy'],digest(body['terms']),canonical(target).decode(),
         request.payload_digest,now), write=True)
    locked = {'order_id': order_id, 'version': version, 'buyer': buyer, 'seller': listing.owner,
        'listing': body, 'listing_id': listing.id, 'escrow_subject': escrow,
        'listing_revision': listing.revision,
        'listing_digest': revision.content.digest, 'package_id': package_id,
        'package_revision': package_revision, 'package_digest': package_digest,
        'quantity': args['quantity'], 'total_price_minor': total,
        'terms_digest': digest(body['terms']), 'policy': policy,
        'recipient_key': recipient_key, 'handle_snapshot': target['handle_snapshot'],
        'buyer_principal': wire(ctx.principal), 'created_at': now}
    if version == 4:
        locked['resource_model'] = 1
        locked['creation_request'] = wire(request)
        locked['settlement_policy'] = {'id': 'explicit-buyer-acceptance', 'version': 1}
    tx.execute('INSERT INTO order_contracts(order_id,body,digest) VALUES (?,?,?)',
               (order_id,canonical(locked).decode(),digest(locked)), write=True)
    tx.execute('INSERT INTO order_deadlines(order_id,expires_at) VALUES (?,?)',
        (order_id, wire(ctx.now+timedelta(seconds=policy['policy']['funding_timeout_seconds']))), write=True)
    from msg.market.email import verification_message
    order = _row(tx, order_id, buyer)
    await verification_message(app, tx, ctx, request, order)
    return order


async def fund(app, tx, ctx, request, order):
    require(order['state'] == 'created', 'order_not_fundable')
    locked = contract(tx, order['id'])
    require(locked['version'] != 5 or (request.operation == 'money.redeem' and
            request.contract_version == 3), 'entitlement_funding_contract_required')
    require(ctx.now < parse_time(order['created_at']) + timedelta(
        seconds=locked['policy']['policy']['funding_timeout_seconds']), 'payment_intent_expired')
    validate_target(tx, order)
    receipt = _post_transfer(tx, sender=order['buyer'], recipient=order['escrow_subject'],
        amount=order['total_price_minor'], actor=order['buyer'], request_id=request.request_id,
        now=ctx.now, receipt_signer=app.receipt_signer, reference='order_fund:'+order['id'])
    tx.execute('''UPDATE store_orders SET payment_transaction_id=?,payment_intent_digest=?,
        funded_at=?,receipt_refs=? WHERE id=?''', (receipt['body']['transaction_id'],
        request.payload_digest,wire(ctx.now),canonical([receipt['body']['transaction_id']]).decode(),
        order['id']), write=True)
    order.update(payment_transaction_id=receipt['body']['transaction_id'],
        payment_intent_digest=request.payload_digest, funded_at=wire(ctx.now),
        receipt_refs=[receipt['body']['transaction_id']])
    await transition(tx, order, 'funded', now=ctx.now, actor=order['buyer'],
                     request_id=request.request_id, reason='signed_payment_intent')
    tx.execute('UPDATE order_deadlines SET expires_at=? WHERE order_id=?',
        (wire(ctx.now+timedelta(seconds=locked['policy']['policy']['delivery_timeout_seconds'])), order['id']), write=True)
    if locked['version'] != 5 and locked['listing']['delivery_mode'] == 'managed_instant':
        from msg.market.delivery import automatic
        await automatic(app, tx, ctx, request, order)
    return receipt


def install(app, op):
    from msg.market.email import install as install_email
    install_email(app, op)

    @op('orders.create', obj(INTENT, INTENT_REQUIRED), signature=True)
    async def reserve(ctx, request, tx):
        order = await create(app, tx, ctx, request)
        return HandlerOutput(data={'order': view(tx, order, order['buyer'])})

    @op('orders.create', obj(INTENT, INTENT_REQUIRED), signature=True, version=2)
    async def reserve_explicit(ctx, request, tx):
        order = await create(app, tx, ctx, request, version=4)
        return HandlerOutput(data={'order': view(tx, order, order['buyer'])})

    @op('orders.buy', obj(INTENT, INTENT_REQUIRED), signature=True, version=3)
    async def buy(ctx, request, tx):
        order = await create(app, tx, ctx, request)
        receipt = await fund(app, tx, ctx, request, order)
        return HandlerOutput(data={'order': view(tx, _row(tx, order['id'], order['buyer']), order['buyer']),
                                   'payment': receipt})

    @op('orders.buy', obj(INTENT, INTENT_REQUIRED), signature=True, version=4)
    async def buy_explicit(ctx, request, tx):
        order = await create(app, tx, ctx, request, version=4)
        receipt = await fund(app, tx, ctx, request, order)
        return HandlerOutput(data={'order': view(tx, _row(tx, order['id'], order['buyer']), order['buyer']),
                                   'payment': receipt})

    @op('orders.fund', obj({'order_id': IDENTIFIER, 'order_digest': HASH,
        'total_price_minor': INTENT['total_price_minor'], 'currency_id': {'const': CURRENCY_ID}},
        ('order_id','order_digest','total_price_minor','currency_id')), signature=True)
    async def pay(ctx, request, tx):
        buyer = _subject(ctx)
        order = _row(tx, request.arguments['order_id'], buyer)
        require(order['buyer'] == buyer, 'order_not_found')
        locked = contract(tx, order['id'])
        require(digest(locked) == request.arguments['order_digest'] and
                order['total_price_minor'] == request.arguments['total_price_minor'],
                'payment_intent_mismatch')
        payment = await fund(app, tx, ctx, request, order)
        return HandlerOutput(data={'order': view(tx, _row(tx, order['id'], buyer), buyer),
                                   'payment': payment})

    @op('orders.cancel', ORDER, signature=True, version=2)
    async def cancel(ctx, request, tx):
        buyer = _subject(ctx)
        order = _row(tx, request.arguments['order_id'], buyer)
        require(order['buyer'] == buyer, 'order_not_found')
        if order['state'] == 'created':
            await transition(tx, order, 'cancelled', now=ctx.now, actor=buyer,
                             request_id=request.request_id, reason='buyer_cancelled')
        elif order['state'] not in {'cancelled', 'refunded'}:
            require(order['state'] == 'funded' and order['delivered_at'] is None,
                    'order_not_cancellable')
            await settle(app, tx, order, now=ctx.now, actor=buyer, request_id=request.request_id,
                         reason='buyer_cancelled', refund_minor=order['total_price_minor'])
        return HandlerOutput(data={'order': view(tx, order, buyer)})

    @op('orders.email_bind', obj({'order_id': IDENTIFIER, 'email': INTENT['email']},
        ('order_id','email')), signature=True)
    async def email_bind(ctx, request, tx):
        buyer = _subject(ctx)
        order = _row(tx, request.arguments['order_id'], buyer)
        require(order['buyer'] == buyer, 'order_not_found')
        contract(tx, order['id'])
        old = order['delivery_target'].get('email', {})
        require(order['state'] in {'created','funded','delivered','settled'}, 'order_closed')
        require(order['delivered_at'] is None or
                (old.get('endpoint_id') is None and
                 old.get('address_snapshot') == request.arguments['email']),
                'delivery_target_locked')
        target = await target_for(app, tx, buyer, request.arguments['email'])
        target['handle_snapshot'] = order['delivery_target']['handle_snapshot']
        require(target['email'].get('endpoint_id') is not None, 'email_not_verified')
        save_target(tx, order, target)
        validate_target(tx, order, email=True)
        if order['delivered_at']:
            await enqueue_notification(app, tx, ctx, request, order)
        return HandlerOutput(data={'order': view(tx, order, buyer)})

    @op('orders.contract', ORDER, effect='read')
    async def get_contract(ctx, request, tx):
        viewer = _viewer(ctx)
        order = _row(tx, request.arguments['order_id'], viewer)
        locked = contract(tx, order['id'])
        # Neither endpoint nor credential ceilings are part of seller projections.
        return HandlerOutput(data={'order': view(tx, order, viewer), 'contract': {
            k:v for k,v in locked.items() if k not in {'buyer_principal','handle_snapshot'}}})
