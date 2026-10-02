"""Ordinary traversal avoids redundant chains without weakening live access checks."""

from dataclasses import replace

import pytest
from test_authorization import approve, scoped
from test_authorization_sources import grant
from test_service import call, register

from msg.core.codec import canonical, wire


def ok(result):
    assert result.status == 'ok', wire(result)
    return result


async def nested_post(app, owner, mode=0o777):
    topic = (
        ok(
            await call(
                app,
                'content.topic_create',
                {'parent': '/main', 'name': 'traversal-cost'},
                key=owner[0],
                subject=owner[1],
            )
        )
        .resources[0]
        .id
    )
    post = (
        ok(
            await call(
                app,
                'content.post_create',
                {'parent': topic, 'body': 'read through live traversal'},
                key=owner[0],
                subject=owner[1],
            )
        )
        .resources[0]
        .id
    )
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(topic)
        await tx.replace(
            replace(resource, mode=mode, generation=resource.generation + 1), resource.generation
        )
    return topic, post


def track_certificate_entries(app, monkeypatch):
    entries = []
    validate = app.certificates.validate

    async def tracked(cid, session, **kwargs):
        if kwargs.get('seen') is None:
            entries.append(cid)
        return await validate(cid, session, **kwargs)

    monkeypatch.setattr(app.certificates, 'validate', tracked)
    return entries


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [0o777, 0o711, 0o700])
async def test_ordinary_traversal_validates_attached_chain_once(installed, monkeypatch, mode):
    app, _ = installed
    owner = await register(app, 'traversal-owner')
    reader = owner if mode == 0o700 else await register(app, 'traversal-reader')
    _, post = await nested_post(app, owner, mode)
    entries = track_certificate_entries(app, monkeypatch)

    result = await call(
        app,
        'discovery.get',
        {'id': post, 'fields': ['id']},
        key=reader[0],
        subject=reader[1],
        certs=(reader[2],),
    )

    assert ok(result).data['id'] == post
    assert entries == [reader[2]]


@pytest.mark.asyncio
async def test_direct_share_traversal_needs_no_override_chain(installed, monkeypatch):
    app, _ = installed
    owner = await register(app, 'share-traversal-owner')
    reader = await register(app, 'share-traversal-reader')
    _, post = await nested_post(app, owner, 0o700)
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(post)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    await grant(app, owner, post, reader[1])
    entries = track_certificate_entries(app, monkeypatch)

    result = await call(
        app,
        'discovery.get',
        {'id': post, 'fields': ['id']},
        key=reader[0],
        subject=reader[1],
        certs=(reader[2],),
    )

    assert ok(result).data['id'] == post
    assert entries == [reader[2]]


@pytest.mark.asyncio
async def test_private_ancestor_still_requires_live_scoped_override(installed, monkeypatch):
    app, root = installed
    owner = await register(app, 'override-traversal-owner')
    reader = await register(app, 'override-traversal-reader')
    topic, post = await nested_post(app, owner, 0o700)
    denied = await call(
        app, 'discovery.get', {'id': post}, key=reader[0], subject=reader[1], certs=(reader[2],)
    )
    assert denied.error.code == 'permission_denied'
    override = await approve(
        app,
        root,
        reader[1],
        reader[0],
        (scoped(app, 'resource.read_override', topic, ('discovery.get@1',), True),),
    )
    entries = track_certificate_entries(app, monkeypatch)
    result = await call(
        app,
        'discovery.get',
        {'id': post, 'fields': ['id']},
        key=reader[0],
        subject=reader[1],
        certs=(override.resource_id,),
    )
    assert ok(result).data['id'] == post
    assert len(entries) > 1 and set(entries) == {override.resource_id}
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'UPDATE certificates SET revoked=1 WHERE id=?', (override.resource_id,), write=True
        )
    revoked = await call(
        app,
        'discovery.get',
        {'id': post},
        key=reader[0],
        subject=reader[1],
        certs=(override.resource_id,),
    )
    assert revoked.error.code == 'certificate_revoked'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['revoked', 'signature'])
async def test_public_read_rejects_invalid_attached_certificate(installed, failure):
    app, _ = installed
    reader = await register(app, 'invalid-traversal-reader')
    async with app.metadata.transaction(write=True) as tx:
        if failure == 'revoked':
            tx.execute('UPDATE certificates SET revoked=1 WHERE id=?', (reader[2],), write=True)
        else:
            certificate = await tx.certificate(reader[2])
            signature = replace(certificate.signature, value=b'\x00' * 64)
            tx.execute(
                'UPDATE certificates SET body=? WHERE id=?',
                (canonical(replace(certificate, signature=signature)).decode(), reader[2]),
                write=True,
            )

    result = await call(
        app,
        'discovery.get',
        {'id': '/AGENTS.md'},
        key=reader[0],
        subject=reader[1],
        certs=(reader[2],),
    )
    assert result.error.code == (
        'certificate_revoked' if failure == 'revoked' else 'invalid_signature'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['preview', 'inactive', 'dm'])
async def test_ordinary_modes_do_not_bypass_private_or_inactive_boundaries(installed, boundary):
    app, _ = installed
    owner = await register(app, 'boundary-traversal-owner')
    reader = await register(app, 'boundary-traversal-reader')
    if boundary == 'dm':
        recipient = await register(app, 'boundary-traversal-recipient')
        requested = ok(
            await call(
                app,
                'communication.dm_request',
                {'recipient': recipient[1], 'introduction': 'must stay private'},
                key=owner[0],
                subject=owner[1],
                contract_version=2,
            )
        )
        topic = requested.data['conversation_id']
        post = requested.data['introduction_ref']['id']
        async with app.metadata.transaction(write=True) as tx:
            for rid in (topic, post):
                resource = await tx.resource(rid)
                await tx.replace(
                    replace(resource, mode=0o777, generation=resource.generation + 1),
                    resource.generation,
                )
    else:
        topic, post = await nested_post(app, owner)
        async with app.metadata.transaction(write=True) as tx:
            if boundary == 'preview':
                tx.set_setting('hosting_preview:' + topic, {'owner': owner[1]})
            else:
                resource = await tx.resource(topic)
                await tx.replace(
                    replace(resource, state='archived', generation=resource.generation + 1),
                    resource.generation,
                )

    result = await call(
        app,
        'discovery.get',
        {'id': post},
        key=reader[0],
        subject=reader[1],
        certs=(reader[2],),
    )
    assert result.error.code == (
        'ancestor_inactive' if boundary == 'inactive' else 'permission_denied'
    )


@pytest.mark.asyncio
async def test_minimal_tool_traversal_uses_only_its_tool_certificate(installed, monkeypatch):
    app, root = installed
    reader = await register(app, 'tool-traversal-reader')
    certificate = await approve(
        app,
        root,
        reader[1],
        reader[0],
        (scoped(app, 'tool.use', 'tool_dns', app.registry.capability('tool.use').operations),),
    )
    override_checks = []
    has = app.authorizer.has

    async def tracked(principal, capability, operation, rid, tx):
        if capability == 'resource.read_override':
            override_checks.append(rid)
        return await has(principal, capability, operation, rid, tx)

    monkeypatch.setattr(app.authorizer, 'has', tracked)
    result = await call(
        app,
        'discovery.get',
        {'id': '/tools/dns', 'fields': ['id']},
        key=reader[0],
        subject=reader[1],
        certs=(certificate.resource_id,),
    )
    assert ok(result).data['id'] == 'tool_dns'
    assert not override_checks
