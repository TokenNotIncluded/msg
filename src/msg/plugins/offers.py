"""Versioned resource offers and atomic, owner-bound purchases.

Only installed quota consumers are sellable. Purchases lock the quote; neither
an offer edit nor a bank role can change the entitlement or bypass permission.
"""
from datetime import timedelta

from msg.constants import ROOT_SUBJECT
from msg.core.codec import canonical, digest, parse_time, wire
from msg.market.compatibility import read_purchase as _purchase
from msg.core.errors import Failure, require
from msg.core.models import HandlerOutput
from msg.plugins.common import new_id, registration
from msg.plugins.hosting_capacity import ENTITLEMENT_KIND, MAX_CAPACITY_BYTES, extra_capacity
from msg.plugins.money import account_requirements, CURRENCY_ID, MAX_MINOR, _owner, _balance, _post_transfer
from msg.plugins.schemas import IDENTIFIER, obj

PURCHASE_TTL = 900
FORBIDDEN_KINDS = frozenset({'user','organization','certificate','csr','delegation',
    'keystore','identity','capability','admin','system','priority','bank_role'})


def _grant_hosting(app, tx, purchase, now, transaction_id):
    snapshot = purchase['offer_snapshot']
    require(extra_capacity(tx, purchase['subject_id'], now) + purchase['quantity'] +
            app.settings.hosting_base_capacity_bytes <= MAX_CAPACITY_BYTES,
            'entitlement_capacity_exceeded')
    expiry = (wire(now + timedelta(seconds=snapshot['duration_seconds']))
              if snapshot['duration_seconds'] is not None else None)
    eid = 'ent_' + digest((purchase['subject_id'], purchase['request_id']))[7:39]
    tx.execute('''INSERT INTO resource_entitlements
        (id,subject_id,offer_id,purchase_request_id,quantity,entitlement_kind,
         granted_at,expires_at,redeem_transaction_id) VALUES (?,?,?,?,?,?,?,?,?)''',
        (eid,purchase['subject_id'],snapshot['offer_id'],purchase['request_id'],
         purchase['quantity'],snapshot['entitlement_kind'],wire(now),expiry,transaction_id),write=True)
    return eid


# Finite, trusted code registrations. No evaluator names or code from config.
ENTITLEMENT_FULFILLERS = {ENTITLEMENT_KIND: ('website', 'byte', 1, _grant_hosting)}


def purchasable(app, resource_kind: str, entitlement_kind: str) -> bool:
    if resource_kind in FORBIDDEN_KINDS:
        return False
    provider = ENTITLEMENT_FULFILLERS.get(entitlement_kind)
    try:
        spec = app.registry.resource_type(resource_kind, 1)
        app.registry.operation('hosting.deploy', 1)
    except Failure:
        return False
    return bool(spec.purchasable and provider and provider[0] == resource_kind)


def validate_local_offer(app, *, resource_kind: str, entitlement_kind: str,
                         unit: str, price_minor: int, min_quantity: int,
                         max_quantity: int, duration_seconds: int | None = None):
    require(purchasable(app, resource_kind, entitlement_kind), 'resource_not_purchasable')
    require(unit == ENTITLEMENT_FULFILLERS[entitlement_kind][1], 'invalid_offer_unit')
    require(type(price_minor) is int and 0 < price_minor <= MAX_MINOR, 'invalid_offer_price')
    require(type(min_quantity) is int and type(max_quantity) is int and
            0 < min_quantity <= max_quantity <= MAX_CAPACITY_BYTES and
            price_minor * max_quantity <= MAX_MINOR, 'invalid_offer_quantity')
    require(duration_seconds is None or
            (type(duration_seconds) is int and 0 < duration_seconds <= 315360000),
            'invalid_offer_duration')


def _valid_catalog_offer(app, row):
    """Apply current issuer bounds to old rows without rewriting signed history."""
    try:
        validate_local_offer(app, resource_kind=row[1], unit=row[2], price_minor=row[3],
                             min_quantity=row[4], max_quantity=row[5],
                             entitlement_kind=row[6], duration_seconds=row[7])
    except Failure:
        return False
    return True


def _public_offer(row):
    return dict(zip(('offer_id','resource_kind','unit','price_minor','min_quantity',
                     'max_quantity','entitlement_kind','duration_seconds','price_revision'),row)) | {
        'currency_id': CURRENCY_ID, 'provider_version': 1}


