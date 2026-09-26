"""The fixed transfer endpoint delegates to registered transfer operations."""
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, register

from msg.core.codec import b64, canonical, digest, unb64
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_fixed_transfer_endpoint_uses_executor_for_resume_and_ranges(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'fixed-transfer')
    counter = 0

    def packet(name, arguments, *, request_id=None):
        nonlocal counter
        counter += 1
        return request_for(name, arguments, app.settings.service_url,
                           signer=key, subject=uid, certificates=(cert,),
                           request_id=request_id or f'fixed-transfer-{counter}',
                           expires_at=NOW + timedelta(seconds=120))

    body = b'fixed transfer\x00bytes'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        description = await http.get('/-/transfer')
        assert description.status_code == 200, description.text
        assert set(description.json()['operations']) == {
            'transfer.open', 'transfer.part_put', 'transfer.part_get',
            'transfer.status', 'transfer.seal', 'transfer.cancel'}
        head = await http.head('/-/transfer')
        assert head.status_code == 200 and head.content == b''
        assert (await http.options('/-/transfer')).status_code == 405

        opened_packet = packet('transfer.open', {
            'direction': 'upload', 'size': len(body), 'digest': digest(body),
            'requested_part_bytes': len(body)})
        ordinary = await http.post('/tmp', content=canonical(opened_packet))
        assert ordinary.status_code == 405
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM transfers')[0] == 0
        opened = await http.post('/-/transfer', content=canonical(opened_packet))
        assert opened.status_code == 200, opened.text
        tid = opened.json()['data']['transfer_id']
        replay = await http.post('/-/transfer', content=canonical(opened_packet))
        assert replay.status_code == 200 and replay.json()['replayed'], replay.text
        assert replay.json()['data']['transfer_id'] == tid

        put = packet('transfer.part_put', {
            'transfer_id': tid, 'offset': 0, 'data': b64(body), 'digest': digest(body)})
        accepted = await http.post('/-/transfer', content=canonical(put))
        assert accepted.status_code == 200, accepted.text
        assert (await http.post('/-/transfer', content=canonical(put))).json()['replayed'] is True
        status = await http.post('/-/transfer', content=canonical(
            packet('transfer.status', {'transfer_id': tid})))
        assert status.status_code == 200 and status.json()['data']['missing'] == [], status.text
        sealed = await http.post('/-/transfer', content=canonical(packet(
            'transfer.seal', {'transfer_id': tid, 'final_size': len(body),
                              'final_digest': digest(body)})))
        assert sealed.status_code == 200, sealed.text
        output = sealed.json()['output']
        assert output['id']

        download = await http.post('/-/transfer', content=canonical(packet(
            'transfer.open', {'direction': 'download', 'target': output,
                              'requested_part_bytes': len(body)})))
        assert download.status_code == 200, download.text
        fetched = await http.post('/-/transfer', content=canonical(packet(
            'transfer.part_get', {'transfer_id': download.json()['data']['transfer_id'],
                                  'offset': 0, 'length': len(body)})))
        assert fetched.status_code == 200, fetched.text
        assert unb64(fetched.json()['data']['data']) == body


@pytest.mark.asyncio
async def test_fixed_transfer_endpoint_rejects_non_transfer_and_unsigned(installed):
    app, _ = installed
    key, uid, _ = await register(app, 'transfer-boundary')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        non_transfer = request_for('content.post_create', {'parent': '/main', 'body': 'bad'},
                                   app.settings.service_url, signer=key, subject=uid,
                                   expires_at=NOW + timedelta(seconds=120))
        rejected = await http.post('/-/transfer', content=canonical(non_transfer))
        assert rejected.status_code == 400
        assert rejected.json()['error']['code'] == 'operation_mismatch'
        unsigned = request_for('transfer.open', {'direction': 'upload'},
                               app.settings.service_url, expires_at=NOW + timedelta(seconds=120))
        unauthorized = await http.post('/-/transfer', content=canonical(unsigned))
        assert unauthorized.status_code == 401
        assert unauthorized.json()['error']['code'] == 'authentication_required'
        assert (await http.get('/-/transfer?operation=transfer.open')).status_code == 400
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM transfers')[0] == 0
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0
