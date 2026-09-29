"""A stored token-only temporary subject upgrades without losing ownership."""

import hashlib
import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_service import NOW, call

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.models import Credential
from msg.plugins import identity
from msg.security.age_keys import generate_age_key
from msg.security.crypto import Ed25519Signer


@pytest.mark.asyncio
async def test_legacy_temporary_upgrade_is_atomic_and_preserves_subject(installed, monkeypatch):
    app, _ = installed
    subject = 'u_tmp_legacy_' + uuid4().hex[:20]
    token_bytes = os.urandom(32)
    token_id = 't_' + subject[2:]
    async with app.metadata.transaction(write=True) as tx:
        await identity.make_user(
            app, tx, SimpleNamespace(now=NOW), subject, 'tmp-' + subject[-20:], 'temporary'
        )
        await tx.save_credential(
            Credential(
                id=token_id,
                subject_id=subject,
                kind='token',
                verifier=hashlib.sha256(token_bytes).digest(),
                ceiling=app.temporary_ceiling(),
                not_before=NOW,
                expires_at=NOW + timedelta(hours=1),
                revoked_at=None,
            ),
            0,
        )
    token = (token_id, token_bytes)
    post = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'legacy temporary work'},
        subject=subject,
        token=token,
    )
    assert post.status == 'ok', wire(post)
    owner_ref = post.resources[0].id
    signer = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    signed = {
        'subject_id': subject,
        'public_key': b64(signer.public_key),
        'handle': 'legacy-promoted',
        'encryption_recipient': recipient,
    }
    args = {
        'handle': 'legacy-promoted',
        'public_key': signed['public_key'],
        'encryption_recipient': recipient,
        'possession_proof': wire(signer.sign(canonical(signed), purpose='upgrade')),
    }
    original = identity.issue_online

    async def fail_after_identity_writes(*_args, **_kwargs):
        raise Failure('forced_issue_failure')

    monkeypatch.setattr(identity, 'issue_online', fail_after_identity_writes)
    failed = await call(
        app, 'identity.upgrade', args, subject=subject, token=token, contract_version=2
    )
    assert failed.error.code == 'forced_issue_failure'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind == 'temporary'
        assert (await tx.resource(owner_ref)).owner == subject
        assert tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (subject,))[0] == 0
        assert tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?', (subject,))[0] == 0
        assert (await tx.credential(token_id)).revoked_at is None
    monkeypatch.setattr(identity, 'issue_online', original)
    upgraded = await call(
        app, 'identity.upgrade', args, subject=subject, token=token, contract_version=2
    )
    assert upgraded.status == 'ok', wire(upgraded)
    assert upgraded.data['subject_id'] == subject
    assert upgraded.data['encryption_recipient'] == recipient
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.subject(subject)).kind == 'registered'
        assert (await tx.resource(owner_ref)).owner == subject
        assert tx.one('SELECT COUNT(*) FROM identity_keys WHERE subject=?', (subject,))[0] == 1
        key = tx.one(
            'SELECT key_id,public_key FROM identity_keys WHERE subject=? AND is_primary=1',
            (subject,),
        )
        assert key == (upgraded.data['key_id'], b64(signer.public_key))
        assert tx.one('SELECT COUNT(*) FROM encryption_subkeys WHERE subject=?', (subject,))[0] == 1
        encryption = tx.one(
            'SELECT key_id,recipient,public_key FROM encryption_subkeys WHERE subject=? AND is_primary=1',
            (subject,),
        )
        assert encryption == (
            upgraded.data['encryption_key_id'],
            recipient,
            b64(identity.public_from_recipient(recipient)),
        )
        assert (await tx.credential(token_id)).revoked_at is not None
    denied = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'legacy token after upgrade'},
        subject=subject,
        token=token,
    )
    assert denied.error.code == 'credential_revoked'
    signed_post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'migrated signing key'},
        key=signer,
        subject=subject,
    )
    assert signed_post.status == 'ok', wire(signed_post)
    repeated = await call(
        app, 'identity.upgrade', args, subject=subject, token=token, contract_version=2
    )
    assert repeated.error.code == 'credential_revoked'
