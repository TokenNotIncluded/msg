"""Public age recovery recipients, independent of account encryption identities.

Plugin data is opaque: checking its public Bech32 encoding does not prove that
the plugin accepts it, or that anyone can decrypt an envelope addressed to it.
Only the local age implementation and the recipient's plugin validate that data.
"""

from __future__ import annotations

import hashlib

from msg.core.errors import Failure, require
from msg.security.age_keys import (
    ALPHABET,
    _convert,
    _hrp_expand,
    _polymod,
    encryption_key_id,
    public_from_recipient,
)

MAX_RECIPIENT_LENGTH = 4096
PLUGIN_NAME_CHARACTERS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789+-._')


def recovery_recipient_fingerprint(recipient):
    """Validate a recovery recipient and return its stable public fingerprint.

    Native X25519 fingerprints retain their existing encryption-key IDs. Plugin
    fingerprints bind the exact canonical public encoding in a separate domain;
    they are metadata identifiers, never account encryption-key IDs.
    """
    require(
        type(recipient) is str
        and 1 <= len(recipient) <= MAX_RECIPIENT_LENGTH
        and recipient.isascii()
        and recipient == recipient.lower(),
        'invalid_age_recipient',
    )
    hrp, separator, encoded = recipient.rpartition('1')
    if hrp == 'age':
        return encryption_key_id(public_from_recipient(recipient))
    require(
        separator
        and hrp.startswith('age1')
        and (name := hrp[4:])
        and all(character in PLUGIN_NAME_CHARACTERS for character in name)
        and len(encoded) >= 6
        and all(character in ALPHABET for character in encoded),
        'invalid_age_recipient',
    )
    numbers = [ALPHABET.index(character) for character in encoded]
    require(_polymod([*_hrp_expand(hrp), *numbers]) == 1, 'invalid_age_recipient')
    try:
        _convert(numbers[:-6], 5, 8, pad=False)
    except Failure as exc:
        raise Failure('invalid_age_recipient') from exc
    return (
        'rr_'
        + hashlib.sha256(
            b'msg-age-plugin-recovery-recipient-v1\0' + recipient.encode('ascii')
        ).hexdigest()
    )
