"""Real signed Transfer operations seal read plans; GET never seals or grants access."""

from dataclasses import replace

import httpx

from msg.core.codec import b64, canonical, digest, wire
from msg.transports.http import create_app

from .test_read_query_tree import query, tree
from .test_read_tree_transports import business_state


async def seal_query(h, args):
    await h.create('r_query_deposits', mode=0o700)
    payload = canonical({'version': 1, 'kind': 'read', 'arguments': args})
    opened = await h.execute(
        'transfer.open',
        {
            'direction': 'upload',
            'target': {'id': 'r_query_deposits'},
            'size': len(payload),
            'digest': digest(payload),
            'media_type': 'application/vnd.msg.read-query+json',
        },
    )
    assert opened.error is None, wire(opened)
    tid = opened.data['transfer_id']
    width = min(opened.data['part_bytes'], max(1, len(payload) // 3))
    for offset in reversed(range(0, len(payload), width)):
        data = payload[offset : offset + width]
        put = await h.execute(
            'transfer.part_put',
            {'transfer_id': tid, 'offset': offset, 'data': b64(data), 'digest': digest(data)},
        )
        assert put.error is None, wire(put)
    sealed = await h.execute(
        'transfer.seal',
        {'transfer_id': tid, 'final_size': len(payload), 'final_digest': digest(payload)},
    )
    assert sealed.error is None, wire(sealed)
    issued = await h.execute('transfer.query_seal', {'transfer_id': tid})
    assert issued.error is None, wire(issued)
    return issued.data['query_ref'], sealed.resources[0]


async def test_query_ref_compacts_v3_root_but_keeps_each_nested_continuation(harness):
    h = harness
    await tree(h)
    ref, source = await seal_query(h, query())
    before = await business_state(h.app)
    initial = await h.execute('transfer.query_get', {'query_ref': ref})
    assert initial.error is None, wire(initial)
    data = initial.data
    assert data['items'][0]['collections']['children']['pageInfo']['hasNextPage']
    assert data['pageInfo']['endCursor'] == data['cursor']
    assert data['next'] == '/_r/q/' + ref + '/c/' + data['cursor']
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(h.app)), base_url=h.app.settings.service_url
    ) as http:
        packet = h.packet('transfer.query_get', {'query_ref': ref})
        response = await http.get('/_r/q/' + ref + '/p/' + b64(canonical(packet)))
        assert response.status_code == 200, response.text
        assert response.json() == wire(data)
        continuation = h.packet('transfer.query_get', {'query_ref': ref, 'cursor': data['cursor']})
        final = await http.get(
            data['next'], headers={'X-Msg-Request': b64(canonical(continuation))}
        )
        assert final.status_code == 200, final.text
        final = final.json()
        assert final['items'][0]['id'] == 'r_b'
        assert final['pageInfo']['hasNextPage'] is False
        assert final['pageInfo']['endCursor'] == final['cursor']
        assert 'next' not in final  # Final endCursor does NOT imply another page.
        nested = data['items'][0]['collections']['children']
        nested_packet = h.packet(
            'discovery.read_query', {'cursor': nested['pageInfo']['endCursor']}, 3
        )
        nested_response = await http.get(
            nested['next'], headers={'X-Msg-Request': b64(canonical(nested_packet))}
        )
        assert nested_response.status_code == 200, nested_response.text
        assert nested_response.json()['items'][0]['id'] == 'r_ab'
        assert await business_state(h.app) == before
        denied = await http.get('/_r/q/' + ref)
        assert denied.status_code == 403, denied.text
        assert await business_state(h.app) == before
    async with h.app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(source.id)
        await tx.replace(
            replace(resource, mode=0, generation=resource.generation + 1), resource.generation
        )
    after = await business_state(h.app)
    denied = await h.execute('transfer.query_get', {'query_ref': ref, 'cursor': data['cursor']})
    assert denied.error.code == 'permission_denied'
    assert await business_state(h.app) == after


async def test_long_query_ref_v3_does_not_echo_query_in_continuation(harness):
    h = harness
    await h.create('r_long')
    term = 'needle' + ('x' * 9000)
    for index in range(3):
        await h.create('r_doc_' + str(index), 'r_long', type='file', body=term)
    args = {
        'parent': 'r_long',
        'query': term,
        'query_version': 3,
        'limit': 2,
        'fields': ['id'],
        'expand': {},
    }
    ref, _ = await seal_query(h, args)
    before = await business_state(h.app)
    first = await h.execute('transfer.query_get', {'query_ref': ref})
    assert first.error is None, wire(first)
    assert len(first.data['next'].encode()) < 8192
    assert term not in first.data['next'] and term not in first.data['cursor']
    final = await h.execute(
        'transfer.query_get', {'query_ref': ref, 'cursor': first.data['cursor']}
    )
    assert final.error is None, wire(final)
    assert len(final.data['items']) == 1 and 'next' not in final.data
    assert final.data['pageInfo']['hasNextPage'] is False
    assert await business_state(h.app) == before


async def test_query_ref_cannot_upgrade_a_credential_ceiling(harness):
    h = harness
    await tree(h)
    ref, _ = await seal_query(h, query())
    async with h.app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(h.signer.key_id)
        restricted = tuple(
            replace(grant, operations=grant.operations - {'discovery.read_query@3'})
            for grant in credential.ceiling
        )
        tx.execute(
            'UPDATE credentials SET body=? WHERE id=?',
            (canonical(replace(credential, ceiling=restricted)).decode(), credential.id),
            write=True,
        )
    before = await business_state(h.app)
    result = await h.execute('transfer.query_get', {'query_ref': ref})
    assert result.error.code == 'credential_ceiling', wire(result)
    assert await business_state(h.app) == before
