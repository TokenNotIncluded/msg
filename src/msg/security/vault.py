"""Domain-separated AES-GCM storage for server-held custodial key material."""
from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64,canonical,unb64,wire
from msg.core.errors import Failure,require
from msg.security.age_keys import recipient_from_identity
from msg.security.crypto import Ed25519Signer


def _aad(subject,key_id,kind):
    return canonical({'domain':'msg-custodial-vault-v1','subject':subject,
                      'key_id':key_id,'kind':kind})


def _seal(app,subject,key_id,kind,secret):
    nonce=os.urandom(12)
    return b64(nonce),b64(AESGCM(app._vault_key).encrypt(nonce,secret,
                                                       _aad(subject,key_id,kind)))


def store_keys(app,tx,subject,signer,age_identity,age_key_id,now):
    require(recipient_from_identity(age_identity),'invalid_age_identity')
    signing_nonce,signing_ciphertext=_seal(app,subject,signer.key_id,'identity',
                                           signer.private_bytes())
    age_nonce,age_ciphertext=_seal(app,subject,age_key_id,'encryption',
                                   age_identity.encode('ascii'))
    tx.execute('''INSERT INTO custodial_vault
        (subject,signing_key_id,encryption_key_id,signing_nonce,signing_ciphertext,
         age_nonce,age_ciphertext,status,created_at,destroyed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (subject,signer.key_id,age_key_id,signing_nonce,signing_ciphertext,
         age_nonce,age_ciphertext,'active',wire(now),None),write=True)


def _open(app,tx,subject,kind):
    row=tx.one('''SELECT signing_key_id,encryption_key_id,signing_nonce,signing_ciphertext,
        age_nonce,age_ciphertext,status FROM custodial_vault WHERE subject=?''',(subject,))
    require(row is not None and row[6]=='active','custodial_vault_unavailable')
    key_id,nonce,ciphertext=(row[0],row[2],row[3]) if kind=='identity' else (row[1],row[4],row[5])
    require(nonce is not None and ciphertext is not None,'custodial_vault_unavailable')
    try:
        return key_id,AESGCM(app._vault_key).decrypt(unb64(nonce,limit=12),
            unb64(ciphertext,limit=8192),_aad(subject,key_id,kind))
    except (InvalidTag,ValueError,TypeError) as exc:
        raise Failure('custodial_vault_corrupt') from exc


def open_signer(app,tx,subject):
    key_id,secret=_open(app,tx,subject,'identity')
    signer=Ed25519Signer.from_bytes(secret)
    require(signer.key_id==key_id,'custodial_vault_corrupt')
    return signer


def open_age_identity(app,tx,subject):
    key_id,secret=_open(app,tx,subject,'encryption')
    try:
        identity=secret.decode('ascii')
    except UnicodeError as exc:
        raise Failure('custodial_vault_corrupt') from exc
    from msg.security.age_keys import encryption_key_id,public_from_recipient
    recipient=recipient_from_identity(identity)
    require(encryption_key_id(public_from_recipient(recipient))==key_id,
            'custodial_vault_corrupt')
    return identity
