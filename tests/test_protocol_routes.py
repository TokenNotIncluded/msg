"""The /-/ namespace is the only HTTP entry point that may execute operations."""
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_post_and_path_get_execute_only_under_protocol_namespace(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'protocol-agent')
    packet = request_for('content.post_create', {'parent': '/main', 'body': 'routed post'},
                         app.settings.service_url, signer=key, subject=uid,
                         request_id='protocol-post', expires_at=NOW + timedelta(seconds=90))
    old_path = '/!content.post_create/run/j/' + b64(canonical(packet))
    new_path = '/-/g/content.post_create/j/' + b64(canonical(packet))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        assert (await http.post('/!content.post_create', content=canonical(packet))).status_code == 404
        assert (await http.get(old_path)).status_code == 404
        assert (await http.post('/main', content=canonical(packet))).status_code == 405
        assert (await http.head(new_path)).status_code == 200
        assert (await http.get('/-/p/content.post_create')).status_code == 405
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0

        first = await http.post('/-/p/content.post_create', content=canonical(packet))
        assert first.status_code == 200, first.text
        assert first.json()['status'] == 'ok'
        read_packet = request_for('discovery.get', {'id': '/main'}, app.settings.service_url)
        read = await http.post('/-/p/discovery.get', content=canonical(read_packet))
        assert read.status_code == 200, read.text
        assert read.json()['operation'] == 'discovery.get'
        replay = await http.get(new_path)
        assert replay.status_code == 200, replay.text
        assert replay.json()['replayed'] is True
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 1


@pytest.mark.asyncio
async def test_mcp_moves_into_protocol_namespace_and_resource_queries_remain_reads(installed):
    app, _ = installed
    message = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
               'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                          'clientInfo': {'name': 'test', 'version': '1'}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        assert (await http.post('/mcp', json=message)).status_code == 404
        assert (await http.post('/~discovery.get', content=b'{}')).status_code == 404
        response = await http.post('/-/mcp', json=message)
        assert response.status_code == 200, response.text
        assert response.json()['result']['capabilities']['tools'] is not None
        ordinary = await http.get('/main?operation=content.post_create&body=hidden')
        assert ordinary.status_code == 200, ordinary.text
        assert (await http.put('/main?operation=content.post_create')).status_code == 405
        assert (await http.get('/-/unknown')).status_code == 404


@pytest.mark.asyncio
async def test_short_code_dictionary_and_percent_encoded_direct_read(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    read_code = dictionary.code_for('operation', 'discovery.get@1')
    write_code = dictionary.code_for('operation', 'content.post_create@1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        listing = await http.get('/-/d')
        assert listing.status_code == 200, listing.text
        assert listing.headers['etag'] == dictionary.index_etag
        assert listing.json() == dictionary.index_document
        assert (await http.get('/-/d', headers={'If-None-Match': dictionary.index_etag})).status_code == 304
        namespace = await http.get('/-/d/content')
        assert namespace.status_code == 200, namespace.text
        assert namespace.json()['operations']
        assert all(op['namespace'] == 'content' for op in namespace.json()['operations'])
        operation = await http.get('/-/d/content.post_create')
        assert operation.status_code == 200, operation.text
        assert [op['name'] for op in operation.json()['operations']] == ['content.post_create']
        assert (await http.get('/-/d/content.post_create',
                               headers={'If-None-Match': operation.headers['etag']})).status_code == 304
        schema = await http.get('/-/schema')
        assert schema.status_code == 200, schema.text
        assert schema.headers['etag'] == dictionary.schema_etag
        direct = await http.get(f'/-/g/{read_code}/%2Fmain')
        assert direct.status_code == 200, direct.text
        assert direct.json()['operation'] == 'discovery.get'
        denied = await http.get(f'/-/g/{write_code}/%2Fmain')
        assert denied.status_code == 400
        assert denied.json()['error']['code'] == 'direct_path_read_only'
        for malformed in ('%GG', '%FF'):
            invalid = await http.get(f'/-/g/{read_code}/{malformed}')
            assert invalid.status_code == 400, invalid.text
            assert invalid.json()['error']['code'] == 'invalid_path'
        limit_field = dictionary.code_for('field', 'discovery.get@1:limit')
        repeated = await http.get(
            f'/-/g/{read_code}/%2Fmain/{limit_field}/1/{limit_field}/2')
        assert repeated.status_code == 400, repeated.text
        assert repeated.json()['error']['code'] == 'invalid_path_argument'


@pytest.mark.asyncio
async def test_direct_read_binds_signed_header_to_path_arguments(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'direct-private')
    created = await call(app, 'content.post_create', {'parent': '/main', 'body': 'private'},
                         key=key, subject=uid)
    rid = created.resources[0].id
    await call(app, 'content.chmod', {'id': rid, 'mode': '0600'}, key=key, subject=uid,
               expected=((rid, created.data['generation']),))
    code = build_dictionary(app.registry).code_for('operation', 'discovery.get@1')
    packet = request_for('discovery.get', {'id': rid}, app.settings.service_url,
                         signer=key, subject=uid, expires_at=NOW + timedelta(seconds=90))
    headers = {'X-Msg-Request': b64(canonical(packet))}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        assert (await http.get(f'/-/g/{code}/{rid}')).status_code == 403
        result = await http.get(f'/-/g/{code}/{rid}', headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()['operation'] == 'discovery.get'
        mismatch = await http.get(f'/-/g/{code}/%2Fmain', headers=headers)
        assert mismatch.status_code == 400
        assert mismatch.json()['error']['code'] == 'representation_mismatch'
