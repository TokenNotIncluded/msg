"""Direct sharing is a current read source for one leaf, never a directory right."""
from datetime import timedelta

import pytest

from msg.core.codec import wire
from test_service import NOW, call, register


@pytest.mark.asyncio
async def test_private_ancestor_exact_read_and_immediate_revocation(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'share-alice')
    bob_key, bob, _ = await register(app, 'share-bob')
    topic = await call(app, 'content.topic_create', {'parent': '/main', 'name': 'share-private'},
                       key=alice_key, subject=alice)
    assert topic.status == 'ok', wire(topic)
    folder = topic.resources[0].id
    secret = await call(app, 'content.post_create', {'parent': folder, 'body': 'shared exact text'},
                        key=alice_key, subject=alice)
    sibling = await call(app, 'content.post_create', {'parent': folder, 'body': 'unshared sibling'},
                         key=alice_key, subject=alice)
    assert secret.status == sibling.status == 'ok'
    leaf = secret.resources[0].id
    chmod = await call(app, 'content.chmod', {'id': folder, 'mode': '0700'},
                       key=alice_key, subject=alice,
                       expected=((folder, topic.data['generation']),))
    assert chmod.status == 'ok', wire(chmod)
    before = await call(app, 'discovery.get', {'id': leaf}, key=bob_key, subject=bob)
    assert before.status == 'error' and before.error.code == 'permission_denied'
    async with app.metadata.transaction(write=False) as tx:
        epoch_before=tx.setting('authorization_epoch',0)
    granted = await call(app, 'sharing.grant', {'resource': leaf, 'grantee': bob,
        'expires_at': wire(NOW + timedelta(days=1))}, key=alice_key, subject=alice)
    assert granted.status == 'ok', wire(granted)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('authorization_epoch',0)==epoch_before+1
    gid = granted.data['grant']['id']
    for view in ({}, {'view': 'meta'}, {'view': 'history'}):
        read = await call(app, 'discovery.get', {'id': leaf, **view}, key=bob_key, subject=bob)
        assert read.status == 'ok', wire(read)
    raw = await call(app, 'discovery.raw', {'id': leaf}, key=bob_key, subject=bob)
    assert raw.status == 'ok', wire(raw)
    for op, args in (('discovery.get', {'id': folder}),
                     ('discovery.list', {'parent': folder}),
                     ('discovery.get', {'id': sibling.resources[0].id}),
                     ('sharing.grant', {'resource': leaf, 'grantee': alice,
                         'expires_at': wire(NOW + timedelta(days=1))})):
        denied = await call(app, op, args, key=bob_key, subject=bob)
        assert denied.status == 'error', (op, wire(denied))
    revoked = await call(app, 'sharing.revoke', {'grant_id': gid},
                         key=alice_key, subject=alice)
    assert revoked.status == 'ok', wire(revoked)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('authorization_epoch',0)==epoch_before+2
    after = await call(app, 'discovery.get', {'id': leaf}, key=bob_key, subject=bob)
    assert after.status == 'error' and after.error.code == 'permission_denied', wire(after)
    # Revoking one source does not disable ordinary mode access.
    traversable = await call(app, 'content.chmod', {'id': folder, 'mode': '0711'},
                             key=alice_key, subject=alice,
                             expected=((folder, chmod.data['generation']),))
    assert traversable.status == 'ok', wire(traversable)
    mode_read = await call(app, 'discovery.get', {'id': leaf}, key=bob_key, subject=bob)
    assert mode_read.status == 'ok', wire(mode_read)
    listed = await call(app, 'sharing.list', {'resource': leaf}, key=alice_key, subject=alice)
    assert listed.status == 'ok' and listed.data['grants'][0]['revoked_at'] is not None


