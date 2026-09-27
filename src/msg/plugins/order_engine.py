"""Finite order policies and ledger-backed transitions; not a signable agent."""
from datetime import timedelta

from msg.core.codec import canonical, digest, parse_time, wire
from msg.core.errors import require
from msg.plugins.money import _balance, _post_transfer

QUOTE_TTL = 900
DELIVERY_TTL = 86400
# escrow-v1 is retained for existing explicitly-accepted managed deliveries.
POLICIES = {
    'escrow-v1': ('managed_instant', False),
    'managed-instant-v1': ('managed_instant', True),
    'sealed-manual-v1': ('sealed_manual', False),
    'service-accept-v1': ('service', False),
}
TRANSITIONS = {
    'created': frozenset({'funded','cancelled'}),
    'funded': frozenset({'delivered','refunded'}),
    'delivered': frozenset({'accepted','disputed','refunded'}),
    'accepted': frozenset({'settled'}),
    'disputed': frozenset({'refunded'}),
    'settled': frozenset(), 'refunded': frozenset(), 'cancelled': frozenset(),
}


def policy(body):
    selected = POLICIES.get(body['escrow_policy'])
    require(selected is not None and selected[0] == body['delivery_mode'] and
            body['dispute_policy'] == 'dispute-v1', 'order_delivery_mode_unsupported')
    return selected


def deadline(row):
    return (parse_time(row['deadline_at']) if row['deadline_at'] else
            parse_time(row['funded_at'] or row['created_at']) + timedelta(seconds=DELIVERY_TTL))


def quote_digest(row):
    return digest({key:row[key] for key in ('id','buyer','seller','listing_id',
        'listing_revision','package_revision','package_digest','quantity','unit_price_minor',
        'total_price_minor','currency_id','escrow_policy','dispute_policy','terms_digest','delivery_mode')})


def record(tx,row,ctx,request,previous,target,reason):
    tx.execute('''INSERT INTO store_order_events
        (order_id,from_state,to_state,reason,actor,request_id,occurred_at,receipt_refs)
        VALUES (?,?,?,?,?,?,?,?)''',(row['id'],previous,target,reason,ctx.principal.actor,
        request.request_id,wire(ctx.now),canonical(row['receipt_refs']).decode()),write=True)


def transition(tx,row,target,ctx,request,reason):
    previous = row['state']
    require(target in TRANSITIONS.get(previous,()),'invalid_order_transition')
    changed = tx.execute('UPDATE store_orders SET state=? WHERE id=? AND state=?',
                         (target,row['id'],previous),write=True)
    require(changed.rowcount == 1,'order_state_conflict')
    row['state'] = target
    record(tx,row,ctx,request,previous,target,reason)


def refund(app,tx,row,ctx,request,reason):
    require(row['state'] in {'funded','delivered','disputed'},'order_not_refundable')
    require(_balance(tx,row['escrow_subject']) == row['total_price_minor'], 'escrow_balance_mismatch')
    receipt = _post_transfer(tx,sender=row['escrow_subject'],recipient=row['buyer'],
        amount=row['total_price_minor'],actor=ctx.principal.actor,request_id=request.request_id,
        now=ctx.now,receipt_signer=app.receipt_signer,kind='refund',
        reference='order_refund:'+row['id'],entry_key='order_refund')
    row['receipt_refs'] = [*row['receipt_refs'],receipt['body']['transaction_id']]
    tx.execute('UPDATE store_orders SET receipt_refs=? WHERE id=?',
               (canonical(row['receipt_refs']).decode(),row['id']),write=True)
    transition(tx,row,'refunded',ctx,request,reason)
    return receipt