def _finalize(app, tx, purchase, now, request_id, *, cancel=False):
    require(purchase['state'] == 'pending', 'purchase_not_pending')
    snapshot = purchase['offer_snapshot']
    provider = ENTITLEMENT_FULFILLERS.get(snapshot['entitlement_kind'])
    reason = ('cancelled' if cancel else 'expired' if parse_time(purchase['expires_at']) <= now
              else 'provider_unavailable' if not purchasable(app,snapshot['resource_kind'],snapshot['entitlement_kind'])
              or provider[2] != snapshot['provider_version'] else None)
    require(_balance(tx,purchase['escrow_account']) == purchase['total_minor'], 'escrow_balance_mismatch')
    from msg.market.ledger import post_escrow_release
    receipt = post_escrow_release(tx,escrow_account=purchase['escrow_account'],
        source_id=purchase['id'], account_kind='purchase_escrow',
        buyer=purchase['subject_id'], seller=ROOT_SUBJECT,
        recipient=purchase['subject_id'] if reason else ROOT_SUBJECT,
        amount=purchase['total_minor'],actor=purchase['subject_id'],request_id=request_id,
        now=now,receipt_signer=app.receipt_signer,kind='refund' if reason else 'redeem',
        reference='purchase_' + ('refund:' if reason else 'settle:') + purchase['id'],
        entry_key='purchase_final')
    tid = receipt['body']['transaction_id']
    eid = None if reason else provider[3](app,tx,purchase,now,tid)
    tx.execute('''UPDATE money_purchases SET state=?,final_transaction_id=?,entitlement_id=?,reason=?
        WHERE id=? AND state='pending' ''',
        ('refunded' if reason else 'settled',tid,eid,reason,purchase['id']),write=True)
    return receipt


