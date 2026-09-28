"""One current buyer binding for site delivery and optional email projections."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from msg.core.codec import decode, loads, wire
from msg.core.errors import Failure, require
from msg.core.models import EmailSettings


def mail_enabled(app):
    config = app.settings.server.mail
    return config is not None and config.enabled


def pickup_url(app, order_id):
    """Build a non-capability link without leaking credentials from configuration."""
    origin = app.settings.service_url
    require(isinstance(origin, str) and
            all(ord(c) > 32 and ord(c) != 127 for c in origin) and
            '\\' not in origin, 'invalid_delivery_origin')
    try:
        base = urlsplit(origin)
        valid = (base.scheme in {'http', 'https'} and base.hostname and
                 base.username is None and base.password is None and
                 not base.query and not base.fragment)
        base.port  # Validate malformed/out-of-range ports as well as IPv6 syntax.
    except ValueError:
        raise Failure('invalid_delivery_origin') from None
    require(valid, 'invalid_delivery_origin')
    return origin.rstrip('/') + '/_orders/' + order_id + '/_delivery'


def account_email(tx, buyer):
    """Read the live account row once; endpoint encodings remain version-specific."""
    row = tx.one('SELECT generation,body FROM emails WHERE subject=?', (buyer,))
    if row is None:
        return None, None
    email = decode(EmailSettings, loads(row[1]))
    require(email.subject_id == buyer, 'delivery_recipient_mismatch')
    return row[0], email


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
    generation, email = account_email(tx, buyer)
    if email is None or email.address != address or email.verified_at is None:
        return None
    validate_address(email.address)
    return {'endpoint_id': 'email:' + buyer, 'owner': buyer,
            'generation': generation, 'address_snapshot': address,
            'verified_at': wire(email.verified_at)}


def validate_email_binding(tx, order, binding):
    validate_target(order)
    require(isinstance(binding, dict) and binding.get('owner') == order['buyer'],
            'delivery_recipient_mismatch')
    current = email_binding(tx, order['buyer'], binding.get('address_snapshot'))
    require(current is not None and current == binding, 'delivery_recipient_mismatch')
    return current['address_snapshot']
