"""age X25519 key text encoding. This module does not encrypt age payloads."""
from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey

from msg.core.errors import Failure, require

ALPHABET = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'


def _polymod(values):
    generators=(0x3b6a57b2,0x26508e6d,0x1ea119fa,0x3d4233dd,0x2a1462b3)
    check=1
    for value in values:
        top=check>>25
        check=((check&0x1ffffff)<<5)^value
        for index,generator in enumerate(generators):
            if (top>>index)&1:
                check^=generator
    return check


def _hrp_expand(hrp):
    return [ord(char)>>5 for char in hrp]+[0]+[ord(char)&31 for char in hrp]


def _convert(data,from_bits,to_bits, *, pad):
    accumulator=bits=0
    result=[]
    maximum=(1<<to_bits)-1
    for value in data:
        require(0<=value<(1<<from_bits),'invalid_age_key')
        accumulator=(accumulator<<from_bits)|value
        bits+=from_bits
        while bits>=to_bits:
            bits-=to_bits
            result.append((accumulator>>bits)&maximum)
    if pad and bits:
        result.append((accumulator<<(to_bits-bits))&maximum)
    elif not pad:
        require(bits<from_bits and (accumulator<<(to_bits-bits))&maximum==0,'invalid_age_key')
    return bytes(result)


def _encode(hrp,payload):
    values=_convert(payload,8,5,pad=True)
    checksum=_polymod([*_hrp_expand(hrp),*values,0,0,0,0,0,0])^1
    check=[(checksum>>(5*(5-index)))&31 for index in range(6)]
    return hrp+'1'+''.join(ALPHABET[value] for value in (*values,*check))


def _decode(value,hrp):
    require(type(value) is str and value.lower()==value and value.startswith(hrp+'1'),
            'invalid_age_recipient')
    characters=value[len(hrp)+1:]
    require(len(characters)==58 and all(char in ALPHABET for char in characters),
            'invalid_age_recipient')
    numbers=[ALPHABET.index(char) for char in characters]
    require(_polymod([*_hrp_expand(hrp),*numbers])==1,'invalid_age_recipient')
    raw=_convert(numbers[:-6],5,8,pad=False)
    require(len(raw)==32,'invalid_age_recipient')
    return raw


def recipient_from_public(public: bytes):
    require(type(public) is bytes and len(public)==32,'invalid_encryption_public_key')
    return _encode('age',public)


def public_from_recipient(recipient: str):
    public=_decode(recipient,'age')
    try:
        # Reject low-order points which cannot produce an X25519 shared secret.
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(public))
    except ValueError as exc:
        raise Failure('invalid_age_recipient') from exc
    return public


def encryption_key_id(public: bytes):
    require(type(public) is bytes and len(public)==32,'invalid_encryption_public_key')
    return 'ek_'+hashlib.sha256(b'msg-encryption-subkey-v1\0'+public).hexdigest()


def generate_age_key():
    private=X25519PrivateKey.generate()
    secret=private.private_bytes_raw()
    public=private.public_key().public_bytes_raw()
    # age's X25519 identity is upper-case Bech32; the recipient is lower-case.
    identity=_encode('age-secret-key-',secret).upper()
    return identity,recipient_from_public(public)


def recipient_from_identity(identity: str):
    require(type(identity) is str and identity==identity.upper(),'invalid_age_identity')
    try:
        private=_decode(identity.lower(),'age-secret-key-')
        public=X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    except Failure as exc:
        raise Failure('invalid_age_identity') from exc
    return recipient_from_public(public)