@pytest.mark.asyncio
async def test_share_rejects_containers_and_managed_or_dm_resources(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'share-boundary-a')
    bob_key, bob, _ = await register(app, 'share-boundary-b')
    expiry = wire(NOW + timedelta(hours=1))
    topic = await call(app, 'content.topic_create', {'parent': '/main', 'name': 'share-container'},
                       key=alice_key, subject=alice)
    assert topic.status == 'ok'
    container = await call(app, 'sharing.grant', {'resource': topic.resources[0].id,
        'grantee': bob, 'expires_at': expiry}, key=alice_key, subject=alice)
    assert container.status == 'error' and container.error.code == 'share_container_forbidden'
    personal = await call(app, 'identity.personal_put', {'kind': 'soul', 'body': 'private soul'},
                          key=alice_key, subject=alice)
    assert personal.status == 'ok', wire(personal)
    soul = personal.resources[0].id
    denied = await call(app, 'sharing.grant', {'resource': soul, 'grantee': bob,
        'expires_at': expiry}, key=alice_key, subject=alice)
    assert denied.status == 'error', wire(denied)
    note = await call(app, 'identity.note_put', {'name': 'only-this.md', 'body': 'one note'},
                      key=alice_key, subject=alice)
    assert note.status == 'ok', wire(note)
    note_id = note.resources[0].id
    shared_note = await call(app, 'sharing.grant', {'resource': note_id, 'grantee': bob,
        'expires_at': expiry}, key=alice_key, subject=alice)
    assert shared_note.status == 'ok', wire(shared_note)
    read_note = await call(app, 'discovery.get', {'id': note_id}, key=bob_key, subject=bob)
    assert read_note.status == 'ok', wire(read_note)
    async with app.metadata.transaction(write=False) as tx:
        notes_folder = (await tx.resource(note_id)).parent
    list_notes = await call(app, 'discovery.list', {'parent': notes_folder},
                            key=bob_key, subject=bob)
    assert list_notes.status == 'error', wire(list_notes)
    todo = await call(app, 'identity.todo_put', {'name': 'do-it.md', 'title': 'one task'},
                      key=alice_key, subject=alice)
    assert todo.status == 'ok', wire(todo)
    shared_todo = await call(app, 'sharing.grant', {'resource': todo.resources[0].id,
        'grantee': bob, 'expires_at': expiry}, key=alice_key, subject=alice)
    assert shared_todo.status == 'error', wire(shared_todo)
    conversation = await call(app, 'communication.dm_request', {'recipient': bob},
                              key=alice_key, subject=alice)
    assert conversation.status == 'ok', wire(conversation)
    dm = await call(app, 'sharing.grant', {'resource': conversation.data['conversation_id'],
        'grantee': bob, 'expires_at': expiry}, key=alice_key, subject=alice)
    assert dm.status == 'error', wire(dm)
    system = await call(app, 'sharing.grant', {'resource': 'r_rules', 'grantee': bob,
        'expires_at': expiry}, key=alice_key, subject=alice)
    assert system.status == 'error', wire(system)


@pytest.mark.asyncio
async def test_expired_grant_does_not_authorize_and_can_be_replaced(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'share-expire-a')
    bob_key, bob, _ = await register(app, 'share-expire-b')
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'expiring'},
                      key=alice_key, subject=alice)
    rid = post.resources[0].id
    private = await call(app, 'content.chmod', {'id': rid, 'mode': '0600'},
                         key=alice_key, subject=alice,
                         expected=((rid, post.data['generation']),))
    assert private.status == 'ok', wire(private)
    first = await call(app, 'sharing.grant', {'resource': rid, 'grantee': bob,
        'expires_at': wire(NOW + timedelta(seconds=1))}, key=alice_key, subject=alice)
    assert first.status == 'ok', wire(first)
    app.executor.clock = lambda: NOW + timedelta(seconds=2)
    expired = await call(app, 'discovery.get', {'id': rid}, key=bob_key, subject=bob)
    assert expired.status == 'error' and expired.error.code == 'permission_denied', wire(expired)
    replacement = await call(app, 'sharing.grant', {'resource': rid, 'grantee': bob,
        'expires_at': wire(NOW + timedelta(days=1))}, key=alice_key, subject=alice)
    assert replacement.status == 'ok', wire(replacement)
    restored = await call(app, 'discovery.get', {'id': rid}, key=bob_key, subject=bob)
    assert restored.status == 'ok', wire(restored)
