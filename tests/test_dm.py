"""Direct conversations have one pair identity and a hard participant boundary."""
import asyncio
import pytest

from msg.core.codec import wire
from test_service import call, register


@pytest.mark.asyncio
async def test_dm_request_accept_send_and_third_party_deny(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'dm-alice')
    bob_key, bob, _ = await register(app, 'dm-bob')
    eve_key, eve, _ = await register(app, 'dm-eve')
    requested = await call(app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice)
    assert requested.status == 'ok', wire(requested)
    topic = requested.data['conversation_id']
    assert requested.data['state'] == 'pending'
    duplicate = await call(app, 'communication.dm_request', {'recipient': alice}, key=bob_key, subject=bob)
    assert duplicate.data['conversation_id'] == topic
    denied = await call(app, 'communication.dm_send', {'conversation_id': topic, 'body': 'too early'},
                        key=alice_key, subject=alice)
    assert denied.status == 'error'
    accepted = await call(app, 'communication.dm_accept', {'conversation_id': topic}, key=bob_key, subject=bob)
    assert accepted.status == 'ok' and accepted.data['state'] == 'active', wire(accepted)
    sent = await call(app, 'communication.dm_send', {'conversation_id': topic, 'body': 'private hello'},
                      key=alice_key, subject=alice)
    assert sent.status == 'ok', wire(sent)
    post = sent.resources[0].id
    for key, subject in ((alice_key, alice), (bob_key, bob)):
        read = await call(app, 'discovery.get', {'id': post}, key=key, subject=subject)
        assert read.status == 'ok' and read.data['content'] == 'private hello', wire(read)
    for op, args in (('discovery.get', {'id': topic}), ('discovery.get', {'id': post}),
                     ('discovery.list', {'parent': topic}), ('discovery.search', {'query': 'private hello'})):
        result = await call(app, op, args, key=eve_key, subject=eve)
        assert result.status == 'error' or not result.data.get('items'), (op, wire(result))
    reference = await call(app, 'communication.send', {'recipient': eve, 'resource': {'id': post}},
                           key=alice_key, subject=alice)
    assert reference.status == 'error'
    anonymous = await call(app, 'discovery.get', {'id': post})
    assert anonymous.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.resource(post)).type == 'post'
        assert (await tx.resource(post)).parent == topic
        assert tx.one('SELECT COUNT(*) FROM dm_conversations')[0] == 1


@pytest.mark.asyncio
async def test_dm_reject_block_archive_and_other_message_immutable(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'dm-amber')
    bob_key, bob, _ = await register(app, 'dm-blue')
    requested = await call(app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice)
    topic = requested.data['conversation_id']
    rejected = await call(app, 'communication.dm_reject', {'conversation_id': topic}, key=bob_key, subject=bob)
    assert rejected.status == 'ok' and rejected.data['state'] == 'rejected'
    denied = await call(app, 'communication.dm_accept', {'conversation_id': topic}, key=bob_key, subject=bob)
    assert denied.status == 'error'
    blocked = await call(app, 'communication.dm_block', {'subject_id': alice}, key=bob_key, subject=bob)
    assert blocked.status == 'ok'
    request_again = await call(app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice)
    assert request_again.status == 'error'


