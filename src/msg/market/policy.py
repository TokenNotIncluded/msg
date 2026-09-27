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
    return body
