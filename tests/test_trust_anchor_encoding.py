"""Pre-encoded immutable anchors still validate every current database fact."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from msg.core.codec import canonical
from msg.core.errors import Failure
from msg.security import certificates
from msg.security.crypto import Ed25519Signer


async def save_root(app, certificate):
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE certificates SET body=? WHERE id=?',
            (canonical(certificate).decode(), certificate.resource_id),
            write=True,
        )


async def validate_root(app):
    async with app.metadata.transaction(write=False) as tx:
        return await app.certificates.validate(app.certificates.root_certificate.resource_id, tx)


async def test_anchor_records_and_nested_constraints_cannot_mutate(installed):
    app, _ = installed
    anchor = app.certificates.root_certificate
    with pytest.raises(FrozenInstanceError):
        anchor.serial = 'changed'
    grant = replace(anchor.grants[0], constraints={'nested': {'depths': [1, 2]}})
    with pytest.raises(TypeError):
        grant.constraints['nested']['depths'][0] = 3
    with pytest.raises(TypeError):
        grant.constraints['nested']['depths'] = (3,)
    with pytest.raises(FrozenInstanceError):
        grant.scope.resource_id = 'different'


async def test_root_replacement_refreshes_bytes_and_requires_current_matching_database(installed):
    app, signer = installed
    before = app.certificates.root_certificate
    replacement = certificates.sign_certificate(
        replace(before, serial=before.serial + '-new'), signer
    )
    app.certificates.root_certificate = replacement
    assert app.certificates.root_certificate is replacement
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'trust_anchor_mismatch'
    await save_root(app, replacement)
    assert await validate_root(app) == replacement
    await save_root(app, before)
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'trust_anchor_mismatch'
    app.certificates.root_certificate = before
    assert await validate_root(app) == before


async def test_each_root_validation_still_verifies_signature_and_reads_current_revocation(
    installed, monkeypatch
):
    app, _ = installed
    anchor = app.certificates.root_certificate
    verified = []
    original = certificates.verify

    def verify(key, payload, signature, *, purpose):
        verified.append((key, payload, signature, purpose))
        return original(key, payload, signature, purpose=purpose)

    monkeypatch.setattr(certificates, 'verify', verify)
    assert await validate_root(app) == anchor
    assert await validate_root(app) == anchor
    assert (
        verified
        == [
            (
                app.certificates.root_public_key,
                canonical(certificates.certificate_body(anchor)),
                anchor.signature,
                'certificate',
            )
        ]
        * 2
    )
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE certificates SET revoked=1 WHERE id=?', (anchor.resource_id,), write=True
        )
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'certificate_revoked'
    assert len(verified) == 2


async def test_current_expiry_is_checked_after_successful_validation(installed, monkeypatch):
    app, _ = installed
    anchor = app.certificates.root_certificate
    assert await validate_root(app) == anchor
    monkeypatch.setattr(app.certificates, 'clock', lambda: anchor.expires_at + timedelta(seconds=1))
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'certificate_expired'


async def test_current_not_before_is_checked_after_successful_validation(installed, monkeypatch):
    app, _ = installed
    anchor = app.certificates.root_certificate
    assert await validate_root(app) == anchor
    monkeypatch.setattr(app.certificates, 'clock', lambda: anchor.not_before - timedelta(seconds=1))
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'certificate_expired'


async def test_signature_verification_keeps_the_current_root_public_key(installed, monkeypatch):
    app, _ = installed
    assert await validate_root(app) == app.certificates.root_certificate
    monkeypatch.setattr(app.certificates, 'root_public_key', Ed25519Signer.generate().public_key)
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'invalid_signature'


async def test_matching_anchor_signed_for_another_purpose_is_still_rejected(installed):
    app, signer = installed
    anchor = app.certificates.root_certificate
    wrong = replace(
        anchor,
        signature=signer.sign(canonical(certificates.certificate_body(anchor)), purpose='request'),
    )
    app.certificates.root_certificate = wrong
    await save_root(app, wrong)
    with pytest.raises(Failure) as failure:
        await validate_root(app)
    assert failure.value.code == 'invalid_signature'
