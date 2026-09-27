"""Signed price snapshots, private orders and finite escrow transitions."""
from __future__ import annotations

import base64
import os
from datetime import timedelta

from msg.core.codec import canonical, digest, loads, parse_time, wire
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import registration
from msg.plugins.money import account_requirements, CURRENCY_ID, MAX_MINOR, _balance, _post_transfer
from msg.plugins.schemas import IDENTIFIER, obj
from msg.plugins.store import _body, _listing, _package_row
from msg.plugins import order_engine as engine


_COLUMNS = ('id', 'buyer', 'seller', 'listing_id', 'listing_revision',
            'package_id', 'package_revision', 'package_digest', 'quantity',
            'unit_price_minor', 'total_price_minor', 'currency_id',
            'escrow_subject', 'escrow_policy', 'dispute_policy', 'terms_digest',
            'delivery_target', 'payment_intent_digest', 'payment_transaction_id',
            'state', 'created_at', 'funded_at', 'delivered_at', 'settled_at',
            'receipt_refs', 'delivery_mode', 'deadline_at', 'quote_digest')


def _subject(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject and
            ctx.principal.method == 'signature', 'signature_required')
    return subject


def _viewer(ctx):
    subject = ctx.principal.subject
    require(subject is not None and ctx.principal.actor == subject,
            'order_not_found')
    return subject


def _order_id():
    # 160 random bits, unguessable even if an attacker sees other order IDs.
    return 'ord_' + base64.b32encode(os.urandom(20)).decode('ascii').rstrip('=').lower()


def _row(tx, order_id, viewer):
    row = tx.one('''SELECT id,buyer,seller,listing_id,listing_revision,
        package_id,package_revision,package_digest,quantity,unit_price_minor,
        total_price_minor,currency_id,escrow_subject,escrow_policy,
        dispute_policy,terms_digest,delivery_target,payment_intent_digest,
        payment_transaction_id,state,created_at,funded_at,delivered_at,
        settled_at,receipt_refs,delivery_mode,deadline_at,quote_digest FROM store_orders WHERE id=?''', (order_id,))
    # A valid ID is not an access grant. Keep nonexistent and unauthorized alike.
    require(row is not None and viewer in row[1:3], 'order_not_found')
    result = dict(zip(_COLUMNS, row))
    result['delivery_target'] = loads(result['delivery_target'])
    result['receipt_refs'] = loads(result['receipt_refs'])
    expected_digest = engine.quote_digest(result)
    require(result['quote_digest'] is None or result['quote_digest'] == expected_digest,
            'order_snapshot_mismatch')
    result['quote_digest'] = expected_digest
    return result


def _view(row, viewer):
    keys = ('id', 'buyer', 'seller', 'listing_id', 'listing_revision',
            'package_revision', 'package_digest', 'quantity',
            'unit_price_minor', 'total_price_minor', 'currency_id',
            'escrow_policy', 'dispute_policy', 'terms_digest', 'state',
            'created_at', 'funded_at', 'delivered_at', 'settled_at',
            'delivery_mode', 'deadline_at', 'quote_digest')
    result = {key: row[key] for key in keys}
    result['payment_status'] = ('cancelled' if row['state'] == 'cancelled' else
                                'refunded' if row['state'] == 'refunded' else
                                'settled' if row['state'] == 'settled' else
                                'funded' if row['payment_transaction_id'] else
                                'pending')
    result['delivery_channel'] = row['delivery_target']['channel']
    if viewer == row['buyer']:
        result['delivery_target'] = row['delivery_target']
        result['payment_intent_digest'] = row['payment_intent_digest']
        result['receipt_refs'] = row['receipt_refs']
    return result


