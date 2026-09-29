"""Durable work records use ordinary revisions and explicit transitions."""
from datetime import timedelta

import pytest
from test_service import NOW, call, register

from msg.core.codec import wire


@pytest.mark.asyncio
async def test_work_request_lifecycle_replay_and_current_auth(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'request-owner')
    other_key, other, _ = await register(app, 'request-other')
    args = {'title': 'Review', 'description': 'Review the change', 'requirements': 'Read tests'}
    made = await call(app, 'communication.request_create', args, key=key, subject=owner,
                      rid='create-work-request')
    assert made.status == 'ok', made.error
    rid = made.data['id']
    repeated = await call(app, 'communication.request_create', args, key=key, subject=owner,
                          rid='create-work-request')
    assert repeated.replayed and repeated.data == made.data
    conflict = await call(app, 'communication.request_create', {**args, 'title': 'Changed'},
                          key=key, subject=owner, rid='create-work-request')
    assert conflict.status == 'error'
    assert (await call(app, 'communication.request_get', {'id': rid},
                       key=other_key, subject=other)).status == 'error'
    # Sharing supplies read access, while claim itself is a dedicated signed operation.
    grant = await call(app, 'sharing.grant', {'resource': rid, 'grantee': other,
        'expires_at': wire(NOW + timedelta(days=1))}, key=key, subject=owner)
    assert grant.status == 'ok', grant.error
    claimed = await call(app, 'communication.request_claim', {'id': rid},
                         key=other_key, subject=other, expected=((rid, made.data['generation']),))
    assert claimed.status == 'ok', claimed.error
    denied = await call(app, 'communication.request_fulfill', {'id': rid},
                        key=key, subject=owner, expected=((rid, claimed.data['generation']),))
    assert denied.error.code == 'collaboration_actor_forbidden'
    fulfilled = await call(app, 'communication.request_fulfill', {'id': rid},
                           key=other_key, subject=other, expected=((rid, claimed.data['generation']),))
    assert fulfilled.status == 'ok', fulfilled.error
    assert fulfilled.data['status'] == 'fulfilled'
    got = await call(app, 'communication.request_get', {'id': rid}, key=key, subject=owner)
    assert got.data['request']['assignee'] == other
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM revisions WHERE resource_id=?', (rid,))[0] == 3
        assert tx.one("SELECT COUNT(*) FROM events WHERE body LIKE '%communication.request_%'")[0] >= 3


@pytest.mark.asyncio
async def test_checkpoint_relations_are_private_and_rechecked(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'checkpoint-owner')
    other_key, other, _ = await register(app, 'checkpoint-other')
    note = await call(app, 'identity.note_put', {'name': 'work', 'body': 'private'},
                      key=key, subject=owner)
    target = note.resources[0].id
    made = await call(app, 'communication.checkpoint_create',
                      {'summary': 'Resume here', 'resource_refs': [target], 'state_ref': target},
                      key=key, subject=owner)
    assert made.status == 'ok', made.error
    rid = made.data['id']
    grant = await call(app, 'sharing.grant', {'resource': rid, 'grantee': other,
        'expires_at': wire(NOW + timedelta(days=1))}, key=key, subject=owner)
    assert grant.status == 'ok', grant.error
    got = await call(app, 'communication.checkpoint_get', {'id': rid}, key=other_key, subject=other)
    assert got.status == 'ok', got.error
    assert not got.data['checkpoint']['resource_refs']
    assert 'state_ref' not in got.data['checkpoint']
    generic = await call(app, 'discovery.get', {'id': rid}, key=other_key, subject=other)
    assert generic.status == 'ok', generic.error
    assert target not in str(generic.data)
    own = await call(app, 'communication.checkpoint_get', {'id': rid}, key=key, subject=owner)
    assert own.data['checkpoint']['resource_refs'] == (target,)
    assert own.data['checkpoint']['state_ref'] == target
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM relations WHERE source_id=?', (rid,))[0] == 2


