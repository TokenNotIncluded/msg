"""Domain-separated AES-GCM storage for server-held custodial key material."""
from __future__ import annotations

import os
import hmac

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey,X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

from msg.core.codec import b64,canonical,unb64,wire
from msg.core.errors import Failure,require
from msg.security.age_keys import recipient_from_identity,private_from_identity
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


def _upgrade_aad(subject,challenge_id):
    return canonical({'domain':'msg-custodial-upgrade-ephemeral-v1',
                      'subject':subject,'challenge_id':challenge_id})


def seal_upgrade_private(app,subject,challenge_id,private):
    nonce=os.urandom(12)
    cipher=AESGCM(app._vault_key).encrypt(nonce,private,
                                           _upgrade_aad(subject,challenge_id))
    return b64(nonce),b64(cipher)


def open_upgrade_private(app,subject,challenge_id,nonce,ciphertext):
    try:
        raw=AESGCM(app._vault_key).decrypt(unb64(nonce,limit=12),
            unb64(ciphertext,limit=128),_upgrade_aad(subject,challenge_id))
        return X25519PrivateKey.from_private_bytes(raw)
    except (InvalidTag,ValueError,TypeError) as exc:
        raise Failure('custodial_upgrade_challenge_corrupt') from exc


UPGRADE_CONTEXT_FIELDS=('subject_id','challenge_id','request_id','handle','public_key',
                        'encryption_recipient','server_public','nonce','expires_at')


def _proof(shared,challenge):
    context={name:challenge[name] for name in UPGRADE_CONTEXT_FIELDS}
    key=HKDF(algorithm=hashes.SHA256(),length=32,
             salt=unb64(context['nonce'],limit=64),
             info=b'msg-custodial-upgrade-pop-v1').derive(shared)
    return b64(hmac.digest(key,canonical(context),'sha256'))


def client_upgrade_proof(age_identity,challenge):
    private=private_from_identity(age_identity)
    shared=private.exchange(X25519PublicKey.from_public_bytes(
        unb64(challenge['server_public'],limit=32)))
    return _proof(shared,challenge)


def server_upgrade_proof(app,subject,challenge,nonce,ciphertext):
    from msg.security.age_keys import public_from_recipient
    private=open_upgrade_private(app,subject,challenge['challenge_id'],nonce,ciphertext)
    shared=private.exchange(X25519PublicKey.from_public_bytes(
        public_from_recipient(challenge['encryption_recipient'])))
    return _proof(shared,challenge)
