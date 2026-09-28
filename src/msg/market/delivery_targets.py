"""One current buyer binding for site delivery and optional email projections."""
from __future__ import annotations

import re

from msg.core.codec import decode, loads, wire
from msg.core.errors import require
from msg.core.models import EmailSettings


def validate_target(order, delivery=None):
    require(order['delivery_target'] == {
        'subject_id': order['buyer'], 'channel': 'site'},
        'delivery_recipient_mismatch')
    if delivery is not None:
        require(delivery['order_id'] == order['id'] and
                delivery['recipient_subject'] == order['buyer'] and
                delivery['channel'] == 'site', 'delivery_recipient_mismatch')


def validate_address(address):
    require(isinstance(address, str) and len(address) <= 254 and
            re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address) is not None and
            not any(ord(c) < 32 or ord(c) == 127 for c in address),
            'invalid_email')


def email_binding(tx, buyer, address):
    """Unverified/unmatched addresses are pending, never an authorized target.

    The existing single-email row is the endpoint. Its generation also fences
    revoke/reverify and address reuse; equality of the address alone is unsafe.
    """
    row = tx.one('SELECT generation,body FROM emails WHERE subject=?', (buyer,))
    if row is None:
        return None
    email = decode(EmailSettings, loads(row[1]))
    require(email.subject_id == buyer, 'delivery_recipient_mismatch')
    if email.address != address or email.verified_at is None:
        return None
    validate_address(email.address)
    return {'endpoint_id': 'email:' + buyer, 'owner': buyer,
            'generation': row[0], 'address_snapshot': address,
            'verified_at': wire(email.verified_at)}


def validate_email_binding(tx, order, binding):
    validate_target(order)
    require(isinstance(binding, dict) and binding.get('owner') == order['buyer'],
            'delivery_recipient_mismatch')
    current = email_binding(tx, order['buyer'], binding.get('address_snapshot'))
    require(current is not None and current == binding, 'delivery_recipient_mismatch')
    return current['address_snapshot']