def install(app):
    op,finish = registration(app,'offers',('money',))

    @op('money.offers',obj(),effect='read', version=2)
    @op('money.offers',obj(),effect='read')
    async def offers(ctx,request,tx):
        rows = tx.rows('''SELECT offer_id,resource_kind,unit,price_minor,min_quantity,
            max_quantity,entitlement_kind,duration_seconds,price_revision
            FROM server_offers WHERE enabled=TRUE ORDER BY offer_id''')
        from msg.market.offer_resources import verify_projection
        public = []
        for row in rows:
            if _valid_catalog_offer(app, row):
                await verify_projection(app, tx, row)
                public.append(_public_offer(row))
        data = {'currency_id': CURRENCY_ID, 'offers': public}
        if request.contract_version == 2:
            from msg.market.compatibility import offer_listing
            data['listings'] = [offer_listing(offer) for offer in public]
        return HandlerOutput(data=data)

    @op('money.redeem', obj({'offer_id': IDENTIFIER,
        'quantity': {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR},
        'currency_id': {'const': CURRENCY_ID}, 'price_revision': IDENTIFIER,
        'listing_revision': IDENTIFIER,
        'offer_snapshot_digest': {'type': 'string', 'pattern': '^sha256:[a-f0-9]{64}$'},
        'total_price_minor': {'type': 'integer', 'minimum': 1, 'maximum': MAX_MINOR},
        'settlement_policy': {'const': 'deterministic-entitlement-v1'}},
        ('offer_id', 'quantity', 'currency_id', 'price_revision', 'listing_revision',
         'offer_snapshot_digest', 'total_price_minor', 'settlement_policy')),
        signature=True, version=3, requirements=account_requirements)
    async def redeem_order(ctx, request, tx):
        owner, args = _owner(ctx), request.arguments
        row = tx.one("""SELECT offer_id,resource_kind,unit,price_minor,min_quantity,
            max_quantity,entitlement_kind,duration_seconds,price_revision FROM server_offers
            WHERE offer_id=? AND enabled=TRUE""", (args['offer_id'],))
        require(row is not None and _valid_catalog_offer(app, row), 'offer_not_found')
        from msg.market.offer_resources import verify_projection
        require(await verify_projection(app, tx, row) is not None, 'offer_requires_import')
        require(args['price_revision'] == row[8], 'offer_price_changed')
        require(row[4] <= args['quantity'] <= row[5] and row[3] * args['quantity'] <= MAX_MINOR,
                'invalid_offer_quantity')
        require(extra_capacity(tx, owner, ctx.now) + args['quantity'] + app.settings.hosting_base_capacity_bytes
                <= MAX_CAPACITY_BYTES, 'entitlement_capacity_exceeded')
        require(ENTITLEMENT_FULFILLERS[row[6]][2] == 1, 'offer_provider_unavailable')
        from msg.market.entitlement_orders import redeem as redeem_into_order
        result = await redeem_into_order(app, tx, ctx, request, _public_offer(row),
                                          ENTITLEMENT_FULFILLERS[row[6]][3])
        return HandlerOutput(data=result)

    @op('money.redeem',obj({'offer_id':IDENTIFIER,'quantity':{'type':'integer','minimum':1,'maximum':MAX_MINOR},
        'currency_id':{'const':CURRENCY_ID},'price_revision':IDENTIFIER},
        ('offer_id','quantity','currency_id','price_revision')),signature=True, requirements=account_requirements)
    @op('money.redeem',obj({'offer_id':IDENTIFIER,'quantity':{'type':'integer','minimum':1,'maximum':MAX_MINOR},
        'currency_id':{'const':CURRENCY_ID},'price_revision':IDENTIFIER,'defer':{'type':'boolean'}},
        ('offer_id','quantity','currency_id','price_revision')),signature=True, version=2, requirements=account_requirements)
    async def redeem(ctx,request,tx):
        owner = _owner(ctx)
        args = request.arguments
        row = tx.one('''SELECT offer_id,resource_kind,unit,price_minor,min_quantity,
            max_quantity,entitlement_kind,duration_seconds,price_revision FROM server_offers
            WHERE offer_id=? AND enabled=TRUE''',(args['offer_id'],))
        require(row is not None and _valid_catalog_offer(app, row),'offer_not_found')
        from msg.market.offer_resources import verify_projection
        await verify_projection(app, tx, row)
        require(args['price_revision'] == row[8],'offer_price_changed')
        require(row[4] <= args['quantity'] <= row[5] and row[3]*args['quantity'] <= MAX_MINOR,
                'invalid_offer_quantity')
        require(extra_capacity(tx,owner,ctx.now) + args['quantity'] + app.settings.hosting_base_capacity_bytes
                <= MAX_CAPACITY_BYTES,'entitlement_capacity_exceeded')
        purchase_id = new_id('pur')
        escrow = new_id('esc')
        tx.execute("INSERT INTO ledger_accounts(id,kind,subject_id,source_id) VALUES (?,'purchase_escrow',NULL,?)",
                   (escrow,purchase_id),write=True)
        funding = _post_transfer(tx,sender=owner,recipient=escrow,amount=row[3]*args['quantity'],
            actor=owner,request_id=request.request_id,now=ctx.now,receipt_signer=app.receipt_signer,
            reference='purchase_fund:'+purchase_id,entry_key='purchase_fund')
        tx.execute('''INSERT INTO money_purchases
            (id,subject_id,request_id,offer_snapshot,quantity,total_minor,escrow_account,state,
             created_at,expires_at,funding_transaction_id) VALUES (?,?,?,?,?,?,?,'pending',?,?,?)''',
            (purchase_id,owner,request.request_id,canonical(_public_offer(row)).decode(),args['quantity'],
             row[3]*args['quantity'],escrow,wire(ctx.now),wire(ctx.now+timedelta(seconds=PURCHASE_TTL)),
             funding['body']['transaction_id']),write=True)
        settlement = None
        if not args.get('defer',False):
            settlement = _finalize(app,tx,_purchase(tx,purchase_id,owner),ctx.now,request.request_id)
        return HandlerOutput(data={'purchase':_purchase(tx,purchase_id,owner),
                                   'funding':funding,'settlement':settlement})

    @op('money.purchase_get',obj({'purchase_id':IDENTIFIER},('purchase_id',)),effect='read', version=2, requirements=account_requirements)
    @op('money.purchase_get',obj({'purchase_id':IDENTIFIER},('purchase_id',)),effect='read', requirements=account_requirements)
    async def get(ctx,request,tx):
        purchase = _purchase(tx,request.arguments['purchase_id'],_owner(ctx))
        data = {'purchase': purchase}
        if request.contract_version == 2:
            from msg.market.compatibility import purchase_order
            data['order'] = purchase_order(tx, purchase)
        return HandlerOutput(data=data)

    @op('money.purchase_settle',obj({'purchase_id':IDENTIFIER},('purchase_id',)),signature=True, requirements=account_requirements)
    async def settle(ctx,request,tx):
        owner = _owner(ctx)
        purchase = _purchase(tx,request.arguments['purchase_id'],owner)
        receipt = _finalize(app,tx,purchase,ctx.now,request.request_id)
        return HandlerOutput(data={'purchase':_purchase(tx,purchase['id'],owner),'settlement':receipt})

    @op('money.purchase_cancel',obj({'purchase_id':IDENTIFIER},('purchase_id',)),signature=True, requirements=account_requirements)
    async def cancel(ctx,request,tx):
        owner = _owner(ctx)
        purchase = _purchase(tx,request.arguments['purchase_id'],owner)
        receipt = _finalize(app,tx,purchase,ctx.now,request.request_id,cancel=True)
        return HandlerOutput(data={'purchase':_purchase(tx,purchase['id'],owner),'refund':receipt})

    @op('money.entitlements',obj(),effect='read', requirements=account_requirements)
    async def entitlements(ctx,request,tx):
        owner = _owner(ctx)
        rows = tx.rows('''SELECT id,offer_id,quantity,entitlement_kind,granted_at,expires_at,
            redeem_transaction_id FROM resource_entitlements WHERE subject_id=? ORDER BY id''',(owner,))
        return HandlerOutput(data={'entitlements':[dict(zip(('id','offer_id','quantity','entitlement_kind',
            'granted_at','expires_at','transaction_id'),row)) for row in rows],
            'hosting_extra_bytes':extra_capacity(tx,owner,ctx.now)})
    finish()
