"""ReadQuery v2 gives every expanded collection its own reauthorized page."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_nested_query_string_path_and_query_ref_share_v2_contract(installed):
    app, _ = installed
    key, user, _ = await register(app, 'nested-path-owner')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'nested-path'},
        key=key,
        subject=user,
    )
    for index in range(2):
        assert (
            await call(
                app,
                'content.post_create',
                {'parent': topic.resources[0].id, 'body': f'nested path {index}'},
                key=key,
                subject=user,
            )
        ).status == 'ok'
    query = (
        '/_read/query?version=2&root=%2Fmain&first=10&select=id,type&expand=children&nested_first=1'
    )
    path = '/_r/q/2/r/%2Fmain/n/10/f/i,t/x/children/nf/1'
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        left = await http.get(query)
        right = await http.get(path)
        assert left.status_code == right.status_code == 200, (left.text, right.text)
        assert left.content == right.content and left.headers['etag'] == right.headers['etag']
        topic_row = next(row for row in left.json()['items'] if row['id'] == topic.resources[0].id)
        children = topic_row['collections']['children']
        assert children['pageInfo']['hasNextPage'] and len(children['items']) == 1
        continued = await http.get(children['next'])
        assert continued.status_code == 200, continued.text
        assert continued.json()['items'][0]['id'] != children['items'][0]['id']
    assert await business_state(app) == before
    args = {
        'parent': '/main',
        'limit': 10,
        'fields': ['id', 'type'],
        'sort': 'id',
        'expand': ['children'],
        'nested_first': 1,
    }
    descriptor = canonical({'version': 1, 'kind': 'read', 'arguments': args})
    opened = await call(
        app,
        'transfer.open',
        {
            'direction': 'upload',
            'size': len(descriptor),
            'digest': digest(descriptor),
            'media_type': 'application/vnd.msg.read-query+json',
        },
        key=key,
        subject=user,
    )
    tid = opened.data['transfer_id']
    await call(
        app,
        'transfer.part_put',
        {'transfer_id': tid, 'offset': 0, 'data': b64(descriptor), 'digest': digest(descriptor)},
        key=key,
        subject=user,
    )
    await call(
        app,
        'transfer.seal',
        {'transfer_id': tid, 'final_size': len(descriptor), 'final_digest': digest(descriptor)},
        key=key,
        subject=user,
    )
    sealed = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=user)
    assert sealed.status == 'ok', wire(sealed)
    token = sealed.data['query_ref']
    proof = request_for(
        'transfer.query_get',
        {'query_ref': token},
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=60),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        resolved = await http.get('/_r/q/' + token + '/p/' + b64(canonical(proof)))
        assert resolved.status_code == 200, resolved.text
        assert any(
            row['id'] == topic.resources[0].id and 'children' in row['collections']
            for row in resolved.json()['items']
        )


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM jobs')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
        )


@pytest.mark.asyncio
async def test_nested_collections_have_independent_pages_and_no_business_writes(installed):
    app, _ = installed
    key, user, _ = await register(app, 'nested-reader')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'nested-query'},
        key=key,
        subject=user,
    )
    topic_id = topic.resources[0].id
    post = await call(
        app,
        'content.post_create',
        {'parent': topic_id, 'body': 'a root post'},
        key=key,
        subject=user,
    )
    post_id = post.resources[0].id
    subtree = await call(
        app, 'content.topic_create', {'parent': topic_id, 'name': 'subtree'}, key=key, subject=user
    )
    subtree_id = subtree.resources[0].id
    for index in range(3):
        assert (
            await call(
                app,
                'discussion.reply',
                {'target': {'id': post_id}, 'body': f'reply {index}'},
                key=key,
                subject=user,
            )
        ).status == 'ok'
        assert (
            await call(
                app,
                'content.post_create',
                {'parent': subtree_id, 'body': f'child {index}'},
                key=key,
                subject=user,
            )
        ).status == 'ok'
    before = await business_state(app)
    args = {
        'parent': topic_id,
        'limit': 10,
        'fields': ['id', 'type'],
        'expand': ['children', 'replies'],
        'nested_first': 1,
    }
    result = await call(app, 'discovery.read_query', args, contract_version=2)
    assert result.status == 'ok', wire(result)
    rows = {row['id']: row for row in result.data['items']}
    assert post_id in rows and subtree_id in rows
    replies = rows[post_id]['collections']['replies']
    children = rows[subtree_id]['collections']['children']
    assert len(replies['items']) == len(children['items']) == 1
    assert replies['pageInfo']['hasNextPage'] and children['pageInfo']['hasNextPage']
    assert replies['pageInfo']['endCursor'] != children['pageInfo']['endCursor']
    assert replies['next'].startswith('/_r/c/')
    assert children['next'].startswith('/_r/c/')
    top = await call(app, 'discovery.read_query', {**args, 'limit': 1}, contract_version=2)
    assert top.status == 'ok', wire(top)
    assert top.data['pageInfo']['hasNextPage']
    resumed = await call(
        app,
        'discovery.read_query',
        {'cursor': top.data['pageInfo']['endCursor']},
        contract_version=2,
    )
    assert resumed.status == 'ok', wire(resumed)
    assert resumed.data['items'][0]['collections']
    assert resumed.data['items'][0]['id'] != top.data['items'][0]['id']
    for first in (replies, children):
        cursor = first['pageInfo']['endCursor']
        seen = [first['items'][0]]
        while cursor:
            result = await call(app, 'discovery.read_query', {'cursor': cursor}, contract_version=2)
            assert result.status == 'ok', wire(result)
            page = result.data
            seen.extend(page['items'])
            cursor = page['pageInfo']['endCursor'] if page['pageInfo']['hasNextPage'] else None
        assert len(seen) == 3
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_nested_continuation_filters_child_revoked_after_first_page(installed):
    app, _ = installed
    key, user, _ = await register(app, 'nested-child-revoke')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'nested-child-revoke-topic'},
        key=key,
        subject=user,
    )
    rid = topic.resources[0].id
    created = []
    for index in range(3):
        result = await call(
            app,
            'content.post_create',
            {'parent': rid, 'body': f'child {index}'},
            key=key,
            subject=user,
        )
        created.append((result.resources[0].id, result.data['generation']))
    first = await call(
        app,
        'discovery.read_query',
        {'parent': rid, 'collection': 'children', 'limit': 1},
        contract_version=2,
    )
    assert first.status == 'ok', wire(first)
    cursor = first.data['pageInfo']['endCursor']
    first_id = first.data['items'][0]['id']
    revoked_id, generation = sorted(item for item in created if item[0] > first_id)[0]
    assert (
        await call(
            app,
            'content.chmod',
            {'id': revoked_id, 'mode': '0000'},
            key=key,
            subject=user,
            expected=((revoked_id, generation),),
        )
    ).status == 'ok'
    continued = await call(app, 'discovery.read_query', {'cursor': cursor}, contract_version=2)
    assert continued.status == 'ok', wire(continued)
    assert revoked_id not in {item['id'] for item in continued.data['items']}


@pytest.mark.asyncio
async def test_nested_cursor_rechecks_current_parent_access_and_binds_principal(installed):
    app, _ = installed
    owner, user, _ = await register(app, 'nested-private-owner')
    outsider, other, _ = await register(app, 'nested-private-other')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'nested-private'},
        key=owner,
        subject=user,
    )
    rid = topic.resources[0].id
    for index in range(3):
        assert (
            await call(
                app,
                'content.post_create',
                {'parent': rid, 'body': str(index)},
                key=owner,
                subject=user,
            )
        ).status == 'ok'
    assert (
        await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0700'},
            key=owner,
            subject=user,
            expected=((rid, topic.data['generation']),),
        )
    ).status == 'ok'
    first = await call(
        app,
        'discovery.read_query',
        {'parent': rid, 'collection': 'children', 'limit': 1},
        key=owner,
        subject=user,
        contract_version=2,
    )
    assert first.status == 'ok', wire(first)
    cursor = first.data['pageInfo']['endCursor']
    assert cursor
    denied = await call(
        app,
        'discovery.read_query',
        {'cursor': cursor},
        key=outsider,
        subject=other,
        contract_version=2,
    )
    assert denied.error.code == 'cursor_principal_mismatch'
    allowed = await call(
        app, 'discovery.read_query', {'cursor': cursor}, key=owner, subject=user, contract_version=2
    )
    assert allowed.status == 'ok', wire(allowed)
    assert (
        await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0000'},
            key=owner,
            subject=user,
            expected=((rid, topic.data['generation'] + 1),),
        )
    ).status == 'ok'
    revoked = await call(
        app, 'discovery.read_query', {'cursor': cursor}, key=owner, subject=user, contract_version=2
    )
    assert revoked.error.code == 'permission_denied'
    app.clock = lambda: NOW + timedelta(minutes=16)
    app.executor.clock = app.clock
    expired = await call(
        app, 'discovery.read_query', {'cursor': cursor}, key=owner, subject=user, contract_version=2
    )
    assert expired.error.code == 'cursor_expired'


@pytest.mark.asyncio
async def test_read_query_v1_expansion_semantics_remain_closed(installed):
    app, _ = installed
    old = await call(app, 'discovery.read_query', {'parent': '/main', 'expand': ['children']})
    assert old.error.code == 'schema_validation'
    costly = await call(
        app,
        'discovery.read_query',
        {'parent': '/main', 'limit': 11, 'expand': ['children'], 'nested_first': 10},
        contract_version=2,
    )
    assert costly.error.code == 'query_cost_exceeded'
