"""Single escrow debit boundary for the implemented objective policy.

This is not an arbitration service: v1 supports a buyer's pre-delivery
cancellation and explicit acceptance of a verified delivery. Disputed orders,
subjective claims, splits and time-based decisions fail closed. No LLM, role or
administrator can supply an arbitrary payout to this entry point.
"""
from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, freeze_json, loads, parse_time, wire
from msg.core.errors import require
from msg.core.models import Signature
from msg.core.requests import signing_bytes
from msg.plugins.money import CURRENCY_ID, _balance, _post_transfer
from msg.security.crypto import verify


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
        from msg.plugins.orders import _row, _subject
        from msg.plugins.delivery import _delivery, _verified_delivery
        buyer=_subject(ctx)
        require(order_id is None or reason == 'checkout_accept', 'escrow_decision_mismatch')
        order=_row(tx,order_id or request.arguments['order_id'],buyer)
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
        receipt=_post_transfer(tx,sender=order['escrow_subject'],recipient=recipient,
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
