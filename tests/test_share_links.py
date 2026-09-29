"""Bearer links stay off by default and authorize exactly one bounded read."""

import hashlib
import os
from datetime import timedelta

import httpx
import pytest
from test_authorization import approve, scoped
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, unb64, wire
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


def proof():
    token = b64(os.urandom(32))
    verifier = 'sha256:' + hashlib.sha256(b'share-link-v1\0' + unb64(token)).hexdigest()
    return token, verifier


@pytest.mark.asyncio
async def test_bearer_link_rejects_get_packet_and_short_paths(installed):
    app, _ = installed
    token, _ = proof()
    packet = request_for(
        'sharing.link_read',
        {'link_id': 'link_missing', 'token': token},
        app.settings.service_url,
        expires_at=NOW + timedelta(seconds=120),
    )
    encoded = b64(canonical(wire(packet)))
    code = build_dictionary(app.registry).code_for('operation', 'sharing.link_read@1')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        named = await http.get('/-/g/sharing.link_read/j/' + encoded)
        short = await http.get('/-/g/' + code)
        assert named.status_code == short.status_code == 405
        posted = await http.post('/-/p/sharing.link_read', content=canonical(wire(packet)))
        assert posted.status_code == 400
        assert posted.json()['error']['code'] == 'share_links_disabled'


@pytest.mark.asyncio
async def test_share_links_require_explicit_system_enable(installed):
    app, root = installed
    key, subject, _ = await register(app, 'link-operator')
    denied = await call(app, 'system.share_links_set', {'enabled': True}, key=key, subject=subject)
    assert denied.status == 'error' and denied.error.code == 'capability_required'
    cert = await approve(
        app,
        root,
        subject,
        key,
        (scoped(app, 'system.config', 'r_root', ('system.share_links_set@1',)),),
    )
    enabled = await call(
        app,
        'system.share_links_set',
        {'enabled': True},
        key=key,
        subject=subject,
        certs=(cert.resource_id,),
    )
    assert enabled.status == 'ok' and enabled.data['enabled'] is True
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('share_links_enabled') is True


@pytest.mark.asyncio
async def test_share_link_is_explicit_bounded_and_revocable(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'link-owner')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'link-folder'},
        key=key,
        subject=owner,
    )
    folder = topic.resources[0].id
    post = await call(
        app,
        'content.post_create',
        {'parent': folder, 'body': 'one private leaf'},
        key=key,
        subject=owner,
    )
    sibling = await call(
        app,
        'content.post_create',
        {'parent': folder, 'body': 'hidden sibling'},
        key=key,
        subject=owner,
    )
    rid = post.resources[0].id
    await call(
        app,
        'content.chmod',
        {'id': folder, 'mode': '0700'},
        key=key,
        subject=owner,
        expected=((folder, topic.data['generation']),),
    )
    token, verifier = proof()
    args = {'resource': rid, 'verifier': verifier, 'expires_at': wire(NOW + timedelta(hours=1))}
    off = await call(app, 'sharing.link_create', args, key=key, subject=owner)
    assert off.error.code == 'share_links_disabled'
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('share_links_enabled', True)
    created = await call(app, 'sharing.link_create', args, key=key, subject=owner)
    assert created.status == 'ok', wire(created)
    link = created.data['link']
    assert token not in str(created.data) and verifier not in str(created.data)
    assert created.data['endpoint'] == '/-/p/sharing.link_read'
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM audit')[0],
            tx.setting('authorization_epoch', 0),
        )
        assert tx.one('SELECT verifier FROM share_links WHERE id=?', (link['id'],))[0] == verifier
    wrong = await call(
        app, 'sharing.link_read', {'link_id': link['id'], 'token': b64(os.urandom(32))}
    )
    missing = await call(app, 'sharing.link_read', {'link_id': 'link_missing', 'token': token})
    for denied in (wrong, missing):
        assert denied.status == 'error' and denied.error.code == 'share_link_unavailable'
    read = await call(app, 'sharing.link_read', {'link_id': link['id'], 'token': token})
    assert read.status == 'ok', (read.error.code if read.error else None, wire(read))
    assert read.data['id'] == rid and 'hidden sibling' not in str(read.data)
    assert set(read.data) == {'id', 'revision', 'media_type', 'data'}
    assert 'one private leaf' in unb64(read.data['data']).decode()
    anonymous_folder = await call(app, 'discovery.list', {'parent': folder})
    anonymous_sibling = await call(app, 'discovery.get', {'id': sibling.resources[0].id})
    assert anonymous_folder.status == anonymous_sibling.status == 'error'
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM audit')[0],
            tx.setting('authorization_epoch', 0),
        ) == before
    revoked = await call(
        app, 'sharing.link_revoke', {'link_id': link['id']}, key=key, subject=owner
    )
    assert revoked.status == 'ok'
    after = await call(app, 'sharing.link_read', {'link_id': link['id'], 'token': token})
    assert after.error.code == 'share_link_unavailable'


@pytest.mark.asyncio
async def test_share_link_rechecks_owner_acl_expiry_and_global_switch(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'link-policy-owner')
    post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'policy body'},
        key=key,
        subject=owner,
    )
    rid = post.resources[0].id
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('share_links_enabled', True)
    token, verifier = proof()
    created = await call(
        app,
        'sharing.link_create',
        {'resource': rid, 'verifier': verifier, 'expires_at': wire(NOW + timedelta(seconds=2))},
        key=key,
        subject=owner,
    )
    assert created.status == 'ok', wire(created)
    lid = created.data['link']['id']

    def read():
        return call(app, 'sharing.link_read', {'link_id': lid, 'token': token})

    first = await read()
    assert first.status == 'ok', (first.error.code if first.error else None, wire(first))
    hidden = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0000'},
        key=key,
        subject=owner,
        expected=((rid, post.data['generation']),),
    )
    assert hidden.status == 'ok', wire(hidden)
    assert (await read()).error.code == 'share_link_unavailable'
    restored = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=owner,
        expected=((rid, hidden.data['generation']),),
    )
    assert restored.status == 'ok', wire(restored)
    assert (await read()).status == 'ok'
    app.executor.clock = lambda: NOW + timedelta(seconds=3)
    assert (await read()).error.code == 'share_link_unavailable'
    app.executor.clock = lambda: NOW
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('share_links_enabled', False)
    assert (await read()).error.code == 'share_links_disabled'