@pytest.mark.asyncio
async def test_offer_expiry_withdraw_and_request_schema_are_explicit(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'offer-owner')
    args = {'description': 'Review code', 'scope': 'Python', 'availability': 'Today',
            'expires_at': wire(NOW + timedelta(seconds=60))}
    made = await call(app, 'communication.offer_create', args, key=key, subject=owner)
    assert made.status == 'ok', made.error
    rid = made.data['id']
    app.executor.clock = lambda: NOW + timedelta(seconds=61)
    got = await call(app, 'communication.offer_get', {'id': rid}, key=key, subject=owner)
    assert got.data['offer']['effective_status'] == 'expired'
    expired = await call(app, 'communication.offer_withdraw', {'id': rid}, key=key, subject=owner,
                         expected=((rid, made.data['generation']),))
    assert expired.error.code == 'collaboration_expired'
    app.executor.clock = lambda: NOW
    withdrawn = await call(app, 'communication.offer_withdraw', {'id': rid}, key=key, subject=owner,
                           expected=((rid, made.data['generation']),))
    assert withdrawn.data['status'] == 'withdrawn'
    for args in ({}, {'title': '', 'description': 'x', 'requirements': 'x'},
                 {'title': 'x', 'description': 'x', 'requirements': 'x', 'status': 'fulfilled'}):
        assert (await call(app, 'communication.request_create', args, key=key, subject=owner)).status == 'error'


@pytest.mark.asyncio
async def test_request_claim_race_revocation_and_cancel_preserve_history(installed):
    import asyncio
    app, _ = installed
    key, owner, _ = await register(app, 'claim-race-owner')
    alice_key, alice, _ = await register(app, 'claim-race-alice')
    bob_key, bob, _ = await register(app, 'claim-race-bob')
    made = await call(app, 'communication.request_create',
        {'title': 'One task', 'description': 'Only one assignee', 'requirements': 'Review'},
        key=key, subject=owner)
    rid = made.data['id']
    grants = {}
    for subject in (alice, bob):
        grant = await call(app, 'sharing.grant', {'resource': rid, 'grantee': subject,
            'expires_at': wire(NOW + timedelta(days=1))}, key=key, subject=owner)
        assert grant.status == 'ok', grant.error
        grants[subject] = grant.data['grant']['id']
    results = await asyncio.gather(*(call(app, 'communication.request_claim', {'id': rid},
        key=k, subject=s, expected=((rid, made.data['generation']),))
        for k, s in ((alice_key, alice), (bob_key, bob))))
    assert sum(r.status == 'ok' for r in results) == 1
    winner = next((k, s, r) for (k, s), r in zip(((alice_key, alice), (bob_key, bob)), results)
                  if r.status == 'ok')
    revoked = await call(app, 'sharing.revoke', {'grant_id': grants[winner[1]]}, key=key, subject=owner)
    assert revoked.status == 'ok'
    forbidden = await call(app, 'communication.request_fulfill', {'id': rid},
        key=winner[0], subject=winner[1], expected=((rid, winner[2].data['generation']),))
    assert forbidden.status == 'error'
    cancelled = await call(app, 'communication.request_cancel', {'id': rid}, key=key, subject=owner,
        expected=((rid, winner[2].data['generation']),))
    assert cancelled.status == 'ok' and cancelled.data['status'] == 'cancelled'
    again = await call(app, 'communication.request_claim', {'id': rid}, key=key, subject=owner,
        expected=((rid, cancelled.data['generation']),))
    assert again.error.code == 'request_not_open'


@pytest.mark.asyncio
async def test_checkpoint_event_failure_rolls_back_resource_revision_and_retry(installed, monkeypatch):
    from msg.core.errors import Failure
    app, _ = installed
    key, owner, _ = await register(app, 'checkpoint-atomic')
    # Inject at the common authoritative Event append point, after handler writes.
    async with app.metadata.transaction(write=False) as tx:
        tx_type = type(tx)
    append = tx_type.append_event

    async def fail_event(self, event):
        if event.type == 'communication.checkpoint_create':
            raise Failure('injected_event_failure')
        return await append(self, event)

    args = {'summary': 'A durable point', 'resource_refs': []}
    with monkeypatch.context() as patch:
        patch.setattr(tx_type, 'append_event', fail_event)
        failed = await call(app, 'communication.checkpoint_create', args, key=key,
                            subject=owner, rid='checkpoint-atomic-retry')
    assert failed.error.code == 'injected_event_failure'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM resources WHERE type='checkpoint'")[0] == 0
    retried = await call(app, 'communication.checkpoint_create', args, key=key,
                         subject=owner, rid='checkpoint-atomic-retry')
    assert retried.status == 'ok', retried.error
