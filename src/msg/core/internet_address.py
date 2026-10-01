"""Canonical MSG account addresses and signed cross-service message envelopes."""

import re
from urllib.parse import urlsplit

from msg.core.codec import canonical, decode, digest
from msg.core.errors import Failure, require
from msg.core.models import Signature
from msg.security.crypto import verify

NS = 'https://msg.lmm.best/ns/internet/'
PROFILE_REL = 'http://webfinger.net/rel/profile-page'
KEYS_REL = NS + 'identity-keys'
DELIVERY_REL = NS + 'delivery'
SUBJECT_PROPERTY = NS + 'subject-id'
PURPOSE = 'internet-message-v1'
MAX_BODY_BYTES = 16384


def address_parts(value):
    require(isinstance(value, str) and len(value) <= 300, 'invalid_internet_address')
    match = re.fullmatch(r'([a-z][a-z0-9-]{1,40})@([a-zA-Z0-9.-]+(?::[0-9]{1,5})?)', value)
    require(match is not None, 'invalid_internet_address')
    handle, authority = match.groups()
    parsed = urlsplit('https://' + authority)
    require(
        parsed.hostname
        and all(
            re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?', p)
            for p in parsed.hostname.split('.')
        ),
        'invalid_internet_address',
    )
    try:
        require(parsed.port is None or 0 < parsed.port <= 65535, 'invalid_internet_address')
    except ValueError:
        raise Failure('invalid_internet_address') from None
    return handle, authority.lower()


def account_address(name, origin):
    value = name.removeprefix('@') + '@' + urlsplit(origin).netloc
    handle, authority = address_parts(value)
    return handle + '@' + authority


def envelope_bytes(envelope):
    return canonical({key: value for key, value in envelope.items() if key != 'signature'})


def verify_envelope(envelope, public):
    verify(
        public, envelope_bytes(envelope), decode(Signature, envelope['signature']), purpose=PURPOSE
    )


def delivery_request_id(envelope):
    return 'internet-' + digest(envelope).removeprefix('sha256:')
