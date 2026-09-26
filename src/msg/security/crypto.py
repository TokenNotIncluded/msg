"""Ed25519 and password wrapping from cryptography; no custom cryptographic primitive."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, PublicFormat

from msg.core.codec import b64, canonical, unb64
from msg.core.errors import Failure, require
from msg.core.models import Signature


def key_id(public: bytes) -> str:
    return 'k_'+hashlib.sha256(public).hexdigest()


def subject_id(public: bytes) -> str:
    return 'u_'+hashlib.sha256(b'msg-subject-v1\0'+public).hexdigest()[:32]


def framed(payload: bytes, purpose: str) -> bytes:
    require(purpose and '\0' not in purpose,'invalid_signature_purpose')
    return b'msg.lmm.best/v1/'+purpose.encode('ascii')+b'\0'+payload


@dataclass(frozen=True,slots=True)
class Ed25519Signer:
    _private: Ed25519PrivateKey=field(repr=False)

    @classmethod
    def generate(cls):
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_bytes(cls,private: bytes):
        require(len(private)==32,'invalid_private_key')
        return cls(Ed25519PrivateKey.from_private_bytes(private))

    @property
    def public_key(self):
        return self._private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)

    @property
    def key_id(self):
        return key_id(self.public_key)

    def private_bytes(self):
        return self._private.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())

    def sign(self,payload: bytes, *, purpose: str):
        return Signature(key_id=self.key_id,algorithm='ed25519',value=self._private.sign(framed(payload,purpose)))


def verify(public: bytes,payload: bytes,signature: Signature, *, purpose: str):
    require(signature.algorithm=='ed25519' and signature.key_id==key_id(public),'invalid_signature')
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature.value,framed(payload,purpose))
    except (InvalidSignature,ValueError,TypeError) as exc:
        raise Failure('invalid_signature') from exc


def _pin(pin):
    require(type(pin) is str and len(pin)>=12 and
            pin.casefold() not in {'password','passwordpassword','changemechangeme','defaultdefault'} and
            not (pin.isdecimal() and len(pin)<20),'weak_pin')
    return pin.encode('utf-8')


def seal_private_key(private: bytes,pin: str):
    password=_pin(pin)
    signer=Ed25519Signer.from_bytes(private)
    header={'version':1,'kdf':'scrypt','n':2**17,'r':8,'p':1,
            'salt':b64(os.urandom(16)),'nonce':b64(os.urandom(12)),
            'cipher':'aes-256-gcm','public_key':b64(signer.public_key)}
    derived=Scrypt(salt=unb64(header['salt']),length=32,n=header['n'],r=8,p=1).derive(password)
    encrypted=AESGCM(derived).encrypt(unb64(header['nonce']),private,canonical(header))
    return dict(header,ciphertext=b64(encrypted))


def open_private_key(envelope: dict,pin: str):
    required={'version','kdf','n','r','p','salt','nonce','cipher','public_key','ciphertext'}
    require(set(envelope)==required and envelope['version']==1 and envelope['kdf']=='scrypt' and
            envelope['cipher']=='aes-256-gcm','invalid_key_envelope')
    n=envelope['n']
    require(type(n) is int and 2**17<=n<=2**20 and n&(n-1)==0 and
            envelope['r']==8 and envelope['p']==1,'invalid_kdf_parameters')
    header={k:v for k,v in envelope.items() if k!='ciphertext'}
    salt,nonce=unb64(header['salt']),unb64(header['nonce'])
    require(len(salt)==16 and len(nonce)==12,'invalid_key_envelope')
    try:
        derived=Scrypt(salt=salt,length=32,n=n,r=8,p=1).derive(pin.encode('utf-8'))
        private=AESGCM(derived).decrypt(nonce,unb64(envelope['ciphertext']),canonical(header))
        require(b64(Ed25519Signer.from_bytes(private).public_key)==header['public_key'],'invalid_key_envelope')
        return private
    except InvalidTag as exc:
        raise Failure('invalid_pin') from exc
