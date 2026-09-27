"""Handoff and lease are durable collaboration facts, never grants or locks."""
import asyncio
from datetime import timedelta

import pytest

from msg.core.codec import wire
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_handoff_references_current_acl_and_decision_cas(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'handoff-alice')
    bob_key, bob, _ = await register(app, 'handoff-bob')
    other_key, other, _ = await register(app, 'handoff-other')
    private = await call(app, 'identity.note_put',
                         {'name': 'secret-note', 'body': 'Private plan'},
                         key=alice_key, subject=alice)
    assert private.status == 'ok', wire(private)
    rid = private.resources[0].id
    created = await call(app, 'communication.handoff_create',
                         {'to_subject': bob, 'resource_refs': [rid],
                          'message': 'Please take a look'}, key=alice_key, subject=alice,
                         rid='handoff-replay')
    assert created.status == 'ok', wire(created)
    hid = created.data['handoff']['id']
    own = await call(app, 'communication.handoff_get', {'id': hid},
                     key=alice_key, subject=alice)
    assert tuple(own.data['handoff']['resource_refs']) == (rid,)
    received = await call(app, 'communication.handoff_get', {'id': hid},
                          key=bob_key, subject=bob)
    assert received.status == 'ok' and not received.data['handoff']['resource_refs']
    shared = await call(app, 'sharing.grant',
                        {'resource': rid, 'grantee': bob,
                         'expires_at': wire(NOW + timedelta(days=1))},
                        key=alice_key, subject=alice)
    assert shared.status == 'ok', wire(shared)
    now_visible = await call(app, 'communication.handoff_get', {'id': hid},
                             key=bob_key, subject=bob)
    assert tuple(now_visible.data['handoff']['resource_refs']) == (rid,)
    revoked = await call(app, 'sharing.revoke',
                         {'grant_id': shared.data['grant']['id']},
                         key=alice_key, subject=alice)
    assert revoked.status == 'ok'
    hidden_again = await call(app, 'communication.handoff_get', {'id': hid},
                              key=bob_key, subject=bob)
    assert not hidden_again.data['handoff']['resource_refs']
    replay = await call(app, 'communication.handoff_create',
                        {'to_subject': bob, 'resource_refs': [rid],
                         'message': 'Please take a look'}, key=alice_key,
                        subject=alice, rid='handoff-replay')
    assert replay.replayed and rid not in str(replay.data)
    assert (await call(app, 'discovery.get', {'id': rid},
                       key=bob_key, subject=bob)).status == 'error'
    assert (await call(app, 'communication.handoff_get', {'id': hid},
                       key=other_key, subject=other)).error.code == 'handoff_not_found'
    inbox = await call(app, 'communication.inbox', {}, key=bob_key, subject=bob)
    notices = [i for i in inbox.data['items'] if i.get('handoff_id') == hid]
    assert len(notices) == 1 and notices[0]['source'] == 'handoff'
    assert rid not in str(notices[0]) and 'Private plan' not in str(notices[0])
    before = await _count(app)
    assert (await call(app, 'communication.handoff_get', {'id': hid},
                       key=bob_key, subject=bob)).status == 'ok'
    assert await _count(app) == before
    denied = await call(app, 'communication.handoff_decide',
                        {'id': hid, 'decision': 'accept', 'expected_generation': 1},
                        key=alice_key, subject=alice)
    assert denied.error.code == 'handoff_decision_forbidden'
    accepted = await call(app, 'communication.handoff_decide',
                          {'id': hid, 'decision': 'accept', 'expected_generation': 1},
                          key=bob_key, subject=bob)
    assert accepted.status == 'ok' and accepted.data['handoff']['status'] == 'accepted'
    stale = await call(app, 'communication.handoff_decide',
                       {'id': hid, 'decision': 'reject', 'expected_generation': 1},
                       key=bob_key, subject=bob)
    assert stale.status == 'error'


