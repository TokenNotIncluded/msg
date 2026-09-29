"""A deployment upgrade cannot rewrite existing signed CA policy."""

from dataclasses import replace

import pytest
from test_service import NOW, call

from msg.admin.diagnostics import authority_snapshot_drift, doctor
from msg.core.codec import b64, canonical, wire
from msg.security.age_keys import generate_age_key
from msg.security.certificates import sign_certificate
from msg.security.crypto import Ed25519Signer, subject_id
from msg.storage.git import durable_write


def without_new_share_authority(grants):
    result = []
    for grant in grants:
        if grant.capability == 'sharing.basic':
            continue
        if grant.capability == 'system.config':
            grant = replace(grant, operations=grant.operations - {'system.share_links_set@1'})
        result.append(grant)
    return tuple(result)


@pytest.mark.asyncio
async def test_old_signed_ca_snapshots_are_diagnosed_without_repair(installed):
    app, root_key = installed
    async with app.metadata.transaction(write=True) as tx:
        root = await tx.certificate(app.certificates.root_certificate.resource_id)
        online_id = tx.setting('online_ca_certificate')
        online = await tx.certificate(online_id)
        old_root = sign_certificate(
            replace(
                root,
                grants=without_new_share_authority(root.grants),
                issuance=replace(
                    root.issuance,
                    issue_grants=without_new_share_authority(root.issuance.issue_grants),
                ),
            ),
            root_key,
        )
        old_online = sign_certificate(
            replace(
                online,
                issuance=replace(
                    online.issuance,
                    issue_grants=without_new_share_authority(online.issuance.issue_grants),
                ),
            ),
            root_key,
        )
        tx.execute(
            'UPDATE certificates SET body=? WHERE id=?',
            (canonical(old_root).decode(), root.resource_id),
            write=True,
        )
        tx.execute(
            'UPDATE certificates SET body=? WHERE id=?',
            (canonical(old_online).decode(), online_id),
            write=True,
        )
    durable_write(
        app.settings.trust_file,
        canonical({
            'version': 1,
            'public_key': b64(root_key.public_key),
            'certificate': wire(old_root),
        }),
        mode=0o444,
    )
    app.certificates.root_certificate = old_root
    async with app.metadata.transaction(write=False) as tx:
        before = await authority_snapshot_drift(app, old_root, old_online, tx)
        share = next(g for g in app.base_grants() if g.capability == 'sharing.basic')
        assert not await app.certificates.allowed_issuance(share, old_root.issuance, tx)
        assert not await app.certificates.allowed_issuance(share, old_online.issuance, tx)
        assert {item['capability'] for item in before['root_use']} >= {
            'sharing.basic',
            'system.config',
        }
        assert {item['capability'] for item in before['root_issue']} >= {
            'sharing.basic',
            'system.config',
        }
        assert {item['capability'] for item in before['online_issue']} >= {'sharing.basic'}
        assert all(item['capability'] != 'system.config' for item in before['online_issue'])
        assert next(item for item in before['root_use'] if item['capability'] == 'system.config')[
            'operations'
        ] == ['system.share_links_set@1']
        old_rows = tuple(tx.rows('SELECT id,body FROM certificates ORDER BY id'))
    old_trust = app.settings.trust_file.read_bytes()
    diagnosed = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert diagnosed['checks']['root_trust']['ok'], diagnosed
    assert diagnosed['checks']['online_ca']['ok'], diagnosed
    assert diagnosed['checks']['authority_snapshot']['code'] == 'signed_authority_outdated'
    assert diagnosed['checks']['authority_snapshot']['missing'] == before
    assert 'rotation invalidates old chains' in diagnosed['checks']['authority_snapshot']['action']
    assert not diagnosed['ok']
    diagnosed_again = doctor(app.settings.config_dir, clock=lambda: NOW)
    assert (
        diagnosed_again['checks']['authority_snapshot'] == diagnosed['checks']['authority_snapshot']
    )
    async with app.metadata.transaction(write=False) as tx:
        assert tuple(tx.rows('SELECT id,body FROM certificates ORDER BY id')) == old_rows
    assert app.settings.trust_file.read_bytes() == old_trust
    # A new identity would need the current base grants. The old issuer cannot
    # silently obtain sharing.basic merely because the code now knows it.
    applicant = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    result = await call(
        app,
        'identity.register',
        {
            'handle': 'old-policy-applicant',
            'public_key': b64(applicant.public_key),
            'encryption_recipient': recipient,
        },
        key=applicant,
        subject=subject_id(applicant.public_key),
        contract_version=2,
    )
    assert result.status == 'error' and result.error.code == 'issuance_scope_exceeded', wire(result)
    async with app.metadata.transaction(write=False) as tx:
        assert tuple(tx.rows('SELECT id,body FROM certificates ORDER BY id')) == old_rows