@pytest.mark.asyncio
async def test_dm_owner_cannot_publish_or_edit_other_post(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'dm-gold')
    bob_key, bob, _ = await register(app, 'dm-silver')
    topic = (await call(app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice)).data['conversation_id']
    await call(app, 'communication.dm_accept', {'conversation_id': topic}, key=bob_key, subject=bob)
    sent = await call(app, 'communication.dm_send', {'conversation_id': topic, 'body': 'owned by Bob'},
                      key=bob_key, subject=bob)
    post = sent.resources[0].id
    edited = await call(app, 'content.post_edit', {'id': post, 'expected_revision': sent.resources[0].revision,
                        'body': 'Alice changes it'}, key=alice_key, subject=alice,
                        expected=((post, sent.data['generation']),))
    assert edited.status == 'error'
    for op, args in (('content.chmod', {'id': topic, 'mode': '0777'}),
                     ('content.move', {'id': topic, 'parent': '/main'}),
                     ('discussion.quote', {'parent': '/main', 'target': {'id': post}, 'body': 'leak'}),
                     ('content.post_create', {'parent': '/main', 'source': {'id': post,
                         'revision': sent.resources[0].revision}})):
        result = await call(app, op, args, key=alice_key, subject=alice)
        assert result.status == 'error', (op, wire(result))
    archived = await call(app, 'communication.dm_archive', {'conversation_id': topic}, key=alice_key, subject=alice)
    assert archived.status == 'ok'
    alice_list = await call(app, 'communication.dm_list', {}, key=alice_key, subject=alice)
    bob_list = await call(app, 'communication.dm_list', {}, key=bob_key, subject=bob)
    assert not alice_list.data['items'] and len(bob_list.data['items']) == 1
    read_history = await call(app, 'discovery.get', {'id': post}, key=alice_key, subject=alice)
    assert read_history.status == 'ok'


@pytest.mark.asyncio
async def test_simultaneous_requests_share_one_pair_and_block_keeps_history(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'dm-concurrent-a')
    bob_key, bob, _ = await register(app, 'dm-concurrent-b')
    first, second = await asyncio.gather(
        call(app, 'communication.dm_request', {'recipient': bob}, key=alice_key, subject=alice),
        call(app, 'communication.dm_request', {'recipient': alice}, key=bob_key, subject=bob),
    )
    assert first.status == second.status == 'ok', (wire(first), wire(second))
    assert first.data['conversation_id'] == second.data['conversation_id']
    topic = first.data['conversation_id']
    # Whichever transaction won, the other participant can accept it.
    async with app.metadata.transaction(write=False) as tx:
        actual_initiator = tx.one('SELECT initiator FROM dm_conversations WHERE resource_id=?', (topic,))[0]
        assert tx.one('SELECT COUNT(*) FROM dm_conversations')[0] == 1
    recipient_key, recipient = (bob_key, bob) if actual_initiator == alice else (alice_key, alice)
    accepted = await call(app, 'communication.dm_accept', {'conversation_id': topic},
                          key=recipient_key, subject=recipient)
    assert accepted.status == 'ok', wire(accepted)
    sent = await call(app, 'communication.dm_send', {'conversation_id': topic, 'body': 'retained'},
                      key=alice_key, subject=alice)
    assert sent.status == 'ok', wire(sent)
    post = sent.resources[0].id
    inbox = await call(app, 'communication.inbox', {}, key=bob_key, subject=bob)
    assert any(item['resource']['id'] == post for item in inbox.data['items'])
    changes = await call(app, 'communication.changes', {}, key=bob_key, subject=bob)
    assert any(any(ref['id'] == post for ref in item['resources']) for item in changes.data['items'])
    async with app.metadata.transaction(write=False) as tx:
        acknowledgements_before = tx.one("SELECT COUNT(*) FROM reactions WHERE kind LIKE 'ack.%'")[0]
    blocked = await call(app, 'communication.dm_block', {'subject_id': alice}, key=bob_key, subject=bob)
    assert blocked.status == 'ok'
    later = await call(app, 'communication.dm_send', {'conversation_id': topic, 'body': 'blocked'},
                       key=alice_key, subject=alice)
    assert later.status == 'error'
    edited = await call(app, 'content.post_edit',
                        {'id': post, 'expected_revision': sent.resources[0].revision,
                         'body': 'changed after block'},
                        key=alice_key, subject=alice,
                        expected=((post, sent.data['generation']),))
    assert edited.status == 'error' and edited.error.code == 'dm_blocked', wire(edited)
    history = await call(app, 'discovery.get', {'id': post}, key=bob_key, subject=bob)
    assert history.status == 'ok' and history.data['content'] == 'retained'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one("SELECT COUNT(*) FROM reactions WHERE kind LIKE 'ack.%'")[0] == acknowledgements_before
