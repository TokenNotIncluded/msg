"""Small, immutable policies. No expressions, callbacks or model discretion."""
from __future__ import annotations

from msg.core.codec import canonical, digest, loads
from msg.core.errors import require

DEFAULT_POLICY = {
    'id': 'dispute-v1', 'version': 1,
    'funding_timeout_seconds': 900, 'delivery_timeout_seconds': 86400,
    'case_timeout_seconds': 604800, 'decision_lifetime_seconds': 86400,
    'panel_size': 3, 'quorum': 2, 'candidates': [],
    'allow_appeal': False, 'appeal_seconds': 3600,
    'selection': 'sha256-order-policy-round-subject-v1',
    'member_invalidation': 'hold-no-replacement-v1',
    'rules': 'objective-faults-v1',
}


def validate(policy):
    require(isinstance(policy, dict) and set(policy) == set(DEFAULT_POLICY),
            'arbitration_policy_invalid')
    require(isinstance(policy['id'], str) and 1 <= len(policy['id']) <= 80 and
            type(policy['version']) is int and policy['version'] == 1 and policy['rules'] == 'objective-faults-v1' and
            policy['selection'] == DEFAULT_POLICY['selection'] and
            policy['member_invalidation'] == DEFAULT_POLICY['member_invalidation'],
            'arbitration_policy_invalid')
    for name, maximum in (('funding_timeout_seconds', 86400),
                          ('delivery_timeout_seconds', 2592000),
                          ('case_timeout_seconds', 2592000),
                          ('decision_lifetime_seconds', 604800),
                          ('appeal_seconds', 86400)):
        require(type(policy[name]) is int and 60 <= policy[name] <= maximum,
                'arbitration_policy_invalid', name)
    size, quorum, candidates = policy['panel_size'], policy['quorum'], policy['candidates']
    require(type(size) is int and 1 <= size <= 9 and type(quorum) is int and
            size // 2 < quorum <= size and type(policy['allow_appeal']) is bool and
            isinstance(candidates, list) and len(candidates) <= 100 and
            all(isinstance(s, str) and s.startswith('u_') and s != 'u_root'
                for s in candidates) and len(candidates) == len(set(candidates)),
            'arbitration_policy_invalid')
    require(not policy['allow_appeal'] or
            policy['decision_lifetime_seconds'] > policy['appeal_seconds'],
            'arbitration_policy_invalid')
    return policy


def load_policy(tx, policy_id):
    row = tx.one('SELECT body,digest FROM arbitration_policies WHERE id=?', (policy_id,))
    if row:
        policy = loads(row[0])
        require(digest(policy) == row[1], 'arbitration_policy_corrupt')
    else:
        require(policy_id == DEFAULT_POLICY['id'], 'arbitration_policy_unknown')
        policy = loads(canonical(DEFAULT_POLICY))
    return validate(policy)


def snapshot(tx, policy_id):
    policy = load_policy(tx, policy_id)
    # Freeze the grant epoch, not merely the subject: revoke+regrant cannot revive
    # a vote or silently replace a member in an existing order.
    epochs = {}
    for candidate in policy['candidates']:
        row = tx.one('SELECT epoch,active FROM arbitrator_roles WHERE subject=?', (candidate,))
        if row and row[1]:
            epochs[candidate] = row[0]
    return {'policy': policy, 'policy_digest': digest(policy), 'candidate_epochs': epochs}


def contract(tx, order_id):
    row = tx.one('SELECT body,digest FROM order_contracts WHERE order_id=?', (order_id,))
    require(row is not None, 'order_contract_required')
    body = loads(row[0])
    require(digest(body) == row[1] and body['order_id'] == order_id,
            'order_contract_corrupt')
    validate_projection(tx, body)
    return body