@pytest.mark.asyncio
async def test_lease_ttl_generation_and_no_exclusive_write_claim(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'lease-alice')
    bob_key, bob, _ = await register(app, 'lease-bob')
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'Open task'},
                      key=alice_key, subject=alice)
    assert post.status == 'ok', wire(post)
    rid = post.resources[0].id
    args = {'target': rid, 'purpose': 'Draft update',
            'expires_at': wire(NOW + timedelta(hours=1))}
    first = await call(app, 'communication.lease_acquire', args,
                       key=alice_key, subject=alice)
    second = await call(app, 'communication.lease_acquire', args,
                        key=bob_key, subject=bob)
    assert first.status == second.status == 'ok'
    lid = first.data['lease']['id']
    assert lid != second.data['lease']['id']
    assert (await call(app, 'communication.lease_get', {'id': lid},
                       key=bob_key, subject=bob)).error.code == 'lease_not_found'
    stale = await call(app, 'communication.lease_renew',
                       {'id': lid, 'expires_at': wire(NOW + timedelta(hours=2)),
                        'expected_generation': 2}, key=alice_key, subject=alice)
    assert stale.error.code == 'generation_conflict'
    renewed = await call(app, 'communication.lease_renew',
                         {'id': lid, 'expires_at': wire(NOW + timedelta(hours=2)),
                          'expected_generation': 1}, key=alice_key, subject=alice)
    assert renewed.status == 'ok' and renewed.data['lease']['generation'] == 2
    released = await call(app, 'communication.lease_release',
                          {'id': lid, 'expected_generation': 2},
                          key=alice_key, subject=alice)
    assert released.status == 'ok' and released.data['lease']['status'] == 'released'
    assert (await call(app, 'communication.lease_renew',
                       {'id': lid, 'expires_at': wire(NOW + timedelta(hours=3)),
                        'expected_generation': 3}, key=alice_key, subject=alice)).status == 'error'
    active_id = second.data['lease']['id']
    app.executor.clock = lambda: NOW + timedelta(hours=3)
    before = await _count(app)
    expired = await call(app, 'communication.lease_get', {'id': active_id},
                         key=bob_key, subject=bob)
    assert expired.data['lease']['effective_status'] == 'expired'
    assert await _count(app) == before
    assert (await call(app, 'communication.lease_release',
                       {'id': active_id, 'expected_generation': 1},
                       key=bob_key, subject=bob)).error.code == 'lease_inactive'


@pytest.mark.asyncio
async def test_collaboration_rejects_dm_and_system_refs_and_serializes_decisions(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'collab-alice')
    bob_key, bob, _ = await register(app, 'collab-bob')
    topic = (await call(app, 'communication.dm_request', {'recipient': bob},
                        key=alice_key, subject=alice)).data['conversation_id']
    assert (await call(app, 'communication.dm_accept', {'conversation_id': topic},
                       key=bob_key, subject=bob)).status == 'ok'
    secret = (await call(app, 'communication.dm_send',
                         {'conversation_id': topic, 'body': 'Private'},
                         key=alice_key, subject=alice)).resources[0].id
    for target in (topic, secret, '/_rules/_index.md'):
        handoff = await call(app, 'communication.handoff_create',
                             {'to_subject': bob, 'resource_refs': [target]},
                             key=alice_key, subject=alice)
        lease = await call(app, 'communication.lease_acquire',
                           {'target': target, 'purpose': 'bypass attempt',
                            'expires_at': wire(NOW + timedelta(hours=1))},
                           key=alice_key, subject=alice)
        assert handoff.status == lease.status == 'error', (wire(handoff), wire(lease))
    created = await call(app, 'communication.handoff_create',
                         {'to_subject': bob, 'resource_refs': []},
                         key=alice_key, subject=alice)
    hid = created.data['handoff']['id']
    outcomes = await asyncio.gather(
        call(app, 'communication.handoff_decide',
             {'id': hid, 'decision': 'accept', 'expected_generation': 1},
             key=bob_key, subject=bob),
        call(app, 'communication.handoff_decide',
             {'id': hid, 'decision': 'reject', 'expected_generation': 1},
             key=bob_key, subject=bob),
    )
    assert sorted(result.status for result in outcomes) == ['error', 'ok']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT generation FROM handoffs WHERE id=?', (hid,))[0] == 2
        assert tx.one('SELECT COUNT(*) FROM messages WHERE body LIKE ?',
                      ('%' + hid + '%',))[0] == 2


async def _count(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0] for table in
                     ('handoffs', 'collaboration_leases', 'messages', 'events', 'audit'))
