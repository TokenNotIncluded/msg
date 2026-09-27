"""Single escrow debit boundary for the implemented objective policy.

This is not an arbitration service: v1 supports a buyer's pre-delivery
cancellation and explicit acceptance of a verified delivery. Disputed orders,
subjective claims, splits and time-based decisions fail closed. No LLM, role or
administrator can supply an arbitrary payout to this entry point.
"""
from __future__ import annotations

from msg.core.codec import b64, canonical, decode, digest, freeze_json, parse_time, wire
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


def validate_policy(escrow_policy, dispute_policy):
    require((escrow_policy,dispute_policy)==(POLICY['escrow_policy'],POLICY['dispute_policy']),
            'escrow_policy_unsupported')


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

    async def settle(self, ctx, request, tx, *, reason):
        # Import handlers' read-only projections, never their write entry points.
        from msg.plugins.orders import _row, _subject
        from msg.plugins.delivery import _delivery, _verified_delivery
        buyer=_subject(ctx)
        order=_row(tx,request.arguments['order_id'],buyer)
        require(order['buyer']==buyer, 'order_not_found')
        validate_policy(order['escrow_policy'],order['dispute_policy'])
        await self.app.authorizer.require_base(ctx.principal,
            f'{request.operation}@{request.contract_version}',buyer,tx)
        delivery=_delivery(tx,order['id'])
        if reason=='buyer_cancel':
            require(request.operation=='orders.cancel' and request.contract_version==1,
                    'escrow_decision_mismatch')
            require(order['state']=='funded' and order['delivered_at'] is None and
                    delivery is None, 'order_not_cancellable')
            recipient,outcome,state=buyer,'refund','refunded'
        elif reason=='buyer_accept':
            require(request.operation=='delivery.accept' and request.contract_version==1,
                    'escrow_decision_mismatch')
            await _verified_delivery(self.app,tx,order,delivery)
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
        body={'id':'ed_'+digest((order['id'],request.payload_digest,reason))[7:],
            'order_id':order['id'],'outcome':outcome,'reason':reason,
            'recipient':recipient,'currency_id':CURRENCY_ID,
            'amount_minor':order['total_price_minor'],
            'policy_version':POLICY['version'],'policy_digest':POLICY_DIGEST,
            'order_digest':digest(order),'evidence_digest':digest(delivery),
            'request_id':request.request_id,'request_digest':request.payload_digest,
            'actor':buyer,'issued_at':wire(ctx.now),'expires_at':wire(request.expires_at)}
        decision={'body':body,'signature':wire(self.app.receipt_signer.sign(
            canonical(body),purpose='escrow-decision-v1'))}
        # The current policy, source request, order and delivery are all checked
        # under the same serialized write transaction as the debit and journal.
        validate_decision(decision,body,self.app.receipt_signer.public_key,
                          self.app.executor.clock())
        receipt=_post_transfer(tx,sender=order['escrow_subject'],recipient=recipient,
            amount=order['total_price_minor'],actor=buyer,request_id=request.request_id,
            now=ctx.now,receipt_signer=self.app.receipt_signer,
            reference='order_'+outcome+':'+order['id'],
            kind='refund' if outcome=='refund' else 'transfer')
        transaction_id=receipt['body']['transaction_id']
        if outcome=='release':
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