async def _fund_order(app,tx,row,ctx,request):
    require(row['state']=='created','order_not_payable')
    require(engine.deadline(row)>ctx.now,'order_quote_expired')
    listing = await _listing(app,ctx,request,tx,row['listing_id'])
    current,_ = await _body(app,tx,listing)
    require(current['state']=='active' and (current['expires_at'] is None or
            parse_time(current['expires_at'])>ctx.now),'listing_not_available')
    sold = int(tx.one("""SELECT COALESCE(SUM(quantity),0) FROM store_orders
        WHERE listing_id=? AND state NOT IN ('created','cancelled','refunded')""",(listing.id,))[0])
    require(sold+row['quantity']<=current['quantity'],'quantity_unavailable')
    # The order snapshot, not a later seller edit, determines the charged price.
    receipt = _post_transfer(tx,sender=row['buyer'],recipient=row['escrow_subject'],
        amount=row['total_price_minor'],actor=row['buyer'],request_id=request.request_id,
        now=ctx.now,receipt_signer=app.receipt_signer,reference='order_fund:'+row['id'],entry_key='order_fund')
    row['receipt_refs'] = [receipt['body']['transaction_id']]
    tx.execute('''UPDATE store_orders SET payment_transaction_id=?,payment_intent_digest=?,
        funded_at=?,deadline_at=?,receipt_refs=? WHERE id=?''',
        (receipt['body']['transaction_id'],request.payload_digest,wire(ctx.now),
         wire(ctx.now+timedelta(seconds=engine.DELIVERY_TTL)),canonical(row['receipt_refs']).decode(),row['id']),write=True)
    engine.transition(tx,row,'funded',ctx,request,'signed_payment')
    data = {'payment':receipt}
    if row['escrow_policy']=='managed-instant-v1':
        from msg.plugins.delivery import prepare_order, accept_order
        prepared = await prepare_order(app,tx,_row(tx,row['id'],row['buyer']),ctx,request)
        settled = await accept_order(app,tx,_row(tx,row['id'],row['buyer']),ctx,request,
                                     prepared['delivery_digest'],automatic=True)
        data.update(delivery=prepared,settlement=settled['receipt'])
    return data


