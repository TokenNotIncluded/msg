"""Tags are normalized resource metadata; index/search never bypass ACL."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_foundation import item
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.storage.postgres import PostgresMetadataStore
from msg.storage.sqlite import FakeMetadataStore
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_taggable_types_normalize_tags_and_preserve_revision(installed):
    app, _ = installed
    assert all(app.registry.resource_type(kind).taggable for kind in ('post', 'topic', 'repo'))
    key, uid, cert = await register(app, 'tag-normalization')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'tagged'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    tagged = await call(
        app,
        'content.tags_set',
        {'id': rid, 'tags': ['  Café ', 'café', 'ＡＩ', 'ai']},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, created.data['generation']),),
    )
    assert tagged.status == 'ok', tagged.error
    projection = await call(app, 'discovery.get', {'id': rid}, key=key, subject=uid, certs=(cert,))
    assert projection.data['tags'] == ('ai', 'café')
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(rid)
        assert resource.tags == ('ai', 'café')
        assert resource.revision == revision
        assert tx.rows(
            'SELECT tag FROM resource_tags WHERE resource_id=? ORDER BY tag', (rid,)
        ) == [('ai',), ('café',)]
    duplicate = await call(
        app,
        'content.tags_set',
        {'id': rid, 'tags': ['new']},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, created.data['generation']),),
    )
    assert duplicate.error.code == 'generation_conflict'
    invalid = await call(
        app,
        'content.tags_set',
        {'id': rid, 'tags': ['bad/tag']},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, tagged.data['generation']),),
    )
    assert invalid.error.code == 'invalid_tag'


@pytest.mark.asyncio
async def test_topic_and_repo_use_the_same_tag_index(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'tag-topic-repo')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'tag-topic'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    repo = await call(
        app,
        'git.create',
        {'parent': uid, 'name': 'tag-repo.git'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert topic.status == repo.status == 'ok'
    ids = []
    for result in (topic, repo):
        rid = result.resources[0].id
        tagged = await call(
            app,
            'content.tags_set',
            {'id': rid, 'tags': ['Shared']},
            key=key,
            subject=uid,
            certs=(cert,),
            expected=((rid, result.data['generation']),),
        )
        assert tagged.status == 'ok', tagged.error
        ids.append(rid)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        response = await http.get('/_i/by-tag/shared')
        assert response.status_code == 200, response.text
        assert {row['id'] for row in response.json()['items']} == set(ids)


@pytest.mark.asyncio
async def test_by_tag_index_aliases_paginate_and_hide_revoked_resources(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'tag-index')
    posts = []
    for number in range(3):
        created = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'tag post {number}'},
            key=key,
            subject=uid,
            certs=(cert,),
        )
        rid = created.resources[0].id
        tagged = await call(
            app,
            'content.tags_set',
            {'id': rid, 'tags': ['Café']},
            key=key,
            subject=uid,
            certs=(cert,),
            expected=((rid, created.data['generation']),),
        )
        assert tagged.status == 'ok'
        posts.append((rid, tagged.data['generation']))
    async with app.metadata.transaction(write=False) as tx:
        events_before = tx.one('SELECT COUNT(*) FROM events')[0]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        long = await http.get('/_index/by-tag/caf%C3%A9?limit=1')
        short = await http.get('/_i/by-tag/caf%C3%A9?limit=1')
        assert long.status_code == short.status_code == 200, (long.text, short.text)
        assert long.json() == short.json()
        seen = []
        page = long.json()
        while True:
            assert len(page['items']) == 1
            seen.append(page['items'][0]['id'])
            if not page.get('next'):
                break
            assert page['next'].startswith('/_i/by-tag/')
            response = await http.get(page['next'])
            assert response.status_code == 200, response.text
            page = response.json()
        assert set(seen) == {rid for rid, _ in posts}
        search_long = await http.get('/_search?tag=caf%C3%A9&limit=2')
        search_short = await http.get('/_s?tag=caf%C3%A9&limit=2')
        assert search_long.status_code == search_short.status_code == 200
        assert search_long.json() == search_short.json()
        assert len(search_long.json()['items']) == 2
        assert (await http.post('/_index/by-tag/caf%C3%A9')).status_code == 405
        assert (await http.get('/_index/by-id/' + posts[0][0])).status_code == 404
        assert (await http.get('/_search?tag=caf%C3%A9&unexpected=1')).status_code == 400
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM events')[0] == events_before


@pytest.mark.asyncio
async def test_by_tag_visibility_changes_immediately_and_signed_search_can_read_private(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'tag-private')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'secret tag'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid = created.resources[0].id
    tagged = await call(
        app,
        'content.tags_set',
        {'id': rid, 'tags': ['Private']},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, created.data['generation']),),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        before = await http.get('/_index/by-tag/private')
        assert [item['id'] for item in before.json()['items']] == [rid]
    await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, tagged.data['generation']),),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        path = '/_index/by-tag/private'
        anonymous = await http.get(path)
        assert anonymous.status_code == 200
        assert anonymous.json()['items'] == []
        assert 'etag' not in anonymous.headers
        packet = request_for(
            'discovery.list',
            {'tag': 'private', 'limit': 50},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=120),
        )
        permitted = await http.get(path, headers={'X-Msg-Request': b64(canonical(packet))})
        assert permitted.status_code == 200, permitted.text
        assert [item['id'] for item in permitted.json()['items']] == [rid]
        wrong = await http.get(
            path,
            headers={
                'X-Msg-Request': b64(
                    canonical(
                        request_for(
                            'discovery.list',
                            {'tag': 'other', 'limit': 50},
                            app.settings.service_url,
                            signer=key,
                            subject=uid,
                            expires_at=NOW + timedelta(seconds=120),
                        )
                    )
                )
            },
        )
        assert wrong.status_code == 400
        assert wrong.json()['error']['code'] == 'representation_mismatch'

        changed = await call(
            app,
            'content.tags_set',
            {'id': rid, 'tags': []},
            key=key,
            subject=uid,
            certs=(cert,),
            expected=((rid, tagged.data['generation'] + 1),),
        )
        assert changed.status == 'ok', changed.error
        removed = await http.get(path, headers={'X-Msg-Request': b64(canonical(packet))})
        assert removed.status_code == 200 and removed.json()['items'] == []


@pytest.mark.parametrize('backend', ('postgres', 'sqlite'))
@pytest.mark.asyncio
async def test_tags_index_has_same_transactional_contract_on_both_stores(backend, pg_dsn):
    store = PostgresMetadataStore(pg_dsn) if backend == 'postgres' else FakeMetadataStore()
    try:
        async with store.transaction(write=True) as tx:
            await tx.insert(item('root'))
            await tx.insert(replace(item('post', 'root'), type='post', tags=('ai',)))
        async with store.transaction(write=True) as tx:
            old = await tx.resource('post')
            assert old.tags == ('ai',)
            assert tx.rows('SELECT resource_id FROM resource_tags WHERE tag=?', ('ai',)) == [
                ('post',)
            ]
            await tx.replace(replace(old, tags=('café',), generation=1), 0)
        async with store.transaction(write=False) as tx:
            assert (await tx.resource('post')).tags == ('café',)
            assert tx.rows('SELECT tag FROM resource_tags WHERE resource_id=?', ('post',)) == [
                ('café',)
            ]
    finally:
        await store.close()
