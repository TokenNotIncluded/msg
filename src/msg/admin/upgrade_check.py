"""Isolated selftest of the real upgrade commit and new-key recovery boundary."""

import hashlib
import os
from datetime import timedelta
from uuid import uuid4

from msg.core.codec import b64, canonical, unb64, wire
from msg.core.requests import request_for
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer


async def check_upgrade_recovery(app, now):
    signer = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    nonce, recovery = b64(os.urandom(32)), b64(os.urandom(32))
    subject = 'u_tmp_' + hashlib.sha256(unb64(nonce)).hexdigest()[:32]
    request_id = uuid4().hex
    proof = signer.sign(
        canonical({
            'subject_id': subject,
            'nonce': nonce,
            'public_key': b64(signer.public_key),
            'encryption_recipient': recipient,
            'request_id': request_id,
        }),
        purpose='temporary-key-possession-v1',
    )
    created = await app.executor.execute(
        request_for(
            'identity.temporary',
            {
                'nonce': nonce,
                'recovery_secret': recovery,
                'public_key': b64(signer.public_key),
                'encryption_recipient': recipient,
                'possession_proof': wire(proof),
            },
            app.settings.service_url,
            request_id=request_id,
            contract_version=3,
            expires_at=now + timedelta(seconds=120),
        )
    )
    if created.status != 'ok':
        return False
    token = created.data['credential_id'], unb64(created.data['token'])
    upgrade_id = uuid4().hex
    signed = {
        'subject_id': subject,
        'handle': 'selftest-upgrade',
        'public_key': b64(signer.public_key),
        'encryption_recipient': recipient,
    }
    arguments = {key: value for key, value in signed.items() if key != 'subject_id'}
    arguments['possession_proof'] = wire(signer.sign(canonical(signed), purpose='upgrade'))
    committed = await app.executor.execute(
        request_for(
            'identity.upgrade',
            arguments,
            app.settings.service_url,
            subject=subject,
            token=token,
            request_id=upgrade_id,
            contract_version=2,
            expires_at=now + timedelta(seconds=120),
        )
    )
    if committed.status != 'ok':
        return False
    async with app.metadata.transaction(write=False) as tx:
        before = tuple(
            tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('events', 'results', 'credentials', 'certificates')
        )
    # No old token, bootstrap nonce, original response or cached certificate is
    # supplied to this signed query. The stored result is only a lookup target.
    recovered = await app.executor.execute(
        request_for(
            'identity.upgrade_result',
            {'upgrade_request_id': upgrade_id},
            app.settings.service_url,
            subject=subject,
            signer=signer,
            expires_at=now + timedelta(seconds=120),
        )
    )
    denied = await app.executor.execute(
        request_for(
            'identity.upgrade_result',
            {'upgrade_request_id': upgrade_id},
            app.settings.service_url,
            subject=subject,
            token=token,
            expires_at=now + timedelta(seconds=120),
        )
    )
    async with app.metadata.transaction(write=False) as tx:
        after = tuple(
            tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('events', 'results', 'credentials', 'certificates')
        )
    return (
        recovered.status == 'ok'
        and recovered.subject == subject
        and recovered.data['key_id'] == signer.key_id
        and recovered.data['status'] == 'completed'
        and recovered.data['certificate_id'] == committed.data['certificate_id']
        and denied.status == 'error'
        and denied.error.code == 'credential_revoked'
        and before == after
    )