def install(app):
    op, finish = registration(app, 'orders', ('store', 'money'))
    amount = {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR}
    quantity = {'type': 'integer', 'minimum': 1, 'maximum': 10**9}

    quote_schema = obj({'listing_id': IDENTIFIER,
        'listing_revision': IDENTIFIER, 'quantity': quantity,
        'currency_id': {'const': CURRENCY_ID}, 'total_price_minor': amount},
        ('listing_id', 'listing_revision', 'quantity', 'currency_id','total_price_minor'))

    @op('orders.create', quote_schema, signature=True, requirements=account_requirements)
    @op('orders.buy', quote_schema, signature=True, requirements=account_requirements)
    async def buy(ctx, request, tx):
        buyer = _subject(ctx)
        args = request.arguments
        listing = await _listing(app, ctx, request, tx, args['listing_id'])
        body, _ = await _body(app, tx, listing)
        require(body['mode'] == 'sale' and body['state'] == 'active' and
                (body['expires_at'] is None or
                 parse_time(body['expires_at']) > ctx.now), 'listing_not_available')
        engine.policy(body)
        require(body['item_kind'] in ({'service'} if body['delivery_mode']=='service' else
                {'file','bundle'} if body['delivery_mode']=='managed_instant' else {'file','bundle','text','secret'}),
                'order_delivery_mode_unsupported')
        require(args['listing_revision'] == listing.revision,
                'listing_revision_conflict')
        require(body['currency_id'] == args['currency_id'] == CURRENCY_ID,
                'unsupported_currency')
        requested = args['quantity']
        require(requested <= body['quantity'], 'quantity_unavailable')
        sold = tx.one('''SELECT COALESCE(SUM(quantity),0) FROM store_orders
            WHERE listing_id=? AND state NOT IN ('created','cancelled','refunded')''',
            (listing.id,))[0]
        require(int(sold) + requested <= body['quantity'], 'quantity_unavailable')
        total = body['price_minor'] * requested
        require(0 < total <= MAX_MINOR, 'money_overflow')
        require(args['total_price_minor'] == total, 'price_changed')
        package_id = body['package_ref']
        package_revision = package_digest = None
        if body['delivery_mode'] == 'managed_instant':
            # The package remains private; checkout sees only its locked digest.
            package = await _package_row(tx, package_id) if package_id else None
            require(package is not None and package[1] == listing.id and
                    package[3] == listing.owner and
                    package[5] == body['item_kind'] and
                    package[10] == body['delivery_mode'], 'package_not_available')
            require(0 <= package[9] <= 1024 * 1024,
                    'delivery_package_too_large')
            package_revision, package_digest = package[4], package[8]
        # The only currently supported target is the buyer's in-site order
        # collection. Verified email and encryption endpoints need Delivery.
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
        tx.execute('''INSERT INTO ledger_accounts(id,kind,subject_id,source_id)
            VALUES (?,'order_escrow',NULL,?)''', (escrow,order_id), write=True)
        for sid in (buyer,seller):
            subject = await tx.subject(sid)
            require(subject.kind in {'registered','custodial'} and not subject.local_only,
                    'money_subject_required')
        tx.execute('''INSERT INTO store_orders
            (id,buyer,seller,listing_id,listing_revision,package_id,
             package_revision,package_digest,quantity,unit_price_minor,
             total_price_minor,currency_id,escrow_subject,escrow_policy,
             dispute_policy,terms_digest,delivery_target,payment_intent_digest,
             payment_transaction_id,state,created_at,funded_at,delivered_at,
             settled_at,receipt_refs,delivery_mode,deadline_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (order_id,buyer,seller,listing.id,listing.revision,package_id,
             package_revision,package_digest,requested,body['price_minor'],
             total,CURRENCY_ID,escrow,body['escrow_policy'],
             body['dispute_policy'],digest(body['terms']),
             canonical(target).decode(),request.payload_digest,None,
             'created',wire(ctx.now),None,None,None,'[]',body['delivery_mode'],
             wire(ctx.now+timedelta(seconds=engine.QUOTE_TTL))), write=True)
        row = _row(tx,order_id,buyer)
        tx.execute('UPDATE store_orders SET quote_digest=? WHERE id=?',(row['quote_digest'],order_id),write=True)
        engine.record(tx,row,ctx,request,None,'created','quote_created')
        data = {}
        if request.operation == 'orders.buy':
            data = await _fund_order(app,tx,row,ctx,request)
        return HandlerOutput(data={'order':_view(_row(tx,order_id,buyer),buyer),**data})

    @op('orders.cancel', obj({'order_id': IDENTIFIER}, ('order_id',)),
        signature=True, requirements=account_requirements)
    async def cancel(ctx, request, tx):
        buyer = _subject(ctx)
        row = _row(tx, request.arguments['order_id'], buyer)
        require(row['buyer'] == buyer, 'order_not_found')
        if row['state'] == 'created':
            engine.transition(tx,row,'cancelled',ctx,request,'buyer_cancelled_unpaid')
            refund = None
        else:
            require(row['state'] == 'funded' and row['delivered_at'] is None,
                    'order_not_cancellable')
            require(tx.one('SELECT 1 FROM store_deliveries WHERE order_id=?',(row['id'],)) is None,
                    'order_not_cancellable')
            refund = engine.refund(app,tx,row,ctx,request,'buyer_cancelled')
        return HandlerOutput(data={'order':_view(_row(tx,row['id'],buyer),buyer),'refund':refund})

    @op('orders.pay', obj({'order_id':IDENTIFIER,'quote_digest':IDENTIFIER,
        'currency_id':{'const':CURRENCY_ID},'total_price_minor':amount},
        ('order_id','quote_digest','currency_id','total_price_minor')), signature=True, requirements=account_requirements)
    async def pay(ctx,request,tx):
        buyer = _subject(ctx)
        row = _row(tx,request.arguments['order_id'],buyer)
        require(row['buyer']==buyer,'order_not_found')
        require(request.arguments['quote_digest']==row['quote_digest'] and
                request.arguments['total_price_minor']==row['total_price_minor'],'order_quote_mismatch')
        data = await _fund_order(app,tx,row,ctx,request)
        return HandlerOutput(data={'order':_view(_row(tx,row['id'],buyer),buyer),**data})

    @op('orders.resolve',obj({'order_id':IDENTIFIER},('order_id',)),signature=True, requirements=account_requirements)
    async def resolve(ctx,request,tx):
        buyer = _subject(ctx)
        row = _row(tx,request.arguments['order_id'],buyer)
        require(row['buyer']==buyer,'order_not_found')
        if row['state']=='created':
            require(engine.deadline(row)<=ctx.now,'no_objective_refund')
            engine.transition(tx,row,'cancelled',ctx,request,'quote_expired')
            receipt = None
        else:
            require(row['state'] in {'funded','delivered','disputed'},'order_not_refundable')
            from msg.plugins.delivery import objective_fault
            reason = await objective_fault(app,tx,row,ctx.now)
            require(reason is not None,'no_objective_refund')
            receipt = engine.refund(app,tx,row,ctx,request,reason)
        return HandlerOutput(data={'order':_view(_row(tx,row['id'],buyer),buyer),'refund':receipt})

    @op('orders.refund',obj({'order_id':IDENTIFIER},('order_id',)),signature=True, requirements=account_requirements)
    async def seller_refund(ctx,request,tx):
        seller = _subject(ctx)
        row = _row(tx,request.arguments['order_id'],seller)
        require(row['seller']==seller,'order_not_found')
        receipt = engine.refund(app,tx,row,ctx,request,'seller_refund')
        return HandlerOutput(data={'order':_view(_row(tx,row['id'],seller),seller),'refund':receipt})

    @op('orders.dispute',obj({'order_id':IDENTIFIER},('order_id',)),signature=True, requirements=account_requirements)
    async def dispute(ctx,request,tx):
        buyer = _subject(ctx)
        row = _row(tx,request.arguments['order_id'],buyer)
        require(row['buyer']==buyer,'order_not_found')
        require(row['state']=='delivered','order_not_disputable')
        engine.transition(tx,row,'disputed',ctx,request,'buyer_disputed')
        return HandlerOutput(data={'order':_view(_row(tx,row['id'],buyer),buyer)})

    @op('orders.history',obj({'order_id':IDENTIFIER},('order_id',)),effect='read', requirements=account_requirements)
    async def history(ctx,request,tx):
        row = _row(tx,request.arguments['order_id'],_viewer(ctx))
        items = tx.rows('''SELECT seq,from_state,to_state,reason,actor,request_id,occurred_at,receipt_refs
            FROM store_order_events WHERE order_id=? ORDER BY seq''',(row['id'],))
        return HandlerOutput(data={'events':[dict(zip(('seq','from_state','to_state','reason','actor',
            'request_id','occurred_at','receipt_refs'),(*item[:-1],loads(item[-1])))) for item in items]})

    @op('orders.recipient_key',obj({'order_id':IDENTIFIER},('order_id',)),effect='read', requirements=account_requirements)
    async def recipient_key(ctx,request,tx):
        row = _row(tx,request.arguments['order_id'],_viewer(ctx))
        key = tx.one('''SELECT key_id,recipient FROM encryption_subkeys WHERE subject=?
            AND is_primary=1 AND retired_at IS NULL''',(row['buyer'],))
        require(key is not None,'recipient_key_unavailable')
        return HandlerOutput(data={'subject_id':row['buyer'],'key_id':key[0],'recipient':key[1]})

    @op('orders.get', obj({'order_id': IDENTIFIER}, ('order_id',)), effect='read', requirements=account_requirements)
    async def get(ctx, request, tx):
        viewer = _viewer(ctx)
        return HandlerOutput(data={'order': _view(_row(tx, request.arguments['order_id'],
                                                       viewer), viewer)})

    @op('orders.list', obj({'role': {'enum': ['buy', 'sell']},
        'status': {'enum': ['open', 'completed', 'disputed']},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}), effect='read', requirements=account_requirements)
    async def list_orders(ctx, request, tx):
        viewer = _viewer(ctx)
        args = request.arguments
        role = args.get('role')
        status = args.get('status')
        limit = args.get('limit', 50)
        where = '(buyer=? OR seller=?)'
        values = [viewer, viewer]
        if role:
            where = ('buyer=?' if role == 'buy' else 'seller=?')
            values = [viewer]
        if status:
            where += (' AND state IN (\'created\',\'funded\',\'delivered\',\'accepted\')'
                      if status == 'open' else
                      ' AND state IN (\'settled\',\'cancelled\',\'refunded\')'
                      if status == 'completed' else
                      " AND state='disputed'")
        rows = tx.rows('SELECT id FROM store_orders WHERE ' + where +
                       ' ORDER BY created_at DESC,id DESC LIMIT ?',
                       (*values,limit))
        return HandlerOutput(data={'orders': [_view(_row(tx, id, viewer), viewer)
                                              for (id,) in rows]})

    @op('orders.payment', obj({'order_id': IDENTIFIER}, ('order_id',)),
        effect='read', requirements=account_requirements)
    async def payment(ctx, request, tx):
        viewer = _viewer(ctx)
        row = _row(tx, request.arguments['order_id'], viewer)
        result = {'order_id': row['id'],
                  'status': _view(row,viewer)['payment_status'],
                  'amount_minor': row['total_price_minor'],
                  'currency_id': row['currency_id']}
        if viewer == row['buyer']:
            receipts = []
            for transaction_id in row['receipt_refs']:
                ledger = tx.one('SELECT receipt FROM money_ledger WHERE id=?',
                                (transaction_id,))
                require(ledger is not None, 'order_not_found')
                receipts.append(loads(ledger[0]))
            result['receipt'] = receipts[0] if receipts else None
            result['receipts'] = receipts
            result['escrow_balance_minor'] = _balance(tx, row['escrow_subject'])
        return HandlerOutput(data={'payment': result})

    finish()
