"""Local Root approval repairs only Agent Link authority, atomically and with a signed audit."""

from dataclasses import replace
from datetime import timedelta

import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_service import NOW, call, register

from msg.admin.link_authority import LINK_OPERATIONS, link_preview, repair_link_authority
from msg.constants import ROOT_SUBJECT
from msg.core.codec import b64, canonical, decode, digest, loads, wire
from msg.core.errors import Failure
from msg.core.models import Signature
from msg.security.crypto import Ed25519Signer, verify
from msg.storage.postgres import PostgresSession


async def old_credential(app, key, subject):
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        narrowed = replace(
            credential,
            ceiling=tuple(
                replace(grant, operations=grant.operations - LINK_OPERATIONS)
                for grant in credential.ceiling
            ),
        )
        await tx.save_credential(narrowed, (await tx.subject(subject)).auth_version)
    return narrowed


async def preview(app, subject, *, key_id=None):
    async with app.metadata.transaction(write=False) as tx:
        return await link_preview(app, tx, subject, key_id=key_id)


async def sequences(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            name: tuple(
                tx.one('SELECT last_value,is_called FROM "' + name.replace('"', '""') + '"')
            )
            for (name,) in tx.rows(
                "SELECT sequencename FROM pg_sequences WHERE schemaname='public' "
                'ORDER BY sequencename'
            )
        }


async def authority(app, subject, key):
    async with app.metadata.transaction(write=False) as tx:
        return {
            'credential': await tx.credential(key.key_id),
            'subject': await tx.subject(subject),
            'user': await tx.resource(subject),
            'epoch': tx.setting('authorization_epoch', 0),
            'certificates': tuple(tx.rows('SELECT body FROM certificates ORDER BY id')),
        }


@pytest.mark.asyncio
async def test_link_repair_preview_is_readonly_and_original_key_works_with_signed_audit(
    installed, monkeypatch
):
    app, root = installed
    key, subject, certificate = await register(app, 'link-repair-owner')
    old = await old_credential(app, key, subject)
    denied = await call(
        app,
        'identity.link_open',
        {'invite_id': 'a' * 32, 'name': 'reviewer', 'minutes': 30},
        key=key,
        subject=subject,
        certs=(certificate,),
    )
    assert denied.status == 'error' and denied.error.code == 'credential_ceiling'
    before = await authority(app, subject, key)
    sequence_before = await sequences(app)
    async with readonly_evidence(app, monkeypatch):
        plan = await preview(app, subject)
    assert await sequences(app) == sequence_before
    assert plan['key_id'] == key.key_id
    assert plan['before_credential_digest'] == digest(old)
    assert plan['auth_version'] == before['subject'].auth_version
    assert set(plan['additions']) == LINK_OPERATIONS

    await repair_link_authority(
        app,
        subject,
        root,
        expected_digest=digest(plan),
        operator='isolated-link-repair-test',
    )
    after = await authority(app, subject, key)
    expected = replace(
        old,
        ceiling=tuple(
            replace(grant, operations=grant.operations | LINK_OPERATIONS)
            if grant.capability == 'identity.basic' and grant.version == 1
            else grant
            for grant in old.ceiling
        ),
    )
    assert after['credential'] == expected
    assert after['subject'] == replace(
        before['subject'], auth_version=before['subject'].auth_version + 1
    )
    assert after['user'] == before['user']
    assert after['epoch'] == before['epoch'] + 1
    assert after['certificates'] == before['certificates']
    async with app.metadata.transaction(write=False) as tx:
        audit = loads(tx.one('SELECT body FROM audit ORDER BY seq DESC LIMIT 1')[0])
    event = audit['event']
    assert event['actor'] == event['subject'] == ROOT_SUBJECT
    statement = dict(event['data'])
    signature = decode(Signature, statement.pop('signature'))
    assert statement['subject_id'] == subject
    assert statement['key_id'] == key.key_id
    assert statement['before_credential_digest'] == digest(old)
    assert statement['operator'] == 'isolated-link-repair-test'
    verify(root.public_key, canonical(statement), signature, purpose='account-admin')

    # An already repaired account does not accumulate new administrative audits.
    current = await preview(app, subject)
    assert not current['additions']
    unchanged = await business_snapshot(app)
    sequence_after = await sequences(app)
    await repair_link_authority(
        app, subject, root, expected_digest=digest(current), operator='repeat-test'
    )
    assert await business_snapshot(app) == unchanged
    assert await sequences(app) == sequence_after

    # The same key and certificate can open, inspect and close the invitation.
    for operation, arguments in (
        ('identity.link_open', {'invite_id': 'a' * 32, 'name': 'reviewer', 'minutes': 30}),
        ('identity.link_pending', {}),
        ('identity.link_close', {'invite_id': 'a' * 32}),
    ):
        result = await call(
            app, operation, arguments, key=key, subject=subject, certs=(certificate,)
        )
        assert result.status == 'ok', result


