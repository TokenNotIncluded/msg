"""Recovery plugin encodings never become account encryption identities."""

import hashlib

import pytest

from msg.core.errors import Failure
from msg.security.age_keys import (
    ALPHABET,
    _encode,
    _hrp_expand,
    _polymod,
    encryption_key_id,
    generate_age_key,
    public_from_recipient,
)
from msg.security.age_recipients import recovery_recipient_fingerprint


def test_native_recovery_fingerprint_is_unchanged_and_rejects_low_order_key():
    _, recipient = generate_age_key()
    assert recovery_recipient_fingerprint(recipient) == encryption_key_id(
        public_from_recipient(recipient)
    )
    with pytest.raises(Failure, match='invalid_age_recipient'):
        recovery_recipient_fingerprint(_encode('age', bytes(32)))


@pytest.mark.parametrize('name', ['yubikey', 'tag', 'a1-plugin.v2_test+key'])
def test_plugin_fingerprint_binds_exact_public_encoding_in_separate_domain(name):
    recipient = _encode('age1' + name, b'opaque public plugin data')
    fingerprint = recovery_recipient_fingerprint(recipient)
    assert (
        fingerprint
        == 'rr_'
        + hashlib.sha256(
            b'msg-age-plugin-recovery-recipient-v1\0' + recipient.encode('ascii')
        ).hexdigest()
    )
    assert fingerprint != recovery_recipient_fingerprint(
        _encode('age1' + name, b'different public data')
    )
    with pytest.raises(Failure, match='invalid_age_recipient'):
        public_from_recipient(recipient)


@pytest.mark.parametrize(
    'recipient',
    [
        None,
        b'age1bytes',
        '',
        'age1yubikey1opaque',
        'AGE-PLUGIN-YUBIKEY-1SECRET',
        _encode('age1tag', b'public').upper(),
        _encode('age1tag', b'public').replace('age1', 'Age1', 1),
        _encode('age1tag', b'public') + '\n',
        _encode('age1tag', b'public') + ' ',
        _encode('age1', b'public'),
        _encode('age1../unsafe', b'public'),
        _encode('age1t\u212ag', b'public'),
        _encode('age1tag', b'public')[:-1] + '!',
        _encode('age1tag', b'public')[:-1] + 'q',
        'age1tag1' + 'q' * 4096,
    ],
)
def test_invalid_recovery_encoding_is_rejected(recipient):
    with pytest.raises(Failure, match='invalid_age_recipient'):
        recovery_recipient_fingerprint(recipient)


def test_valid_checksum_does_not_hide_noncanonical_padding():
    hrp = 'age1tag'
    values = [1]  # Five nonzero data bits cannot encode a whole byte.
    checksum = _polymod([*_hrp_expand(hrp), *values, 0, 0, 0, 0, 0, 0]) ^ 1
    check = [(checksum >> (5 * (5 - index))) & 31 for index in range(6)]
    recipient = hrp + '1' + ''.join(ALPHABET[value] for value in (*values, *check))
    with pytest.raises(Failure, match='invalid_age_recipient'):
        recovery_recipient_fingerprint(recipient)
