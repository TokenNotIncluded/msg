"""Pure operation selection for published subject views and permanent aliases."""

import re

SUBJECT_RESOURCE_ALIASES = {
    'cert': 'certificates',
    'certificates': 'certificates',
    'ks': 'keystore',
    'keystore': 'keystore',
    'ssh': 'keys',
    'ssh-keys': 'keys',
}
SUBJECT_KEY_ALIASES = {
    'pk': ('identity.identity_key_get', 'pk'),
    'pubkey': ('identity.identity_key_get', 'pk'),
    'k': ('identity.identity_key_list', 'k'),
    'keys': ('identity.identity_key_list', 'k'),
    'ek': ('identity.encryption_key_get', 'ek'),
    'encryption-key': ('identity.encryption_key_get', 'ek'),
    'e': ('identity.encryption_key_list', 'e'),
    'encryption-keys': ('identity.encryption_key_list', 'e'),
}
SUBJECT_OPERATION_ALIASES = {
    'ach': 'achievement.list',
    'achievements': 'achievement.list',
    'in': 'communication.inbox',
    'inbox': 'communication.inbox',
    'out': 'communication.outbox',
    'outbox': 'communication.outbox',
    'dm': 'communication.dm_list',
    'following': 'communication.following',
}
SUBJECT_COLLABORATION_VIEWS = frozenset({
    'handoffs',
    'leases',
    'requests',
    'offers',
    'checkpoints',
    'proposals',
    'watches',
})
CERTIFICATE_COLLECTION_VIEWS = frozenset({None, '/', '/json', '/meta', '/history'})


def subject_view_operation(path):
    """Select metadata without resolving a subject, resource or hosting site.

    The router validates path spelling and arguments after the effect fence.
    Both the fence and execution use this same selection.
    """
    match = re.fullmatch(r'/@([^/]+)/([^/]+)(/.*)?', path)
    if match is None:
        return None
    _, name, remainder = match.groups()
    tail = (remainder or '').strip('/')
    if name == 'orders':
        tail = tail.removesuffix('/json')
        if tail in {'', 'json'}:
            return 'orders.list'
        parts = tail.split('/')
        if len(parts) == 2:
            return 'orders.payment' if parts[1] == '_payment' else 'delivery.get'
        return 'orders.get'
    if name in {'public-balance', 'public-ledger'}:
        return 'money.public_ledger' if name == 'public-ledger' else 'money.public_balance'
    if name in {'bal', 'balance', 'ledger'}:
        return 'money.ledger' if name == 'ledger' else 'money.balance'
    if name in SUBJECT_COLLABORATION_VIEWS:
        singular = 'watch' if name == 'watches' else name[:-1]
        return 'communication.' + singular + ('_list' if tail in {'', 'json'} else '_get')
    if name == 'receipts':
        return 'communication.receipt_' + ('list' if tail in {'', 'json'} else 'get')
    if name in {'cert', 'certificates'} and remainder not in CERTIFICATE_COLLECTION_VIEWS:
        return 'cert.get'
    if name in SUBJECT_KEY_ALIASES:
        operation, _ = SUBJECT_KEY_ALIASES[name]
        return operation.replace('_list', '_get') if tail not in {'', 'json'} else operation
    return SUBJECT_OPERATION_ALIASES.get(name)
