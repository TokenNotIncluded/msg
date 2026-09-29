"""Domain-separated AES-GCM storage for server-held custodial key material."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from msg.core.codec import b64, canonical, unb64, wire
from msg.core.errors import Failure, require
from msg.security.age_keys import recipient_from_identity
from msg.security.crypto import Ed25519Signer
from msg.security.custodial_protocol import (
    UPGRADE_CONTEXT_FIELDS as UPGRADE_CONTEXT_FIELDS,
    _proof as _proof,
    client_upgrade_proof as client_upgrade_proof,
)


def _aad(subject, key_id, kind):
    return canonical({
        'domain': 'msg-custodial-vault-v1',
        'subject': subject,
        'key_id': key_id,
        'kind': kind,
    })


def _seal(app, subject, key_id, kind, secret):
    nonce = os.urandom(12)
    return b64(nonce), b64(
        AESGCM(app._vault_key).encrypt(nonce, secret, _aad(subject, key_id, kind))
    )


def store_keys(app, tx, subject, signer, age_identity, age_key_id, now):
    require(recipient_from_identity(age_identity), 'invalid_age_identity')
    signing_nonce, signing_ciphertext = _seal(
        app, subject, signer.key_id, 'identity', signer.private_bytes()
    )
    age_nonce, age_ciphertext = _seal(
        app, subject, age_key_id, 'encryption', age_identity.encode('ascii')
    )
    tx.execute(
        """INSERT INTO custodial_vault
        (subject,signing_key_id,encryption_key_id,signing_nonce,signing_ciphertext,
         age_nonce,age_ciphertext,status,created_at,destroyed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            subject,
            signer.key_id,
            age_key_id,
            signing_nonce,
            signing_ciphertext,
            age_nonce,
            age_ciphertext,
            'active',
            wire(now),
            None,
        ),
        write=True,
    )


def _open(app, tx, subject, kind):
    row = tx.one(
        """SELECT signing_key_id,encryption_key_id,signing_nonce,signing_ciphertext,
        age_nonce,age_ciphertext,status FROM custodial_vault WHERE subject=?""",
        (subject,),
    )
    require(
        row is not None
        and (row[6] == 'active' or (kind == 'encryption' and row[6] == 'decrypt_only')),
        'custodial_vault_unavailable',
    )
    key_id, nonce, ciphertext = (
        (row[0], row[2], row[3]) if kind == 'identity' else (row[1], row[4], row[5])
    )
    require(nonce is not None and ciphertext is not None, 'custodial_vault_unavailable')
    try:
        return key_id, AESGCM(app._vault_key).decrypt(
            unb64(nonce, limit=12), unb64(ciphertext, limit=8192), _aad(subject, key_id, kind)
        )
    except (InvalidTag, ValueError, TypeError) as exc:
        raise Failure('custodial_vault_corrupt') from exc


def open_signer(app, tx, subject):
    key_id, secret = _open(app, tx, subject, 'identity')
    signer = Ed25519Signer.from_bytes(secret)
    require(signer.key_id == key_id, 'custodial_vault_corrupt')
    return signer


def open_age_identity(app, tx, subject):
    key_id, secret = _open(app, tx, subject, 'encryption')
    try:
        identity = secret.decode('ascii')
    except UnicodeError as exc:
        raise Failure('custodial_vault_corrupt') from exc
    from msg.security.age_keys import encryption_key_id, public_from_recipient

    recipient = recipient_from_identity(identity)
    require(
        encryption_key_id(public_from_recipient(recipient)) == key_id, 'custodial_vault_corrupt'
    )
    return identity