@pytest.mark.asyncio
async def test_link_repair_rejects_unsafe_targets_and_stale_preview_without_writes(installed):
    app, root = installed
    key, subject, _ = await register(app, 'link-repair-guards')
    original = await old_credential(app, key, subject)
    secondary, derived = Ed25519Signer.generate(), Ed25519Signer.generate()
    token_id = 't_link_repair_test_token'
    async with app.metadata.transaction(write=True) as tx:
        auth_version = (await tx.subject(subject)).auth_version
        for signer, source in ((secondary, None), (derived, original.id)):
            await tx.save_credential(
                replace(
                    original,
                    id=signer.key_id,
                    verifier=signer.public_key,
                    source_credential_id=source,
                ),
                auth_version,
            )
            tx.execute(
                'INSERT INTO identity_keys VALUES (?,?,?,?,?,?)',
                (signer.key_id, subject, b64(signer.public_key), wire(NOW), None, 0),
                write=True,
            )
        await tx.save_credential(
            replace(
                original, id=token_id, kind='token', verifier=b'fixture-token-digest'.ljust(32)
            ),
            auth_version,
        )
    plan = await preview(app, subject, key_id=key.key_id)
    initial = await business_snapshot(app)
    with pytest.raises(Failure, match='root_key_mismatch'):
        await repair_link_authority(
            app,
            subject,
            Ed25519Signer.generate(),
            key_id=key.key_id,
            expected_digest=digest(plan),
            operator='wrong-root-test',
        )
    assert await business_snapshot(app) == initial
    async with app.metadata.transaction(write=True) as tx:
        identity = await tx.subject(subject)
        await tx.update_identity(
            replace(identity, auth_version=identity.auth_version + 1), identity.auth_version
        )
    stale_before = await business_snapshot(app)
    assert digest(await preview(app, subject, key_id=key.key_id)) != digest(plan)
    with pytest.raises(Failure, match='link_repair_preview_changed'):
        await repair_link_authority(
            app,
            subject,
            root,
            key_id=key.key_id,
            expected_digest=digest(plan),
            operator='stale-test',
        )
    assert await business_snapshot(app) == stale_before

    cases = (
        ('revoked', {'revoked_at': NOW}),
        ('expired', {'not_before': NOW - timedelta(seconds=1), 'expires_at': NOW}),
        ('not-yet-active', {'not_before': NOW + timedelta(seconds=1)}),
        ('token', {}),
        ('derived', {}),
        ('secondary', {}),
        ('delegated', {}),
        (
            'missing-identity-grant',
            {
                'ceiling': tuple(
                    grant for grant in original.ceiling if grant.capability != 'identity.basic'
                )
            },
        ),
    )
    for label, changes in cases:
        selected_key = {
            'token': token_id,
            'derived': derived.key_id,
            'secondary': secondary.key_id,
        }.get(label, key.key_id)
        primary_key = derived.key_id if label == 'derived' else key.key_id
        async with app.metadata.transaction(write=True) as tx:
            await tx.save_credential(
                replace(original, **changes), (await tx.subject(subject)).auth_version
            )
            tx.execute(
                'UPDATE identity_keys SET is_primary=0 WHERE subject=?',
                (subject,),
                write=True,
            )
            tx.execute(
                'UPDATE identity_keys SET is_primary=1 WHERE key_id=? AND subject=?',
                (primary_key, subject),
                write=True,
            )
            if label == 'delegated':
                tx.set_setting('delegated_identity:' + subject, {'grantor': subject})
            else:
                tx.execute(
                    'DELETE FROM settings WHERE key=?',
                    ('delegated_identity:' + subject,),
                    write=True,
                )
        before = await business_snapshot(app)
        sequence_before = await sequences(app)
        with pytest.raises(Failure) as preview_error:
            await preview(app, subject, key_id=selected_key)
        with pytest.raises(Failure) as apply_error:
            await repair_link_authority(
                app,
                subject,
                root,
                key_id=selected_key,
                expected_digest=digest(plan),
                operator='reject-' + label,
            )
        assert apply_error.value.code == preview_error.value.code
        assert await business_snapshot(app) == before, label
        assert await sequences(app) == sequence_before, label

    before = await business_snapshot(app)
    with pytest.raises(Failure):
        await preview(app, ROOT_SUBJECT)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_link_repair_audit_failure_rolls_back_all_authority_changes(installed, monkeypatch):
    app, root = installed
    key, subject, _ = await register(app, 'link-repair-rollback')
    await old_credential(app, key, subject)
    plan = await preview(app, subject)
    original = await authority(app, subject, key)
    before = await business_snapshot(app)
    calls = []

    async def fail_audit(tx, audit):
        calls.append(audit)
        credential = await tx.credential(key.key_id)
        assert LINK_OPERATIONS <= next(
            grant.operations for grant in credential.ceiling if grant.capability == 'identity.basic'
        )
        assert (await tx.subject(subject)).auth_version == original['subject'].auth_version + 1
        assert tx.setting('authorization_epoch', 0) == original['epoch'] + 1
        raise RuntimeError('injected administrative audit failure')

    monkeypatch.setattr(PostgresSession, 'append_audit', fail_audit)
    with pytest.raises(RuntimeError, match='injected administrative audit failure'):
        await repair_link_authority(
            app, subject, root, expected_digest=digest(plan), operator='rollback-test'
        )
    assert len(calls) == 1
    assert await business_snapshot(app) == before
    assert await authority(app, subject, key) == original


