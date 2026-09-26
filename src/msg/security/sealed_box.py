"""Client-side X25519 + HKDF-SHA256 + AES-256-GCM envelope.

This is a versioned msg envelope, not age, OpenPGP or a new cryptographic
primitive. Encryption keys are independent of signing keys. The server validates
only the envelope shape; it never receives a decryption key.
"""
from __future__ import annotations
import os
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from msg.core.codec import b64, unb64, canonical, loads
from msg.core.errors import Failure, require

FORMAT='msg-x25519-v1'


def generate_key():
    key=X25519PrivateKey.generate()
    return key.private_bytes_raw(),key.public_key().public_bytes_raw()


def validate_envelope(content):
    value=loads(content)
    require(isinstance(value,dict) and set(value)=={'format','recipient','ephemeral','salt','nonce','ciphertext'},
            'invalid_ciphertext_envelope')
    require(value['format']==FORMAT,'invalid_ciphertext_format')
    for name,size in (('recipient',32),('ephemeral',32),('salt',32),('nonce',12)):
        require(len(unb64(value[name],limit=size))==size,'invalid_ciphertext_envelope')
    require(len(unb64(value['ciphertext']))>=16,'invalid_ciphertext_envelope')
    return value


def derived(shared,salt,recipient,ephemeral):
    return HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,
                info=b'msg-keystore-v1\0'+recipient+ephemeral).derive(shared)


def encrypt(plaintext,recipient_public):
    require(isinstance(plaintext,bytes) and len(recipient_public)==32,'invalid_encryption_input')
    ephemeral=X25519PrivateKey.generate()
    public=ephemeral.public_key().public_bytes_raw()
    salt,nonce=os.urandom(32),os.urandom(12)
    header={'format':FORMAT,'recipient':b64(recipient_public),'ephemeral':b64(public),
            'salt':b64(salt),'nonce':b64(nonce)}
    try:
        shared=ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public))
    except ValueError as exc:
        raise Failure('invalid_encryption_recipient') from exc
    key=derived(shared,salt,recipient_public,public)
    ciphertext=AESGCM(key).encrypt(nonce,plaintext,canonical(header))
    return canonical({**header,'ciphertext':b64(ciphertext)})


def decrypt(content,private):
    try:
        value=validate_envelope(content)
        key=X25519PrivateKey.from_private_bytes(private)
        recipient=key.public_key().public_bytes_raw()
        require(b64(recipient)==value['recipient'],'wrong_decryption_recipient')
        ephemeral=unb64(value['ephemeral'])
        shared=key.exchange(X25519PublicKey.from_public_bytes(ephemeral))
        derived_key=derived(shared,unb64(value['salt']),recipient,ephemeral)
        header={k:v for k,v in value.items() if k!='ciphertext'}
        return AESGCM(derived_key).decrypt(unb64(value['nonce']),unb64(value['ciphertext']),canonical(header))
    except (ValueError,InvalidTag,KeyError,TypeError) as exc:
        if isinstance(exc,Failure):raise
        raise Failure('ciphertext_authentication_failed') from exc