def rewrap_owned_age_ciphertext(identity, recipient, ciphertext):
    """Bounded age-to-age conversion; never expose the decrypted bytes to a caller."""
    from msg.security.age_keys import public_from_recipient

    public_from_recipient(recipient)
    require(
        type(ciphertext) is bytes
        and ciphertext.startswith(b'age-encryption.org/v1\n')
        and len(ciphertext) <= 1048576,
        'custodial_rewrap_ciphertext_invalid',
    )
    executable = shutil.which('age')
    require(executable is not None, 'age_dependency_unavailable')
    with tempfile.TemporaryDirectory(prefix='msg-custodial-rewrap-') as folder:
        secret = Path(folder) / 'identity'
        secret.write_bytes((identity + '\n').encode('ascii'))
        secret.chmod(0o600)
        try:
            opened = subprocess.run(
                [executable, '--decrypt', '--identity', str(secret)],
                input=ciphertext,
                capture_output=True,
                timeout=15,
                check=False,
            )
            require(
                opened.returncode == 0 and len(opened.stdout) <= 1048576,
                'custodial_rewrap_decrypt_failed',
            )
            sealed = subprocess.run(
                [executable, '--encrypt', '--recipient', recipient],
                input=opened.stdout,
                capture_output=True,
                timeout=15,
                check=False,
            )
            require(
                sealed.returncode == 0
                and sealed.stdout.startswith(b'age-encryption.org/v1\n')
                and len(sealed.stdout) <= 1114112,
                'custodial_rewrap_encrypt_failed',
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure('custodial_rewrap_operation_failed') from exc
        return sealed.stdout


def _upgrade_aad(subject, challenge_id):
    return canonical({
        'domain': 'msg-custodial-upgrade-ephemeral-v1',
        'subject': subject,
        'challenge_id': challenge_id,
    })


def seal_upgrade_private(app, subject, challenge_id, private):
    nonce = os.urandom(12)
    cipher = AESGCM(app._vault_key).encrypt(nonce, private, _upgrade_aad(subject, challenge_id))
    return b64(nonce), b64(cipher)


def open_upgrade_private(app, subject, challenge_id, nonce, ciphertext):
    try:
        raw = AESGCM(app._vault_key).decrypt(
            unb64(nonce, limit=12),
            unb64(ciphertext, limit=128),
            _upgrade_aad(subject, challenge_id),
        )
        return X25519PrivateKey.from_private_bytes(raw)
    except (InvalidTag, ValueError, TypeError) as exc:
        raise Failure('custodial_upgrade_challenge_corrupt') from exc


def server_upgrade_proof(app, subject, challenge, nonce, ciphertext):
    from msg.security.age_keys import public_from_recipient

    private = open_upgrade_private(app, subject, challenge['challenge_id'], nonce, ciphertext)
    shared = private.exchange(
        X25519PublicKey.from_public_bytes(public_from_recipient(challenge['encryption_recipient']))
    )
    return _proof(shared, challenge)


def seal_retired_encryption_key(app, tx, subject, old_key_id, new_recipient, challenge_id):
    """Export only this subject's retired age subkey, encrypted to its proved new key."""
    from msg.security.age_keys import encryption_key_id, public_from_recipient

    row = tx.one(
        'SELECT subject,recipient,retired_at,is_primary FROM encryption_subkeys WHERE key_id=?',
        (old_key_id,),
    )
    require(
        row is not None and row[0] == subject and row[2] is not None and row[3] == 0,
        'custodial_recovery_requires_retired_owned_key',
    )
    vault = tx.one('SELECT encryption_key_id FROM custodial_vault WHERE subject=?', (subject,))
    require(vault == (old_key_id,), 'custodial_recovery_key_mismatch')
    primary = tx.one(
        'SELECT key_id,recipient FROM encryption_subkeys WHERE subject=? AND is_primary=1',
        (subject,),
    )
    new_key_id = encryption_key_id(public_from_recipient(new_recipient))
    require(
        primary == (new_key_id, new_recipient) and new_key_id != old_key_id,
        'custodial_recovery_recipient_mismatch',
    )
    identity = open_age_identity(app, tx, subject)
    payload = {
        'format': 'msg-custodial-retired-key-v1',
        'purpose': 'retired-encryption-subkey-recovery',
        'subject_id': subject,
        'challenge_id': challenge_id,
        'encryption_key_id': old_key_id,
        'encryption_recipient': row[1],
        'age_identity': identity,
    }
    executable = shutil.which('age')
    require(executable is not None, 'age_dependency_unavailable')
    try:
        result = subprocess.run(
            [executable, '--encrypt', '--recipient', new_recipient],
            input=canonical(payload),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        raise Failure('custodial_recovery_encryption_failed') from None
    require(
        result.returncode == 0 and result.stdout.startswith(b'age-encryption.org/v1\n'),
        'custodial_recovery_encryption_failed',
    )
    return result.stdout
