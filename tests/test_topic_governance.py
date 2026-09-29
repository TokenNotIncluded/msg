"""Topic roles, bans and virtual event history share the write transaction."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_topic_open_membership_ban_and_last_admin(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'topic-owner')
    member_key, member, _ = await register(app, 'topic-member')
    created = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'governed'},
        key=owner_key,
        subject=owner,
    )
    assert created.status == 'ok', wire(created)
    topic = created.resources[0].id
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(topic)
        assert resource.created_by == owner
        assert tx.one(
            'SELECT role,status FROM topic_memberships WHERE topic=? AND subject=?', (topic, owner)
        ) == ('admin', 'active')
    joined = await call(app, 'content.topic_join', {'id': topic}, key=member_key, subject=member)
    assert joined.status == 'ok' and joined.data['status'] == 'active', wire(joined)
    refused = await call(app, 'content.topic_leave', {'id': topic}, key=owner_key, subject=owner)
    assert refused.status == 'error' and refused.error.code == 'last_topic_admin'
    async with app.metadata.transaction(write=False) as tx:
        after_failed_leave = tx.one('SELECT COUNT(*) FROM events')[0]
    demote = await call(
        app,
        'content.topic_demote',
        {'id': topic, 'subject_id': owner},
        key=owner_key,
        subject=owner,
    )
    assert demote.status == 'error' and demote.error.code == 'last_topic_admin'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == after_failed_leave
    ban = await call(
        app,
        'content.topic_ban',
        {'id': topic, 'subject_id': member, 'reason': 'moderation detail'},
        key=owner_key,
        subject=owner,
    )
    assert ban.status == 'ok', wire(ban)
    blocked = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'should fail'},
        key=member_key,
        subject=member,
    )
    assert blocked.status == 'error' and blocked.error.code == 'topic_banned'
    unban = await call(
        app,
        'content.topic_unban',
        {'id': topic, 'subject_id': member},
        key=owner_key,
        subject=owner,
    )
    assert unban.status == 'ok'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT status FROM topic_memberships WHERE topic=? AND subject=?', (topic, member)
            )[0]
            != 'active'
        )
        assert (await tx.resource(topic)).created_by == owner
    rejoined = await call(app, 'content.topic_join', {'id': topic}, key=member_key, subject=member)
    assert rejoined.status == 'ok'
    no_system_power = await call(
        app, 'discovery.get', {'id': '/private'}, key=owner_key, subject=owner
    )
    assert no_system_power.status == 'error'


@pytest.mark.asyncio
async def test_topic_policy_approval_invite_closed_and_role_transfer(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'policy-owner')
    alice_key, alice, _ = await register(app, 'policy-alice')
    bob_key, bob, _ = await register(app, 'policy-bob')
    topic = (
        (
            await call(
                app,
                'content.topic_create',
                {'parent': '/main', 'name': 'policy-topic'},
                key=owner_key,
                subject=owner,
            )
        )
        .resources[0]
        .id
    )
    approval = await call(
        app,
        'content.topic_policy_set',
        {'id': topic, 'membership_policy': 'approval'},
        key=owner_key,
        subject=owner,
    )
    assert approval.status == 'ok'
    pending = await call(app, 'content.topic_join', {'id': topic}, key=alice_key, subject=alice)
    assert pending.data['status'] == 'pending'
    approved = await call(
        app,
        'content.topic_approve',
        {'id': topic, 'subject_id': alice},
        key=owner_key,
        subject=owner,
    )
    assert approved.data['status'] == 'active'
    member_sync = await call(app, 'communication.changes', {}, key=alice_key, subject=alice)
    assert any(
        item['type'].startswith('topic.') and item['data'].get('topic_id') == topic
        for item in member_sync.data['items']
    )
    assert sum(item['request_id'] == approved.request_id for item in member_sync.data['items']) == 1
    promoted = await call(
        app,
        'content.topic_promote',
        {'id': topic, 'subject_id': alice},
        key=owner_key,
        subject=owner,
    )
    assert promoted.data['role'] == 'admin'
    left = await call(app, 'content.topic_leave', {'id': topic}, key=owner_key, subject=owner)
    assert left.status == 'ok'
    changed = await call(
        app,
        'content.topic_policy_set',
        {'id': topic, 'membership_policy': 'invite'},
        key=alice_key,
        subject=alice,
    )
    assert changed.status == 'ok'
    denied = await call(app, 'content.topic_join', {'id': topic}, key=bob_key, subject=bob)
    assert denied.status == 'error'
    invited = await call(
        app, 'content.topic_invite', {'id': topic, 'subject_id': bob}, key=alice_key, subject=alice
    )
    assert invited.status == 'ok'
    joined = await call(app, 'content.topic_join', {'id': topic}, key=bob_key, subject=bob)
    assert joined.status == 'ok'
    closed = await call(
        app,
        'content.topic_policy_set',
        {'id': topic, 'membership_policy': 'closed'},
        key=alice_key,
        subject=alice,
    )
    assert closed.status == 'ok'
    reopened = await call(
        app,
        'content.topic_policy_set',
        {'id': topic, 'membership_policy': 'open'},
        key=alice_key,
        subject=alice,
    )
    assert reopened.status == 'ok'
    removed = await call(
        app, 'content.topic_remove', {'id': topic, 'subject_id': bob}, key=alice_key, subject=alice
    )
    assert removed.data['status'] == 'removed'
    rejoined = await call(app, 'content.topic_join', {'id': topic}, key=bob_key, subject=bob)
    assert rejoined.data['status'] == 'active'


@pytest.mark.asyncio
async def test_expired_ban_stops_blocking_without_restoring_membership(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'expiry-owner')
    member_key, member, _ = await register(app, 'expiry-member')
    topic = (
        (
            await call(
                app,
                'content.topic_create',
                {'parent': '/main', 'name': 'expiry-topic'},
                key=owner_key,
                subject=owner,
            )
        )
        .resources[0]
        .id
    )
    await call(app, 'content.topic_join', {'id': topic}, key=member_key, subject=member)
    banned = await call(
        app,
        'content.topic_ban',
        {'id': topic, 'subject_id': member, 'expires_at': wire(NOW + timedelta(seconds=60))},
        key=owner_key,
        subject=owner,
    )
    assert banned.status == 'ok'
    app.executor.clock = lambda: NOW + timedelta(seconds=61)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT status FROM topic_memberships WHERE topic=? AND subject=?', (topic, member)
            )[0]
            != 'active'
        )
    joined = await call(app, 'content.topic_join', {'id': topic}, key=member_key, subject=member)
    assert joined.status == 'ok'
    post = await call(
        app,
        'content.post_create',
        {'parent': topic, 'body': 'after expiry'},
        key=member_key,
        subject=member,
    )
    assert post.status == 'ok'


@pytest.mark.asyncio
async def test_virtual_events_compact_are_read_only_and_reason_is_admin_only(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'events-owner')
    member_key, member, _ = await register(app, 'events-member')
    topic = (
        (
            await call(
                app,
                'content.topic_create',
                {'parent': '/main', 'name': 'events-topic'},
                key=owner_key,
                subject=owner,
            )
        )
        .resources[0]
        .id
    )
    await call(app, 'content.topic_join', {'id': topic}, key=member_key, subject=member)
    watched = await call(app, 'communication.watch', {'id': topic}, key=member_key, subject=member)
    assert watched.status == 'ok'
    before = None
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM resources WHERE parent=? AND type=?', (topic, 'post'))[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
        )
    await call(
        app,
        'content.topic_ban',
        {'id': topic, 'subject_id': member, 'reason': 'private moderation note'},
        key=owner_key,
        subject=owner,
    )
    compact = await call(app, 'content.topic_events', {'id': topic}, key=owner_key, subject=owner)
    assert compact.status == 'ok' and compact.data['view'] == 'compact'
    assert len(compact.data['items']) <= 10 and compact.data['items']
    normal = await call(
        app, 'content.topic_events', {'id': topic, 'view': 'normal'}, key=owner_key, subject=owner
    )
    assert any('private moderation note' in str(item) for item in normal.data['items'])
    outsider = await call(
        app, 'content.topic_events', {'id': topic, 'view': 'normal'}, key=member_key, subject=member
    )
    assert outsider.status == 'error' or 'private moderation note' not in str(outsider.data)
    changes = await call(app, 'communication.changes', {}, key=member_key, subject=member)
    assert changes.status == 'ok' and 'private moderation note' not in str(changes.data)
    inbox = await call(app, 'communication.inbox', {}, key=member_key, subject=member)
    assert any(
        item.get('source') == 'topic_governance' and item.get('action') == 'member.ban'
        for item in inbox.data['items']
    )
    for index in range(12):
        policy = 'approval' if index % 2 else 'open'
        changed = await call(
            app,
            'content.topic_policy_set',
            {'id': topic, 'membership_policy': policy},
            key=owner_key,
            subject=owner,
        )
        assert changed.status == 'ok'
    recent = await call(app, 'content.topic_events', {'id': topic}, key=owner_key, subject=owner)
    assert len(recent.data['items']) == 10 and recent.data['cursor']
    async with app.metadata.transaction(write=False) as tx:
        topic_path = await tx.path(topic)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        compact_http = await http.get(topic_path + '/_events.md')
        compact_path = await http.get(topic_path + '/_events.md/compact/10')
        assert compact_http.status_code == compact_path.status_code == 200
        assert compact_http.content == compact_path.content
        assert compact_http.headers['etag'] == compact_path.headers['etag']
        assert len(compact_http.json()['items']) == 10
        normal_query = await http.get(topic_path + '/_events.md?view=normal&limit=10')
        normal_path = await http.get(topic_path + '/_events.md/normal/10')
        assert normal_query.status_code == normal_path.status_code == 200
        assert normal_query.content == normal_path.content
        assert (await http.head(topic_path + '/_events.md')).status_code == 200
        assert (await http.post(topic_path + '/_events.md')).status_code == 405
        assert (
            await http.get(
                topic_path + '/_events.md', headers={'If-None-Match': compact_http.headers['etag']}
            )
        ).status_code == 304
    older = await call(
        app,
        'content.topic_events',
        {'id': topic, 'cursor': recent.data['cursor']},
        key=owner_key,
        subject=owner,
    )
    assert older.status == 'ok' and older.data['items']
    assert {item['c'] for item in recent.data['items']} <= set(recent.data['event_codes'].values())
    async with app.metadata.transaction(write=False) as tx:
        after = (
            tx.one('SELECT COUNT(*) FROM resources WHERE parent=? AND type=?', (topic, 'post'))[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
        )
        assert (
            tx.one('SELECT id FROM resources WHERE parent=? AND name=?', (topic, '_events.md'))
            is None
        )
    assert after == before
    reserved = await call(
        app,
        'content.post_create',
        {'parent': topic, 'name': '_events.md', 'body': 'fake'},
        key=owner_key,
        subject=owner,
    )
    assert reserved.status == 'error'


@pytest.mark.asyncio
async def test_virtual_events_private_topic_authorizes_before_etag(installed):
    app, _ = installed
    owner_key, owner, _ = await register(app, 'events-private-owner')
    created = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'private-events'},
        key=owner_key,
        subject=owner,
    )
    topic = created.resources[0].id
    private = await call(
        app,
        'content.chmod',
        {'id': topic, 'mode': '0700'},
        key=owner_key,
        subject=owner,
        expected=((topic, created.data['generation']),),
    )
    assert private.status == 'ok', wire(private)
    async with app.metadata.transaction(write=False) as tx:
        path = await tx.path(topic)
    packet = request_for(
        'content.topic_events',
        {'id': path, 'view': 'compact', 'limit': 10},
        app.settings.service_url,
        subject=owner,
        signer=owner_key,
        expires_at=NOW + timedelta(seconds=120),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for method in (http.get, http.head):
            denied = await method(path + '/_events.md', headers={'If-None-Match': '*'})
            assert denied.status_code == 403 and 'etag' not in denied.headers
        allowed = await http.get(
            path + '/_events.md', headers={'X-Msg-Request': b64(canonical(packet))}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()['topic_id'] == topic
