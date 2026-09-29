"""Sealed transfer bytes can describe a short-lived, non-authorizing ReadQuery."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


async def path_call(http, app, name, args, key, subject, request_id):
    packet = request_for(
        name,
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        request_id=request_id,
        expires_at=NOW + timedelta(seconds=120),
    )
    return await http.get(
        '/-/g/' + name + '/j/' + b64(canonical(packet)), headers={'User-Agent': 'AgentRuntime/1.0'}
    )


async def state(app):
    async with app.metadata.transaction(write=False) as tx:
        return (
            tx.one('SELECT COUNT(*) FROM events')[0],
            tx.one('SELECT COUNT(*) FROM jobs')[0],
            tx.one('SELECT COUNT(*) FROM revisions')[0],
            tx.one('SELECT SUM(generation) FROM resources')[0],
            tx.one('SELECT COUNT(*) FROM credentials')[0],
            tx.one('SELECT COUNT(*) FROM transfers')[0],
            tx.one('SELECT COUNT(*) FROM chunks')[0],
            tx.one('SELECT COUNT(*) FROM reactions')[0],
            tx.one('SELECT COUNT(*) FROM messages')[0],
        )


@pytest.mark.asyncio
async def test_transfer_get_path_parts_seal_and_query_ref_readback(installed):
    app, _ = installed
    key, user, _ = await register(app, 'query-ref-owner')
    long_term = 'needle' + ('x' * 9000)
    posts = []
    for _ in range(3):
        post = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': long_term},
            key=key,
            subject=user,
        )
        assert post.status == 'ok'
        posts.append(post.resources[0].id)
    args = {
        'parent': '/main',
        'type': 'post',
        'query': long_term,
        'limit': 2,
        'fields': ['id', 'name'],
        'sort': 'id',
    }
    payload = canonical({'version': 1, 'kind': 'read', 'arguments': args})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        too_long = await http.get('/_r/q/1/r/%2Fmain/f/i,n/n/2/t/post/' + long_term)
        assert too_long.status_code == 413
        opened = await path_call(
            http,
            app,
            'transfer.open',
            {
                'direction': 'upload',
                'size': len(payload),
                'digest': digest(payload),
                'media_type': 'application/vnd.msg.read-query+json',
            },
            key,
            user,
            'query-open',
        )
        assert opened.status_code == 200, opened.text
        transfer_id = opened.json()['data']['transfer_id']
        part_size = opened.json()['data']['part_bytes']
        for index, offset in enumerate(range(0, len(payload), part_size)):
            chunk = payload[offset : offset + part_size]
            put = await path_call(
                http,
                app,
                'transfer.part_put',
                {
                    'transfer_id': transfer_id,
                    'offset': offset,
                    'data': b64(chunk),
                    'digest': digest(chunk),
                },
                key,
                user,
                f'query-part-{index}',
            )
            assert put.status_code == 200, put.text
        sealed = await path_call(
            http,
            app,
            'transfer.seal',
            {
                'transfer_id': transfer_id,
                'final_size': len(payload),
                'final_digest': digest(payload),
            },
            key,
            user,
            'query-seal',
        )
        assert sealed.status_code == 200, sealed.text
        issued = await path_call(
            http,
            app,
            'transfer.query_seal',
            {'transfer_id': transfer_id},
            key,
            user,
            'query-ref-seal',
        )
        assert issued.status_code == 200, issued.text
        query_ref = issued.json()['data']['query_ref']
        assert issued.json()['data']['next'] == '/_r/q/' + query_ref
        assert long_term not in query_ref
        before = await state(app)
        read_packet = request_for(
            'transfer.query_get',
            {'query_ref': query_ref},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        long = await http.get('/_read/q/' + query_ref + '/p/' + b64(canonical(read_packet)))
        short = await http.get('/_r/q/' + query_ref + '/p/' + b64(canonical(read_packet)))
        assert long.status_code == short.status_code == 200, (long.text, short.text)
        assert long.content == short.content
        assert long.headers['etag'] == short.headers['etag']
        assert long.headers['cache-control'] == 'no-store'
        assert len(long.json()['items']) == 2
        assert long.json()['next'].startswith('/_r/q/' + query_ref + '/c/')
        assert len(long.json()['next'].encode()) < app.settings.server.limits.max_path_bytes
        page_cursor = long.json()['cursor']
        assert long_term not in page_cursor
        continued = request_for(
            'transfer.query_get',
            {'query_ref': query_ref, 'cursor': page_cursor},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        next_page = await http.get(long.json()['next'] + '/p/' + b64(canonical(continued)))
        assert next_page.status_code == 200, next_page.text
        found = {item['id'] for item in long.json()['items'] + next_page.json()['items']}
        assert found == set(posts)
        assert await state(app) == before
        source = sealed.json()['resources'][0]['id']
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(source)).generation
        revoked = await call(
            app,
            'content.chmod',
            {'id': source, 'mode': '0000'},
            key=key,
            subject=user,
            expected=((source, generation),),
        )
        assert revoked.status == 'ok', wire(revoked)
        after_revoke = await state(app)
        denied = await http.get(long.json()['next'] + '/p/' + b64(canonical(continued)))
        assert denied.status_code == 403
        assert await state(app) == after_revoke


@pytest.mark.asyncio
async def test_query_ref_is_not_bearer_and_rechecks_digest_expiry_and_source_access(installed):
    app, _ = installed
    key, user, _ = await register(app, 'query-ref-private')
    other_key, other, _ = await register(app, 'query-ref-other')
    descriptor = canonical({
        'version': 1,
        'kind': 'read',
        'arguments': {'parent': '/main', 'limit': 1, 'fields': ['id']},
    })
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
    sealed = await call(
        app,
        'transfer.seal',
        {'transfer_id': tid, 'final_size': len(descriptor), 'final_digest': digest(descriptor)},
        key=key,
        subject=user,
    )
    issued = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=user)
    assert issued.status == 'ok', wire(issued)
    token = issued.data['query_ref']
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        anonymous = await http.get('/_r/q/' + token)
        assert anonymous.status_code in {400, 401, 403}
        alien = request_for(
            'transfer.query_get',
            {'query_ref': token},
            app.settings.service_url,
            signer=other_key,
            subject=other,
            expires_at=NOW + timedelta(seconds=60),
        )
        cross = await http.get('/_r/q/' + token + '/p/' + b64(canonical(alien)))
        assert cross.status_code == 403
        assert cross.json()['error']['code'] == 'query_ref_principal_mismatch'
        decoded = app.cursors.inspect(token)
        bad_query = {**decoded['query'], 'digest': 'sha256:' + '0' * 64}
        wrong = app.cursors.encode('query-ref', bad_query, decoded['position'])
        wrong_packet = request_for(
            'transfer.query_get',
            {'query_ref': wrong},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        mismatch = await http.get('/_r/q/' + wrong + '/p/' + b64(canonical(wrong_packet)))
        assert mismatch.status_code == 400
        assert mismatch.json()['error']['code'] == 'query_ref_digest_mismatch'
        expired = app.cursors.encode(
            'query-ref',
            decoded['query'],
            {**decoded['position'], 'expires_at': wire(NOW - timedelta(seconds=1))},
        )
        expired_packet = request_for(
            'transfer.query_get',
            {'query_ref': expired},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        stale = await http.get('/_r/q/' + expired + '/p/' + b64(canonical(expired_packet)))
        assert stale.status_code == 400
        assert stale.json()['error']['code'] == 'query_ref_expired'
        source = sealed.resources[0].id
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource(source)).generation
        revoked = await call(
            app,
            'content.chmod',
            {'id': source, 'mode': '0000'},
            key=key,
            subject=user,
            expected=((source, generation),),
        )
        assert revoked.status == 'ok', wire(revoked)
        owner_packet = request_for(
            'transfer.query_get',
            {'query_ref': token},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=60),
        )
        denied = await http.get('/_r/q/' + token + '/p/' + b64(canonical(owner_packet)))
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_query_ref_rejects_non_read_descriptor_without_executing(installed):
    app, _ = installed
    key, user, _ = await register(app, 'query-ref-deny')
    descriptor = canonical({
        'version': 1,
        'kind': 'script',
        'operation': 'tool.run',
        'arguments': {'id': 'tool_dns'},
    })
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
    before = await state(app)
    rejected = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=user)
    assert rejected.error.code == 'invalid_query_ref', wire(rejected)
    assert await state(app) == before
