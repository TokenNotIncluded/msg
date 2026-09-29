"""One escrow release entry point, guarded transitions and append-only facts."""
from __future__ import annotations

from datetime import timedelta

from msg.core.codec import b64, canonical, decode, digest, freeze_json, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import Event, Signature
from msg.core.requests import signing_bytes
from msg.market.policy import contract, delivery_snapshot
from msg.plugins.common import new_id
from msg.market.ledger import CURRENCY_ID, balance as _balance
# Preserve the failure-injection seam while using the shared protected release.
from msg.market.ledger import post_escrow_release as _post_transfer
from msg.market.ledger import _ESCROW_WRITE  # Compatibility identity; only ledger uses it.
from msg.security.crypto import verify


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


def _post_escrow_transfer(tx, *, order, recipient, amount, actor, request_id, now,
                          receipt_signer, reference, kind):
    """The only place that supplies internal authority to debit OrderEscrow.

    Legacy signed decisions and versioned arbitration keep their distinct
    journals, signatures and request IDs, but share this account boundary.
    """
    return _post_transfer(tx, escrow_account=order['escrow_subject'],
        source_id=order['id'], account_kind='order_escrow', buyer=order['buyer'],
        seller=order['seller'], recipient=recipient, amount=amount, actor=actor,
        request_id=request_id, now=now, receipt_signer=receipt_signer,
        reference=reference, kind=kind)


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
    locked = contract(tx, order['id'])
    require(not (locked['version'] == 4 and reason == 'managed_instant_verified'),
            'escrow_release_forbidden')
    require(tx.one('SELECT 1 FROM order_escrow_decisions WHERE order_id=?', (order['id'],)) is None,
            'order_already_settled')
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
            receipts.append(_post_escrow_transfer(tx, order=order,
                recipient=recipient, amount=amount, actor=actor,
                request_id=f'{order["id"]}:{suffix}', now=now,
                receipt_signer=app.receipt_signer, reference=f'order_{suffix}:{order["id"]}',
                kind=kind))
    final = 'refunded' if refund_minor == total else 'settled'
    await transition(tx, order, final, now=now, actor=actor, request_id=request_id, reason=reason)
    refs = order['receipt_refs'] + [r['body']['transaction_id'] for r in receipts]
    tx.execute('UPDATE store_orders SET settled_at=?,receipt_refs=? WHERE id=?',
               (wire(now), canonical(refs).decode(), order['id']), write=True)
    statement = {'order_id': order['id'], 'refund_minor': refund_minor,
                 'release_minor': total-refund_minor, 'reason': reason,
                 'decision_id': decision['id'] if decision else None,
                 'policy': order['dispute_policy'], 'receipts': receipts, 'at': wire(now),
                 'order_facts': {name: order[name] for name in ('payment_transaction_id',
                     'payment_intent_digest', 'funded_at', 'delivered_at')},
                 'delivery_snapshot': delivery_snapshot(tx, order['id'])}
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
    from msg.market.order_records import read_order as _row
    now = app.clock()
    changed = []
    async with app.metadata.transaction(write=True) as tx:
        app.runtime_generation.require_current(tx)
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


# Published checkout contracts retain their original signed policy and journals.
POLICY = freeze_json({
    'escrow_policy': 'escrow-v1', 'dispute_policy': 'dispute-v1',
    'version': 1, 'reasons': ['buyer_cancel', 'buyer_accept'],
    'arbitration_enabled': False, 'automatic_timeout_enabled': False,
    'allow_split': False,
})
POLICY_DIGEST = digest(POLICY)
# A new policy ID, not a silent expansion of already signed escrow-v1 terms.
INSTANT_POLICY = freeze_json({
    **POLICY, 'escrow_policy': 'escrow-instant-v1',
    'reasons': ['buyer_cancel', 'buyer_accept', 'checkout_accept'],
    'checkout_contract': 'orders.buy@2', 'requires_package_digest': True,
    'checkout_claims_delivery': False,
})
POLICIES = (POLICY, INSTANT_POLICY)


def validate_policy(escrow_policy, dispute_policy):
    for policy in POLICIES:
        if (escrow_policy, dispute_policy) == (policy['escrow_policy'], policy['dispute_policy']):
            return policy
    require(False, 'escrow_policy_unsupported')


def validate_decision(decision, expected, public_key, now):
    """A signature never substitutes for current deterministic policy checks."""
    require(set(decision)=={'body','signature'} and decision['body']==expected,
            'escrow_decision_mismatch')
    require(parse_time(expected['expires_at'])>now, 'escrow_decision_expired')
    verify(public_key,canonical(expected),decode(Signature,decision['signature']),
           purpose='escrow-decision-v1')


