"""Task authority is checked through real signed requests and persisted facts."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, wire
from msg.core.models import Scope
from msg.core.requests import request_for
from msg.plugins.delegated_identity import possession_body
from msg.security.age_keys import generate_age_key
from msg.security.capabilities import grant_for
from msg.security.crypto import Ed25519Signer


def prepare(app, grants, **kwargs):
    key = Ed25519Signer.generate()
    _, recipient = generate_age_key()
    public = b64(key.public_key)
    return key, {
        'grantor': '@alice',
        'public_key': public,
        'encryption_recipient': recipient,
        'possession_proof': wire(
            key.sign(
                possession_body(app.settings.service_url, public, recipient, '@alice'),
                purpose='delegated-identity-v1',
            )
        ),
        'grants': wire(grants),
        'ttl': 600,
        **kwargs,
    }


async def setup(app, *, ops=('discovery.get@1',), capability='discovery.basic', **kwargs):
    owner_key, owner, _ = await register(app, 'alice')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'task target'},
        key=owner_key,
        subject=owner,
    )
    rid = post.resources[0].id
    grant = grant_for(
        app.registry.capability(capability), scope=Scope(resource_id=rid), operations=ops
    )
    task_key, args = prepare(app, (grant,), **kwargs)
    result = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert result.status == 'ok', wire(result)
    return owner_key, owner, rid, task_key, result


async def task_call(app, key, owner, grant, op='discovery.get', args=None, **kwargs):
    return await call(
        app,
        op,
        args or {'id': grant.data.get('target')},
        key=key,
        subject=owner,
        certs=(grant.data['certificate_id'],),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_scoped_identity_cannot_bypass_binding_and_revocation(installed):
    app, _ = installed
    owner_key, owner, rid, key, grant = await setup(app)
    assert grant.data['address'].startswith('@alice~')
    assert grant.data['depth'] == 0
    upgraded = await call(
        app,
        'identity.register',
        {
            'handle': 'permanent-worker',
            'public_key': b64(key.public_key),
            'encryption_recipient': grant.data['encryption_recipient'],
        },
        key=key,
        subject=grant.data['subject_id'],
        contract_version=2,
    )
    assert upgraded.error.code == 'delegated_identity_not_upgradable'
    allowed = await task_call(app, key, owner, grant, args={'id': rid})
    assert allowed.status == 'ok', wire(allowed)
    denied = await task_call(app, key, owner, grant, args={'id': '/main'})
    assert denied.error.code in {'credential_ceiling', 'delegation_scope'}
    omitted = await call(app, 'discovery.get', {'id': rid}, key=key, subject=owner)
    assert omitted.error.code == 'delegation_required'
    own = await call(
        app,
        'discovery.get',
        {'id': rid},
        key=key,
        subject=grant.data['subject_id'],
        certs=(grant.data['certificate_id'],),
    )
    assert own.error.code == 'delegated_subject_required'
    bad_operation = await task_call(
        app,
        key,
        owner,
        grant,
        'content.post_edit',
        {'id': rid, 'body': 'forbidden', 'expected_revision': 'unknown'},
    )
    assert bad_operation.error.code == 'credential_ceiling'
    revoked = await call(
        app,
        'identity.delegation_revoke',
        {'id': grant.data['delegation_id']},
        key=owner_key,
        subject=owner,
    )
    assert revoked.status == 'ok', wire(revoked)
    denied = await task_call(app, key, owner, grant, args={'id': rid})
    assert denied.error.code == 'authority_source_inactive'
    status = await call(
        app,
        'identity.delegated_get',
        {'id': '/' + grant.data['address']},
        key=owner_key,
        subject=owner,
    )
    assert status.status == 'ok' and not status.data['active'], wire(status)


@pytest.mark.asyncio
async def test_expiry_at_exact_deadline(installed):
    app, _ = installed
    owner_key, owner, rid, key, grant = await setup(app, ttl=60)
    cert_id = grant.data['certificate_id']
    async with app.metadata.transaction(write=False) as tx:
        credential = await tx.credential(key.key_id)
        cert = await tx.certificate(cert_id)
        assert credential.expires_at == cert.expires_at == NOW + timedelta(seconds=60)
    for service in (app.authenticator, app.certificates):
        service.clock = lambda: NOW + timedelta(seconds=60)
    packet = request_for(
        'discovery.get',
        {'id': rid},
        app.settings.service_url,
        signer=key,
        subject=owner,
        certificates=(cert_id,),
        expires_at=NOW + timedelta(seconds=120),
    )
    denied = await app.executor.execute(packet)
    assert denied.error.code == 'credential_expired'


@pytest.mark.asyncio
async def test_creator_key_revocation_invalidates_task(installed):
    app, _ = installed
    owner_key, owner, rid, key, grant = await setup(app)
    async with app.metadata.transaction(write=True) as tx:
        old = await tx.credential(owner_key.key_id)
        await tx.save_credential(replace(old, revoked_at=NOW), 0)
    denied = await task_call(app, key, owner, grant, args={'id': rid})
    assert denied.error.code == 'authority_source_inactive'


@pytest.mark.asyncio
async def test_one_success_retry_and_concurrent_calls(installed):
    import asyncio

    app, _ = installed
    _, owner, rid, key, grant = await setup(
        app,
        capability='resource.basic',
        ops=('content.post_edit@1',),
        max_uses=1,
    )
    invalid = await task_call(
        app, key, owner, grant, 'content.post_edit', {'id': '/main', 'body': 'wrong target'}
    )
    assert invalid.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(rid)
        revision, generation = resource.revision, resource.generation
    failed = await task_call(
        app,
        key,
        owner,
        grant,
        'content.post_edit',
        {'id': rid, 'body': 'failed task', 'expected_revision': 'invalid'},
        expected=((rid, generation),),
    )
    assert failed.error.code == 'revision_conflict'
    args = {'id': rid, 'body': 'completed task', 'expected_revision': revision}
    results = await asyncio.gather(
        *(
            task_call(
                app,
                key,
                owner,
                grant,
                'content.post_edit',
                args,
                rid=f'once-{i}',
                expected=((rid, generation),),
            )
            for i in range(2)
        )
    )
    assert sorted(r.status for r in results) == ['error', 'ok'], [
        (r.status, r.error.code if r.error else None) for r in results
    ]
    successful = next(r for r in results if r.status == 'ok')
    denied = next(r for r in results if r.status == 'error')
    assert denied.error.code == 'delegation_exhausted'
    replay = await task_call(
        app,
        key,
        owner,
        grant,
        'content.post_edit',
        args,
        rid=successful.request_id,
        expected=((rid, generation),),
    )
    assert replay.status == 'ok' and replay.replayed, wire(replay)
    async with app.metadata.transaction(write=False) as tx:
        binding = tx.setting('delegated_identity:' + grant.data['subject_id'])
        assert binding['uses'] == 1


@pytest.mark.asyncio
async def test_no_implicit_authority_or_key_reuse(installed):
    app, _ = installed
    owner_key, owner, rid, key, grant = await setup(app)
    overbroad = grant_for(
        app.registry.capability('discovery.basic'),
        scope=Scope(resource_id='/'),
        operations=('discovery.get@1',),
    )
    # An existing task cannot issue another task without explicit create authority.
    _, args = prepare(app, (overbroad,))
    denied = await task_call(app, key, owner, grant, 'identity.delegated_create', args)
    assert denied.error.code == 'credential_ceiling'
    public = b64(key.public_key)
    recipient = grant.data['encryption_recipient']
    args.update(
        public_key=public,
        encryption_recipient=recipient,
        possession_proof=wire(
            key.sign(
                possession_body(app.settings.service_url, public, recipient, '@alice'),
                purpose='delegated-identity-v1',
            )
        ),
    )
    duplicate = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert duplicate.error.code == 'key_exists'


@pytest.mark.asyncio
async def test_redelegation_requires_explicit_depth_and_never_widens(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'alice')
    read = grant_for(
        app.registry.capability('discovery.basic'),
        scope=Scope(resource_id=owner),
        operations=('discovery.get@1',),
    )
    create = grant_for(
        app.registry.capability('identity.basic'),
        scope=Scope(resource_id=owner),
        operations=('identity.delegated_create@1', 'identity.delegate@1'),
    )
    parent_key, args = prepare(app, (read, create), depth=1)
    parent = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert parent.status == 'ok', wire(parent)
    child_key, args = prepare(app, (read,), ttl=300)
    child = await task_call(app, parent_key, owner, parent, 'identity.delegated_create', args)
    assert child.status == 'ok', wire(child)
    allowed = await task_call(app, child_key, owner, child, args={'id': owner})
    assert allowed.status == 'ok', wire(allowed)
    _, longer = prepare(app, (read,), ttl=601)
    denied = await task_call(app, parent_key, owner, parent, 'identity.delegated_create', longer)
    assert denied.error.code == 'delegation_ttl_escalation'
    wider = replace(read, scope=Scope(resource_id=owner, descendants=True))
    _, args = prepare(app, (wider,))
    denied = await task_call(app, parent_key, owner, parent, 'identity.delegated_create', args)
    assert denied.error.code == 'credential_ceiling_escalation'
    # Create another parent with create authority but no permission to redelegate.
    leaf_key, args = prepare(app, (read, create), depth=0)
    leaf = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert leaf.status == 'ok', wire(leaf)
    _, args = prepare(app, (read,), ttl=300)
    denied = await task_call(app, leaf_key, owner, leaf, 'identity.delegated_create', args)
    assert denied.error.code == 'redelegation_forbidden'
    revoked = await call(
        app,
        'identity.delegation_revoke',
        {'id': parent.data['delegation_id']},
        key=owner_key,
        subject=owner,
    )
    assert revoked.status == 'ok', wire(revoked)
    denied = await task_call(app, child_key, owner, child, args={'id': owner})
    assert denied.error.code == 'authority_source_inactive'


@pytest.mark.asyncio
async def test_limited_creator_cannot_issue_extra_operations_and_bad_proof_rolls_back(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'alice')
    create = grant_for(
        app.registry.capability('identity.basic'),
        scope=Scope(resource_id=owner),
        operations=('identity.delegated_create@1',),
    )
    key = Ed25519Signer.generate()
    from msg.core.codec import canonical

    added = await call(
        app,
        'identity.key_add',
        {
            'public_key': b64(key.public_key),
            'ceiling': wire((create,)),
            'possession_proof': wire(
                key.sign(
                    canonical({'subject_id': owner, 'public_key': b64(key.public_key)}),
                    purpose='key-add',
                )
            ),
        },
        key=owner_key,
        subject=owner,
    )
    assert added.status == 'ok', wire(added)
    read = grant_for(
        app.registry.capability('discovery.basic'),
        scope=Scope(resource_id=owner),
        operations=('discovery.get@1',),
    )
    _, args = prepare(app, (read,))
    denied = await call(app, 'identity.delegated_create', args, key=key, subject=owner)
    assert denied.error.code == 'credential_ceiling_escalation'
    bad_key, args = prepare(app, (read,))
    args['possession_proof'] = wire(owner_key.sign(b'invalid', purpose='delegated-identity-v1'))
    denied = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert denied.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT id FROM credentials WHERE id=?', (bad_key.key_id,)) is None
    _, args = prepare(app, (read,), max_uses=1)
    denied = await call(app, 'identity.delegated_create', args, key=owner_key, subject=owner)
    assert denied.error.code == 'limited_delegation_operation'


@pytest.mark.asyncio
async def test_public_possession_request_is_pinned_to_grantor(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'alice')
    bob_key, bob, _ = await register(app, 'bob')
    read = grant_for(
        app.registry.capability('discovery.basic'),
        scope=Scope(resource_id=bob),
        operations=('discovery.get@1',),
    )
    worker, args = prepare(app, (read,))
    denied = await call(app, 'identity.delegated_create', args, key=bob_key, subject=bob)
    assert denied.error.code == 'delegated_grantor_mismatch'
    args['grantor'] = '@bob'
    denied = await call(app, 'identity.delegated_create', args, key=bob_key, subject=bob)
    assert denied.status == 'error'  # Changing the issuer breaks the possession signature.
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT id FROM credentials WHERE id=?', (worker.key_id,)) is None


@pytest.mark.asyncio
async def test_backup_round_trip_preserves_deadline_revocation_and_authority_proof(
    installed,
    tmp_path,
    pg_dsn,
):
    import psycopg

    from msg.admin.backups import backup, restore
    from msg.admin.recovery_proof import capture
    from msg.core.codec import loads

    app, root = installed
    owner_key, owner, _, key, task = await setup(app)
    revoked = await call(
        app,
        'identity.delegation_revoke',
        {'id': task.data['delegation_id']},
        key=owner_key,
        subject=owner,
    )
    assert revoked.status == 'ok'
    proof = await capture(app, root, source_backup_sha256='a' * 64, sequence=1)
    assert proof['state']
    async with app.metadata.transaction(write=False) as tx:
        binding = tx.setting('delegated_identity:' + task.data['subject_id'])
        delegation = tx.setting('delegation:' + task.data['delegation_id'])
        credential = wire(await tx.credential(key.key_id))
    archive = tmp_path / 'task.zip'
    await backup(app, archive)
    restored = restore(
        archive, tmp_path / 'restored-etc', tmp_path / 'restored-data', postgres_dsn=pg_dsn
    )
    assert restored['promotion'] == 'blocked'
    with psycopg.connect(pg_dsn) as connection:
        for name, expected in (
            ('delegated_identity:' + task.data['subject_id'], binding),
            ('delegation:' + task.data['delegation_id'], delegation),
        ):
            row = connection.execute('SELECT value FROM settings WHERE key=%s', (name,)).fetchone()
            assert loads(row[0]) == expected
        row = connection.execute(
            'SELECT body FROM credentials WHERE id=%s', (key.key_id,)
        ).fetchone()
        assert loads(row[0]) == credential
        row = connection.execute(
            'SELECT body FROM resources WHERE id=%s', (task.data['delegation_id'],)
        ).fetchone()
        assert loads(row[0])['state'] == 'archived'
