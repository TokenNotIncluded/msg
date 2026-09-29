"""/_read and /_r are equivalent read routes, including GraphQL query isolation."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_read_aliases_share_handler_without_redirect_or_auth_gap(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'read-alias-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'alias body'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for suffix in ('/json', '/meta', '/raw', '/history', '/rev/' + revision):
            long = await http.get(f'/_read/{rid}{suffix}')
            short = await http.get(f'/_r/{rid}{suffix}')
            assert long.status_code == short.status_code == 200, suffix
            assert long.content == short.content, suffix
            assert long.headers['etag'] == short.headers['etag'], suffix
            assert 'location' not in long.headers and 'location' not in short.headers
        for prefix in ('/_read', '/_r'):
            assert (await http.post(f'{prefix}/{rid}/json')).status_code == 405
            assert (await http.get(f'{prefix}/{rid}/status')).status_code == 404


@pytest.mark.asyncio
async def test_private_read_aliases_hide_head_meta_history_and_etag(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'read-alias-private')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'private alias'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=uid,
        certs=(cert,),
        expected=((rid, created.data['generation']),),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        signed = request_for(
            'discovery.get',
            {'id': rid, 'view': 'meta'},
            app.settings.service_url,
            signer=key,
            subject=uid,
            expires_at=NOW + timedelta(seconds=120),
        )
        for prefix in ('/_read', '/_r'):
            for suffix in ('/json', '/meta', '/raw', '/history', '/rev/' + revision):
                for method in (http.get, http.head):
                    response = await method(
                        f'{prefix}/{rid}{suffix}', headers={'If-None-Match': '*'}
                    )
                    assert response.status_code == 403, (prefix, suffix, response.text)
                    assert 'etag' not in response.headers
                    assert 'location' not in response.headers
            authorized = await http.get(
                f'{prefix}/{rid}/meta', headers={'X-Msg-Request': b64(canonical(signed))}
            )
            assert authorized.status_code == 200, authorized.text
            assert authorized.json()['id'] == rid


@pytest.mark.asyncio
async def test_graphql_query_and_mutation_are_separated_by_route(installed):
    app, _ = installed
    from msg.transports.graphql import GraphQLAdapter

    mutations = GraphQLAdapter(app).schema.get_type('Mutation').fields
    assert 'identity_register' in mutations and 'identity_register_v2' in mutations
    key, uid, cert = await register(app, 'graphql-boundary')
    read_packet = request_for('discovery.get', {'id': '/main'}, app.settings.service_url)
    write_packet = request_for(
        'content.post_create',
        {'parent': '/main', 'body': 'GraphQL write'},
        app.settings.service_url,
        signer=key,
        subject=uid,
        certificates=(cert,),
        expires_at=NOW + timedelta(seconds=120),
        request_id='graphql-route-write',
    )
    query = {
        'query': 'query($p: JSON!) { call(packet: $p) }',
        'variables': {'p': wire(read_packet)},
    }
    mutation = {
        'query': 'mutation($p: JSON!) { call(packet: $p) }',
        'variables': {'p': wire(write_packet)},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for prefix in ('/_read', '/_r'):
            good = await http.post(prefix + '/graphql', json=query)
            assert good.status_code == 200, good.text
            assert good.json()['data']['call']['status'] == 'ok'
            bad = await http.post(prefix + '/graphql', json=mutation)
            assert bad.status_code == 400
            assert bad.json()['error']['code'] == 'graphql_effect_mismatch'
        rejected_query = await http.post('/-/graphql', json=query)
        assert rejected_query.status_code == 400
        assert rejected_query.json()['error']['code'] == 'graphql_effect_mismatch'
        selection = {
            'query': 'query Q { __typename } mutation M { __typename }',
            'operationName': 'M',
        }
        selected_mutation = await http.post('/_read/graphql', json=selection)
        assert selected_mutation.status_code == 400
        assert selected_mutation.json()['error']['code'] == 'graphql_effect_mismatch'
        selected_query = await http.post('/_r/graphql', json={**selection, 'operationName': 'Q'})
        assert selected_query.status_code == 200
        assert selected_query.json()['data']['__typename'] == 'Query'
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
        accepted = await http.post('/-/graphql', json=mutation)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()['data']['call']['status'] == 'ok'
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1
