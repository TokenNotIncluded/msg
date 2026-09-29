"""Explicit saved descriptors outlive read tickets, never their live authority."""
from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for


async def make_query_ref(app, key, subject, arguments, kind='read'):
    payload = canonical({'version': 1, 'kind': kind, 'arguments': arguments})
    opened = await call(app, 'transfer.open', {'direction': 'upload', 'size': len(payload),
        'digest': digest(payload), 'media_type': 'application/vnd.msg.read-query+json'},
        key=key, subject=subject)
    assert opened.status == 'ok', opened.error
    transfer = opened.data['transfer_id']
    part = await call(app, 'transfer.part_put', {'transfer_id': transfer, 'offset': 0,
        'data': b64(payload), 'digest': digest(payload)}, key=key, subject=subject)
    assert part.status == 'ok', part.error
    sealed = await call(app, 'transfer.seal', {'transfer_id': transfer, 'final_size': len(payload),
        'final_digest': digest(payload)}, key=key, subject=subject)
    assert sealed.status == 'ok', sealed.error
    ticket = await call(app, 'transfer.query_seal', {'transfer_id': transfer}, key=key, subject=subject)
    assert ticket.status == 'ok', ticket.error
    return ticket.data['query_ref']


async def current_call(app, op, args, key, subject, **kwargs):
    return await app.executor.execute(request_for(op, args, app.settings.service_url,
        signer=key, subject=subject, expires_at=app.clock() + timedelta(seconds=120), **kwargs))


@pytest.mark.asyncio
async def test_save_pins_descriptor_replays_and_outlives_only_ticket(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'saved-query-owner')
    args = {'parent': '/main', 'type': 'post', 'tag': 'work'}
    token = await make_query_ref(app, key, subject, args)
    saved = await call(app, 'query.save', {'query_ref': token}, key=key, subject=subject,
                       rid='save-descriptor-once')
    assert saved.status == 'ok', saved.error
    replay = await call(app, 'query.save', {'query_ref': token}, key=key, subject=subject,
                        rid='save-descriptor-once')
    assert replay.replayed and replay.resources == saved.resources
    ref = wire(saved.resources[0])
    got = await call(app, 'query.saved_get', {'ref': ref}, key=key, subject=subject)
    assert got.status == 'ok', got.error
    assert dict(got.data['arguments']) == {**args, 'parent': 't_main'}
    assert got.data['operation'] == 'discovery.read_query'
    assert got.data['digest'] == saved.data['descriptor_digest']
    assert 'principal' not in got.data
    assert (await call(app, 'discovery.get', {'id': ref['id']}, key=key, subject=subject)).status == 'error'
    app.executor.clock = lambda: NOW + timedelta(minutes=16)
    app.clock = app.executor.clock
    app.executor.authenticator.clock = app.clock
    expired = await current_call(app, 'query.save', {'query_ref': token}, key, subject)
    assert expired.error.code == 'query_ref_expired'
    durable = await current_call(app, 'query.saved_get', {'ref': ref}, key, subject)
    assert durable.status == 'ok', durable.error
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(ref['id'])).generation
    archived = await current_call(app, 'query.saved_archive', {'ref': ref}, key, subject,
                                  expected=((ref['id'], generation),))
    assert archived.status == 'ok', archived.error
    assert (await current_call(app, 'query.saved_get', {'ref': ref}, key, subject)).error.code == 'saved_query_not_found'


@pytest.mark.asyncio
async def test_saved_query_rechecks_scope_and_never_shares_captured_authority(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'saved-scope-owner')
    other_key, other, _ = await register(app, 'saved-scope-reader')
    topic = await call(app, 'content.topic_create', {'parent': '/main', 'name': 'saved-scope'},
                       key=key, subject=subject)
    rid = topic.resources[0].id
    token = await make_query_ref(app, other_key, other, {'parent': rid, 'type': 'post'})
    saved = await call(app, 'query.save', {'query_ref': token}, key=other_key, subject=other)
    assert saved.status == 'ok', saved.error
    ref = wire(saved.resources[0])
    wrong = await call(app, 'query.save', {'query_ref': token}, key=key, subject=subject)
    assert wrong.error.code == 'query_ref_principal_mismatch'
    assert (await call(app, 'query.saved_get', {'ref': ref}, key=key, subject=subject)).status == 'error'
    grant = await call(app, 'sharing.grant', {'resource': ref['id'], 'grantee': subject,
        'expires_at': wire(NOW + timedelta(hours=1))}, key=other_key, subject=other)
    assert grant.status == 'error'
    hidden = await call(app, 'content.chmod', {'id': rid, 'mode': '0700'}, key=key, subject=subject,
                        expected=((rid, topic.data['generation']),))
    assert hidden.status == 'ok', hidden.error
    denied = await call(app, 'query.saved_get', {'ref': ref}, key=other_key, subject=other)
    assert denied.status == 'error'


@pytest.mark.asyncio
async def test_saved_query_requires_pinned_revision_and_unchanged_digest(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'saved-pin-owner')
    token = await make_query_ref(app, key, subject, {'parent': '/main'})
    saved = await call(app, 'query.save', {'query_ref': token}, key=key, subject=subject)
    assert saved.status == 'ok', saved.error
    ref = wire(saved.resources[0])
    for bad_ref in ({'id': ref['id']}, {**ref, 'revision': 'nonexistent_revision'}):
        assert (await call(app, 'query.saved_get', {'ref': bad_ref}, key=key, subject=subject)).status == 'error'
    tampered = token[:-1] + ('a' if token[-1] != 'a' else 'b')
    assert (await call(app, 'query.save', {'query_ref': tampered}, key=key, subject=subject)).status == 'error'


@pytest.mark.asyncio
async def test_saved_authority_is_current_and_internal_body_cannot_be_projected(installed):
    from dataclasses import replace
    app, _ = installed
    key, subject, _ = await register(app, 'saved-authority-owner')
    token = await make_query_ref(app, key, subject, {'parent': '/main'})
    forbidden_projection = await current_call(app, 'query.save', {'query_ref': token}, key, subject,
                                              return_fields=('content',))
    assert forbidden_projection.error.code == 'projection_unavailable'
    saved = await call(app, 'query.save', {'query_ref': token}, key=key, subject=subject)
    assert saved.status == 'ok', saved.error
    ref = wire(saved.resources[0])
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        ceiling = tuple(replace(grant, operations=grant.operations - {'query.save@1'})
                        for grant in credential.ceiling)
        await tx.save_credential(replace(credential, ceiling=ceiling), (await tx.subject(subject)).auth_version)
        generation = (await tx.resource(ref['id'])).generation
    denied = await call(app, 'query.saved_get', {'ref': ref}, key=key, subject=subject)
    assert denied.error.code == 'credential_ceiling'
    # Revocation remains possible without the removed creation authority.
    archived = await current_call(app, 'query.saved_archive', {'ref': ref}, key, subject,
                                  expected=((ref['id'], generation),))
    assert archived.status == 'ok', archived.error
