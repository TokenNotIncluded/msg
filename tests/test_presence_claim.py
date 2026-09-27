"""Presence is explicit, and claims are signed self-statements only."""
from datetime import timedelta

import pytest

from msg.core.codec import decode, parse_time, unb64, wire
from msg.core.models import Signature
from msg.security.crypto import verify
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_presence_unknown_set_clear_and_expiry(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'presence-user')
    initial = await call(app, 'communication.presence_get', {'subject_id': subject})
    assert initial.status == 'ok' and initial.data == {'subject_id': subject, 'state': 'unknown'}
    await call(app, 'discovery.get', {'id': '/main'})
    still = await call(app, 'communication.presence_get', {'subject_id': subject})
    assert still.data['state'] == 'unknown'
    posted = await call(app, 'communication.presence_set', {
        'state': 'available', 'message': 'reviewing', 'capabilities_hint': ['python'], 'ttl': 60,
    }, key=key, subject=subject)
    assert posted.status == 'ok', wire(posted)
    seen = await call(app, 'communication.presence_get', {'subject_id': subject})
    assert seen.data['state'] == 'available' and seen.data['message'] == 'reviewing'
    app.executor.clock = lambda: NOW + timedelta(seconds=61)
    expired = await call(app, 'communication.presence_get', {'subject_id': subject})
    assert expired.data == {'subject_id': subject, 'state': 'unknown'}
    app.executor.clock = lambda: NOW
    cleared = await call(app, 'communication.presence_clear', {}, key=key, subject=subject)
    assert cleared.status == 'ok'
    assert (await call(app, 'communication.presence_get', {'subject_id': subject})).data['state'] == 'unknown'
    default = await call(app, 'communication.presence_set', {'state': 'busy'}, key=key, subject=subject)
    assert parse_time(default.data['expires_at']) == NOW + timedelta(seconds=300)
    too_long = await call(app, 'communication.presence_set', {'state': 'available', 'ttl': 3601},
                          key=key, subject=subject)
    assert too_long.status == 'error'


@pytest.mark.asyncio
async def test_claim_signature_private_evidence_and_no_authority(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'claim-owner')
    other_key, other, _ = await register(app, 'claim-other')
    empty = await call(app, 'communication.claim_list', {'subject_id': subject})
    assert empty.status == 'ok' and not empty.data['items']
    async with app.metadata.transaction(write=False) as tx:
        files = await tx.resolve('/@claim-owner/files')
    created = await call(app, 'communication.claim_create', {
        'predicate': 'can_help_with', 'value': {'language': 'Python'},
        'evidence_refs': [{'id': files}],
    }, key=key, subject=subject)
    assert created.status == 'ok', wire(created)
    claim_id = created.data['claim_id']
    own = await call(app, 'communication.claim_get', {'id': claim_id}, key=key, subject=subject)
    public = await call(app, 'communication.claim_get', {'id': claim_id}, key=other_key, subject=other)
    assert own.status == public.status == 'ok'
    assert own.data['evidence_refs'] == ({'id': files, 'revision': None},)
    assert public.data['evidence_refs'] == ()
    assert 'signed_envelope' in own.data and 'signed_envelope' not in public.data
    verify(key.public_key, unb64(own.data['signed_envelope']),
           decode(Signature, own.data['signature']), purpose='request')
    projected = await call(app, 'discovery.get', {'id': claim_id}, key=other_key, subject=other)
    assert projected.status == 'ok' and files not in str(projected.data)
    assert public.data['kind'] == 'self_claim' and public.data['authority'] == 'none'
    forbidden = await call(app, 'discovery.get', {'id': '/private'}, key=key, subject=subject)
    assert forbidden.status == 'error'
    missing = await call(app, 'communication.claim_create', {
        'predicate': 'anything', 'value': True, 'evidence_refs': [{'id': '/private'}],
    }, key=key, subject=subject)
    assert missing.status == 'error'
    capability_claim = await call(app, 'communication.claim_create', {
        'predicate': 'system.namespace', 'value': True,
    }, key=key, subject=subject)
    assert capability_claim.status == 'ok'
    still_forbidden = await call(app, 'discovery.get', {'id': '/private'}, key=key, subject=subject)
    assert still_forbidden.status == 'error'
