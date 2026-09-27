"""One escrow release entry point, guarded transitions and append-only facts."""
from __future__ import annotations

from datetime import timedelta

from msg.core.codec import canonical, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import Event
from msg.market.policy import contract
from msg.plugins.common import new_id
from msg.plugins.money import _balance, _post_transfer
from msg.plugins.orders import _row

# Not a credential, account or public capability. Only trusted in-process market
# code can reach this token; ordinary signed money.transfer never receives it.
_ESCROW_WRITE = object()
TRANSITIONS = {
    'created': {'funded', 'cancelled'},
    'funded': {'delivered', 'refunded', 'disputed'},
    'delivered': {'accepted', 'refunded', 'disputed'},
    'accepted': {'settled'},
    'disputed': {'refunded', 'settled'},
    'cancelled': set(), 'refunded': set(), 'settled': set(),
}
OBJECTIVE_REFUNDS = frozenset({'package_missing', 'package_digest_mismatch',
    'delivery_missing', 'delivery_digest_mismatch', 'delivery_timeout', 'buyer_cancelled'})


async def transition(tx, order, state, *, now, actor, request_id, reason):
    require(state in TRANSITIONS.get(order['state'], ()), 'order_transition_invalid')
    changed = tx.execute('UPDATE store_orders SET state=? WHERE id=? AND state=?',
                         (state, order['id'], order['state']), write=True)
    require(changed.rowcount == 1, 'order_transition_conflict')
    fact = {'order_id': order['id'], 'from': order['state'], 'to': state,
            'at': wire(now), 'reason': reason, 'policy': order['dispute_policy'],
            'actor': actor, 'request_id': request_id}
    tx.execute('INSERT INTO order_transitions(id,order_id,body) VALUES (?,?,?)',
               (new_id('ot'), order['id'], canonical(fact).decode()), write=True)
    # Events deliberately contain no buyer handle, evidence, goods or address.
    await tx.append_event(Event(id=new_id('ev'), type='orders.' + state, time=now,
        actor=actor, subject=order['buyer'], request_id=request_id,
        resources=(), data={'order_id': order['id'], 'state': state}))
    order['state'] = state


async def settle(app, tx, order, *, now, actor, request_id, reason,
                 refund_minor=0, decision=None):
    """Validate authority *before* appending either leg of a balanced settlement.

    Caller owns the transaction. Any failure rolls back both legs, the decision
    consumption, transitions, receipts, events and request result together.
    """
    total = order['total_price_minor']
    require(type(refund_minor) is int and 0 <= refund_minor <= total,
            'settlement_amount_invalid')
    existing = tx.one('SELECT body FROM order_settlements WHERE order_id=?', (order['id'],))
    if existing:
        body = loads(existing[0])
        require(body['refund_minor'] == refund_minor and body['reason'] == reason and
                body['decision_id'] == (decision['id'] if decision else None),
                'order_already_settled')
        return body['receipts']
    if decision is not None:
        from msg.market.arbitration import validate_decision
        await validate_decision(app, tx, order, decision, now)
        require(refund_minor == decision['refund_minor'], 'decision_amount_mismatch')
    elif refund_minor == total:
        require(reason in OBJECTIVE_REFUNDS and order['state'] in {'funded', 'delivered'},
                'escrow_release_forbidden')
    else:
        require(refund_minor == 0 and order['state'] == 'accepted' and
                reason in {'buyer_acceptance', 'managed_instant_verified'},
                'escrow_release_forbidden')
    require(_balance(tx, order['escrow_subject']) == total, 'escrow_balance_mismatch')
    receipts = []
    for suffix, recipient, amount, kind in (
        ('refund', order['buyer'], refund_minor, 'refund'),
        ('release', order['seller'], total-refund_minor, 'transfer'),
    ):
        if amount:
            receipts.append(_post_transfer(tx, sender=order['escrow_subject'],
                recipient=recipient, amount=amount, actor=actor,
                request_id=f'{order["id"]}:{suffix}', now=now,
                receipt_signer=app.receipt_signer, reference=f'order_{suffix}:{order["id"]}',
                kind=kind, escrow_authority=_ESCROW_WRITE))
    final = 'refunded' if refund_minor == total else 'settled'
    await transition(tx, order, final, now=now, actor=actor, request_id=request_id, reason=reason)
    refs = order['receipt_refs'] + [r['body']['transaction_id'] for r in receipts]
    tx.execute('UPDATE store_orders SET settled_at=?,receipt_refs=? WHERE id=?',
               (wire(now), canonical(refs).decode(), order['id']), write=True)
    statement = {'order_id': order['id'], 'refund_minor': refund_minor,
                 'release_minor': total-refund_minor, 'reason': reason,
                 'decision_id': decision['id'] if decision else None,
                 'policy': order['dispute_policy'], 'receipts': receipts, 'at': wire(now)}
    tx.execute('INSERT INTO order_settlements(order_id,decision_id,body) VALUES (?,?,?)',
        (order['id'], statement['decision_id'], canonical(statement).decode()), write=True)
    if decision:
        tx.execute("UPDATE arbitration_cases SET state='executed' WHERE id=? AND state='decided'",
                   (decision['case_id'],), write=True)
    order.update(receipt_refs=refs, settled_at=wire(now))
    return receipts


async def resolve_due(app, *, limit=100):
    """Bounded worker tick, never called by a GET or by the ClearingEngine to buy.

    Time starts at persisted creation/funding, not worker retries. Undelivered
    services/manual ciphertext refund; subjective complaints and expired panels
    remain held. This function has no external side effect.
    """
    now = app.clock()
    changed = []
    async with app.metadata.transaction(write=True) as tx:
        rows = tx.rows('''SELECT o.id,o.buyer FROM store_orders o
            JOIN order_deadlines d ON d.order_id=o.id
            WHERE o.state IN ('created','funded') AND d.expires_at<=?
            ORDER BY d.expires_at,o.id LIMIT ?''', (wire(now),limit))
        for order_id, buyer in rows:
            order = _row(tx, order_id, buyer)
            terms = contract(tx, order_id)
            policy = terms['policy']['policy']
            origin, seconds = ((order['created_at'], policy['funding_timeout_seconds'])
                if order['state'] == 'created' else
                (order['funded_at'], policy['delivery_timeout_seconds']))
            if now < parse_time(origin) + timedelta(seconds=seconds):
                continue
            if order['state'] == 'created':
                await transition(tx, order, 'cancelled', now=now, actor=buyer,
                                 request_id='timeout:'+order_id, reason='funding_timeout')
            else:
                await settle(app, tx, order, now=now, actor=buyer,
                    request_id='timeout:'+order_id, reason='delivery_timeout',
                    refund_minor=order['total_price_minor'])
            changed.append(order_id)
    return changed