class EscrowEngine:
    def __init__(self, app):
        self.app=app

    async def settle(self, ctx, request, tx, *, reason, order_id=None):
        # Import handlers' read-only projections, never their write entry points.
        from msg.market.managed_delivery import read_delivery as _delivery, verify_managed_delivery as _verified_delivery
        from msg.market.order_records import read_order as _row, require_signed_subject as _subject
        buyer=_subject(ctx)
        require(order_id is None or reason == 'checkout_accept', 'escrow_decision_mismatch')
        order=_row(tx,order_id or request.arguments['order_id'],buyer)
        require(tx.one('SELECT 1 FROM order_contracts WHERE order_id=?', (order['id'],)) is None,
                'order_contract_requires_v3')
        require(tx.one('SELECT 1 FROM order_settlements WHERE order_id=?', (order['id'],)) is None,
                'order_already_resolved')
        require(order['buyer']==buyer, 'order_not_found')
        policy=validate_policy(order['escrow_policy'],order['dispute_policy'])
        require(reason in policy['reasons'], 'escrow_reason_unsupported')
        await self.app.authorizer.require_base(ctx.principal,
            f'{request.operation}@{request.contract_version}',buyer,tx)
        delivery=_delivery(tx,order['id'])
        if reason=='buyer_cancel':
            require(request.operation=='orders.cancel' and request.contract_version==1,
                    'escrow_decision_mismatch')
            require(order['state']=='funded' and order['delivered_at'] is None and
                    delivery is None, 'order_not_cancellable')
            recipient,outcome,state=buyer,'refund','refunded'
        elif reason in {'buyer_accept', 'checkout_accept'}:
            checkout = reason == 'checkout_accept'
            if checkout:
                require(request.operation == 'orders.buy' and request.contract_version == 2 and
                        request.arguments.get('auto_accept') is True and
                        request.arguments['package_digest'] == order['package_digest'] and
                        order['payment_intent_digest'] == request.payload_digest,
                        'escrow_decision_mismatch')
            else:
                require(request.operation=='delivery.accept' and request.contract_version==1,
                        'escrow_decision_mismatch')
            await _verified_delivery(self.app,tx,order,delivery)
            if not checkout:
                require(delivery['delivery_digest']==request.arguments['delivery_digest'],
                        'delivery_mismatch')
            require(order['state']=='delivered' and order['delivered_at'] is not None and
                    delivery['state']=='prepared', 'delivery_not_acceptable')
            require(order['delivery_target']=={'subject_id':buyer,'channel':'site'},
                    'delivery_recipient_mismatch')
            recipient,outcome,state=order['seller'],'release','settled'
        else:
            require(False, 'escrow_reason_unsupported')
        require(order['currency_id']==CURRENCY_ID and
                _balance(tx,order['escrow_subject'])==order['total_price_minor'],
                'escrow_balance_mismatch')
        account=tx.one('SELECT kind,subject_id,source_id FROM ledger_accounts WHERE id=?',
                       (order['escrow_subject'],))
        require(account==('order_escrow',None,order['id']), 'escrow_account_mismatch')
        require(tx.one('SELECT 1 FROM order_escrow_decisions WHERE order_id=?',
                       (order['id'],)) is None, 'order_already_resolved')
        # The ledger retains one row per (actor, request_id). Checkout has two
        # legs, so its release uses a domain-separated internal ID, explicitly
        # linked to the original signed request by the immutable decision.
        ledger_request_id = (digest(('checkout-settlement-v1', buyer, request.request_id, order['id']))
                             if reason == 'checkout_accept' else request.request_id)
        body={'id':'ed_'+digest((order['id'],request.payload_digest,reason))[7:],
            'order_id':order['id'],'outcome':outcome,'reason':reason,
            'recipient':recipient,'currency_id':CURRENCY_ID,
            'amount_minor':order['total_price_minor'],
            'policy_version':policy['version'],'policy_digest':digest(policy),
            'order_digest':digest(order),'evidence_digest':digest(delivery),
            'request_id':request.request_id,'request_digest':request.payload_digest,
            'actor':buyer,'issued_at':wire(ctx.now),'expires_at':wire(request.expires_at)}
        if reason == 'checkout_accept':
            body['ledger_request_id'] = ledger_request_id
        decision={'body':body,'signature':wire(self.app.receipt_signer.sign(
            canonical(body),purpose='escrow-decision-v1'))}
        # The current policy, source request, order and delivery are all checked
        # under the same serialized write transaction as the debit and journal.
        validate_decision(decision,body,self.app.receipt_signer.public_key,
                          self.app.executor.clock())
        receipt=_post_escrow_transfer(tx,order=order,recipient=recipient,
            amount=order['total_price_minor'],actor=buyer,request_id=ledger_request_id,
            now=ctx.now,receipt_signer=self.app.receipt_signer,
            reference='order_'+outcome+':'+order['id'],
            kind='refund' if outcome=='refund' else 'transfer')
        transaction_id=receipt['body']['transaction_id']
        if reason == 'checkout_accept':
            changed=tx.execute("""UPDATE store_deliveries SET receipt=?
                WHERE order_id=? AND recipient_subject=? AND state='prepared'""",
                (canonical(receipt).decode(),order['id'],buyer),write=True)
            require(changed.rowcount==1, 'delivery_not_acceptable')
        elif outcome=='release':
            changed=tx.execute("""UPDATE store_deliveries
                SET state='claimed',claimed_at=?,receipt=?
                WHERE order_id=? AND recipient_subject=? AND state='prepared'""",
                (wire(ctx.now),canonical(receipt).decode(),order['id'],buyer),write=True)
            require(changed.rowcount==1, 'delivery_not_acceptable')
        changed=tx.execute('''UPDATE store_orders SET state=?,settled_at=?,receipt_refs=?
            WHERE id=? AND buyer=? AND state=?''',
            (state,wire(ctx.now) if outcome=='release' else order['settled_at'],
             canonical([*order['receipt_refs'],transaction_id]).decode(),
             order['id'],buyer,order['state']),write=True)
        require(changed.rowcount==1, 'escrow_state_conflict')
        tx.execute('''INSERT INTO order_escrow_decisions
            (order_id,id,body,signature,source_proof,transaction_id) VALUES (?,?,?,?,?,?)''',
            (order['id'],body['id'],canonical(body).decode(),canonical(decision['signature']).decode(),
             canonical({'signed_envelope':b64(signing_bytes(request)),
                        'proof':wire(request.proof)}).decode(),transaction_id),write=True)
        return receipt,decision,delivery

    async def verify_checkout_release(self, tx, order, delivery):
        """Validate an already committed release, without extending its execution TTL."""
        require(order['state'] == 'settled' and order['settled_at'] is not None and
                delivery['receipt'] is not None, 'delivery_not_acceptable')
        policy = validate_policy(order['escrow_policy'], order['dispute_policy'])
        require(policy == INSTANT_POLICY, 'delivery_not_acceptable')
        row = tx.one('''SELECT body,signature,transaction_id FROM order_escrow_decisions
            WHERE order_id=?''', (order['id'],))
        require(row is not None, 'escrow_decision_mismatch')
        body = loads(row[0])
        require(body['order_id'] == order['id'] and body['reason'] == 'checkout_accept' and
                body['outcome'] == 'release' and body['actor'] == order['buyer'] and
                body['recipient'] == order['seller'] and body['currency_id'] == CURRENCY_ID and
                body['amount_minor'] == order['total_price_minor'] and
                body['policy_version'] == policy['version'] and body['policy_digest'] == digest(policy) and
                body['request_digest'] == order['payment_intent_digest'] and
                order['receipt_refs'] == [order['payment_transaction_id'], row[2]] and
                order['settled_at'] == body['issued_at'], 'escrow_decision_mismatch')
        # Reconstruct only the fields changed by the original atomic release.
        # Mutable projections after restoration must still match its signed
        # order and delivery snapshots, not merely buyer/amount/order ID.
        issued_order = {**order, 'state': 'delivered', 'settled_at': None,
                        'receipt_refs': [order['payment_transaction_id']]}
        issued_delivery = {**delivery, 'state': 'prepared', 'claimed_at': None, 'receipt': None}
        ledger_request_id = digest(('checkout-settlement-v1', order['buyer'],
                                    body['request_id'], order['id']))
        require(body['order_digest'] == digest(issued_order) and
                body['evidence_digest'] == digest(issued_delivery) and
                body['ledger_request_id'] == ledger_request_id, 'escrow_decision_mismatch')
        # The decision's deadline limited execution, not how long the buyer may
        # acknowledge a previously paid order. Verify its original signed fact.
        validate_decision({'body': body, 'signature': loads(row[1])}, body,
                          self.app.receipt_signer.public_key, parse_time(body['issued_at']))
        ledger = tx.one('''SELECT debit_account,credit_account,amount_minor,currency_id,reference,receipt,actor,request_id
            FROM money_ledger WHERE id=?''', (row[2],))
        require(ledger is not None and ledger[:5] == (
            order['escrow_subject'], order['seller'], order['total_price_minor'],
            CURRENCY_ID, 'order_release:' + order['id']) and
            loads(ledger[5]) == delivery['receipt'] and
            ledger[6:] == (order['buyer'], ledger_request_id) and
            _balance(tx, order['escrow_subject']) == 0, 'escrow_decision_mismatch')