@pytest.mark.asyncio
async def test_repaired_old_primary_completes_one_paste_link(installed, tmp_path, monkeypatch):
    import httpx
    from test_client_link import args, prepared_link

    from msg.client import MsgClient
    from msg.client_link import run_command
    from msg.client_subagents_remote import RemoteAgents
    from msg.transports.http import create_app

    app, root = installed
    original_register = MsgClient.register

    async def register_old_owner(client, handle):
        result = await original_register(client, handle)
        assert result.status == 'ok'
        await old_credential(app, client.state.signer, client.state.subject)
        plan = await preview(app, client.state.subject)
        await repair_link_authority(
            app, client.state.subject, root, expected_digest=digest(plan), operator='link-e2e-test'
        )
        return result

    monkeypatch.setattr(MsgClient, 'register', register_old_owner)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        owner, helper, invited, _ = await prepared_link(app, tmp_path, http)
        watched = await run_command(owner, args('watch', once=True, interval=0.1))
        assert watched['data']['approved'] == ['reviewer']
        accepted = await run_command(
            helper, args('join', code=invited['data']['invite_code'], wait=True)
        )
        assert accepted['data']['task']['message'] == 'Review the parser patch and report.'
        await RemoteAgents(helper).send(
            'reviewer', 'reviewer-lead', 'LINK_TEST_READY', message_id='ready'
        )
        await run_command(owner, args('revoke', name='reviewer'))
        denied = await helper.call('discovery.get', {'id': '/@alice/files/agents/reviewer'})
        assert denied.error.code == 'authority_source_inactive'