def delivery_snapshot(tx, order_id):
    """Commit delivery identity and prepared bytes, excluding subsequent buyer ACK."""
    from msg.market.managed_delivery import read_delivery as _delivery
    delivery = _delivery(tx, order_id)
    return digest(None if delivery is None else {
        key: value for key, value in delivery.items()
        if key not in {'state', 'claimed_at', 'receipt'}})


def validate_projection(tx, locked):
    """A restored mutable projection cannot replace the immutable checkout facts."""
    require(locked.get('version') == 3, 'order_contract_version')
    listing = locked['listing']
    expected = {name: locked[name] for name in (
        'buyer', 'seller', 'listing_id', 'listing_revision', 'package_id',
        'package_revision', 'package_digest', 'quantity', 'total_price_minor',
        'escrow_subject', 'terms_digest', 'created_at')}
    expected.update(unit_price_minor=listing['price_minor'],
        currency_id=listing['currency_id'], escrow_policy=listing['escrow_policy'],
        dispute_policy=listing['dispute_policy'])
    mutable = ('state', 'receipt_refs', 'settled_at', 'payment_transaction_id',
               'payment_intent_digest', 'funded_at', 'delivered_at')
    columns = (*expected, *mutable)
    row = tx.one('SELECT ' + ','.join(columns) + ' FROM store_orders WHERE id=?',
                 (locked['order_id'],))
    require(row is not None, 'order_contract_mismatch')
    order = dict(zip(columns, row))
    require(all(order[name] == value for name, value in expected.items()),
            'order_contract_mismatch')
    if order['payment_transaction_id'] is not None:
        payment = tx.one('''SELECT debit_account,credit_account,amount_minor,currency_id,
            reference,committed_at FROM money_ledger WHERE id=?''',
            (order['payment_transaction_id'],))
        require(payment == (locked['buyer'], locked['escrow_subject'],
            locked['total_price_minor'], listing['currency_id'],
            'order_fund:' + locked['order_id'], order['funded_at']), 'order_payment_mismatch')
    fact = tx.one('SELECT body FROM order_settlements WHERE order_id=?', (locked['order_id'],))
    if fact is None:
        require(order['state'] not in {'settled', 'refunded'}, 'order_settlement_missing')
        return
    fact = loads(fact[0])
    receipts = fact['receipts']
    references = [order['payment_transaction_id']] + [r['body']['transaction_id'] for r in receipts]
    require(fact['order_id'] == locked['order_id'] and
            all(type(fact[name]) is int and fact[name] >= 0
                for name in ('refund_minor', 'release_minor')), 'order_settlement_mismatch')
    # Authentic ledger receipts alone are not enough: each one must be the
    # exact escrow leg this fact claims, not e.g. the funding payment reused.
    legs = [leg for leg in (
        ('refund', locked['buyer'], fact['refund_minor'], 'order_refund:' + locked['order_id']),
        ('transfer', locked['seller'], fact['release_minor'], 'order_release:' + locked['order_id']),
    ) if leg[2]]
    require([(r['body']['kind'], r['body']['from_subject'], r['body']['to_subject'],
              r['body']['amount_minor'], r['body']['currency_id'], r['body']['reference'])
             for r in receipts] ==
            [(kind, locked['escrow_subject'], recipient, amount, listing['currency_id'], reference)
             for kind, recipient, amount, reference in legs], 'order_settlement_mismatch')
    require(fact['refund_minor'] + fact['release_minor'] == locked['total_price_minor'] and
            order['state'] == ('refunded' if fact['release_minor'] == 0 else 'settled') and
            order['settled_at'] == fact['at'] and loads(order['receipt_refs']) == references and
            all(order[name] == value for name, value in fact['order_facts'].items()) and
            delivery_snapshot(tx, locked['order_id']) == fact['delivery_snapshot'],
            'order_settlement_mismatch')
    for receipt in receipts:
        row = tx.one('SELECT receipt FROM money_ledger WHERE id=?',
                      (receipt['body']['transaction_id'],))
        require(row is not None and loads(row[0]) == receipt, 'order_settlement_mismatch')
