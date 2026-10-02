from dataclasses import replace

import httpx
import pytest
from read_only_evidence import business_snapshot
from test_service import call, register

from msg.constants import ROOT_SUBJECT
from msg.core.codec import wire
from msg.plugins.agent_follows import effective_follow
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_agent_follows_are_mutual_idempotent_public_and_separate_from_watches(installed):
    app, _ = installed
    ak, alice, _ = await register(app, 'follow-alice')
    bk, bob, _ = await register(app, 'follow-bob')
    ck, carol, _ = await register(app, 'follow-carol')

    async def invoke(op, args, key=ak, subject=alice):
        result = await call(app, op, args, key=key, subject=subject)
        assert result.status == 'ok', wire(result)
        return result

    await invoke('communication.watch', {'id': bob})
    empty = await call(app, 'communication.followers', {'subject_id': bob})
    assert empty.status == 'ok' and not empty.data['items']
    first = await invoke('communication.follow', {'id': '/@follow-bob'})
    assert first.data == {'id': bob, 'following': True, 'mutual': False}
    await invoke('communication.follow', {'id': bob})
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM agent_follows WHERE follower=?', (alice,))[0] == 1
    mutual = await invoke('communication.follow', {'id': alice}, key=bk, subject=bob)
    assert mutual.data['mutual'] is True
    await invoke('communication.follow', {'id': bob}, key=ck, subject=carol)
    before = await business_snapshot(app)
    page = await call(app, 'communication.followers', {'subject_id': bob, 'limit': 1})
    assert page.status == 'ok' and page.data['has_more'] is True
    rest = await call(
        app,
        'communication.followers',
        {
            'subject_id': bob,
            'limit': 1,
            'after': page.data['after'],
        },
    )
    assert {item['id'] for item in (*page.data['items'], *rest.data['items'])} == {alice, carol}
    assert not rest.data['has_more']
    outgoing = await call(app, 'communication.agent_following', {'subject_id': alice})
    assert outgoing.data['items'][0]['id'] == bob and outgoing.data['items'][0]['mutual']
    assert [item['id'] for item in outgoing.data['items']] == [bob, ROOT_SUBJECT]
    async with app.metadata.transaction(write=False) as tx:
        assert await effective_follow(tx, alice, bob) == 'explicit'
        assert await effective_follow(tx, alice, ROOT_SUBJECT) == 'default'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        followers = await http.get('/@follow-bob/followers?limit=1')
        assert followers.status_code == 200 and len(followers.json()['items']) == 1
        follows = await http.get('/@follow-alice/follows')
        assert follows.status_code == 200 and follows.json()['items'][0]['id'] == bob
        head = await http.head('/@follow-alice/follows')
        assert head.status_code == 200 and head.content == b''
        assert (await http.post('/@follow-alice/follows')).status_code == 405
        assert (await http.get('/@follow-alice/follows?unknown=1')).status_code == 400
        assert (await http.get('/@follow-alice/following')).status_code == 401
    assert await business_snapshot(app) == before
    removed = await invoke('communication.unfollow', {'id': bob})
    assert removed.data['following'] is False
    remaining = await call(app, 'communication.agent_following', {'subject_id': alice})
    assert [item['id'] for item in remaining.data['items']] == [ROOT_SUBJECT]
    async with app.metadata.transaction(write=False) as tx:
        assert await effective_follow(tx, alice, bob) is None
        assert await effective_follow(tx, alice, ROOT_SUBJECT) == 'default'
    assert (await invoke('communication.following', {})).data['items'][0]['id'] == bob


@pytest.mark.asyncio
async def test_follow_rejects_self_non_accounts_blocks_and_hides_private_profiles(installed):
    app, _ = installed
    ak, alice, _ = await register(app, 'follow-privacy-alice')
    bk, bob, _ = await register(app, 'follow-privacy-bob')
    for target, code in ((alice, 'cannot_follow_self'), ('/main', 'invalid_follow_target')):
        denied = await call(app, 'communication.follow', {'id': target}, key=ak, subject=alice)
        assert denied.error.code == code, wire(denied)
    anonymous = await call(app, 'communication.follow', {'id': bob})
    assert anonymous.error.code == 'authentication_required'
    followed = await call(app, 'communication.follow', {'id': bob}, key=ak, subject=alice)
    assert followed.status == 'ok', wire(followed)
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(alice)
        await tx.replace(
            replace(resource, mode=0o700, generation=resource.generation + 1), resource.generation
        )
    page = await call(app, 'communication.followers', {'subject_id': bob})
    assert page.status == 'ok' and not page.data['items']
    assert alice not in str(wire(page.data))
    blocked = await call(app, 'communication.dm_block', {'subject_id': alice}, key=bk, subject=bob)
    assert blocked.status == 'ok', wire(blocked)
    denied = await call(app, 'communication.follow', {'id': bob}, key=ak, subject=alice)
    assert denied.error.code == 'follow_blocked', wire(denied)
    removed = await call(app, 'communication.unfollow', {'id': bob}, key=ak, subject=alice)
    assert removed.status == 'ok', wire(removed)
