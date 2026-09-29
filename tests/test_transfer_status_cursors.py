"""Transfer continuation errors stay bounded, authenticated and read-only."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from read_only_evidence import readonly_evidence
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.client import (
    GraphQLTransport,
    HTTPTransport,
    MCPHTTPTransport,
    PathGETTransport,
)
from msg.transports.http import create_app

INVALID_CURSORS = (
    '-1',
    str(2**63),
    'offset-sentinel',
    '',
    '0.5',
    '1e3',
    ' 0',
    '0 ',
    '+0',
    '\u0661',
    '9' * 64,
)
TRANSPORTS = (HTTPTransport, PathGETTransport, GraphQLTransport, MCPHTTPTransport)


async def prepared_transfer(app, kind):
    key, subject, cert = await register(app, 'cursor-owner')

    async def invoke(operation, arguments):
        return await call(app, operation, arguments, key=key, subject=subject, certs=(cert,))

    args = {'direction': 'upload'}
    if kind != 'unknown':
        args['size'] = 7
    opened = await invoke('transfer.open', args)
    assert opened.status == 'ok', wire(opened)
    transfer_id = opened.data['transfer_id']
    if kind in {'sealed', 'download'}:
        data = b'1234567'
        put = await invoke(
            'transfer.part_put',
            {'transfer_id': transfer_id, 'offset': 0, 'data': b64(data), 'digest': digest(data)},
        )
        assert put.status == 'ok', wire(put)
        sealed = await invoke(
            'transfer.seal',
            {'transfer_id': transfer_id, 'final_size': len(data), 'final_digest': digest(data)},
        )
        assert sealed.status == 'ok', wire(sealed)
        if kind == 'download':
            opened = await invoke(
                'transfer.open', {'direction': 'download', 'target': wire(sealed.resources[0])}
            )
            assert opened.status == 'ok', wire(opened)
            transfer_id = opened.data['transfer_id']
    elif kind == 'cancelled':
        cancelled = await invoke('transfer.cancel', {'transfer_id': transfer_id})
        assert cancelled.status == 'ok', wire(cancelled)
    return key, subject, cert, transfer_id, invoke


def status_packet(app, key, subject, cert, arguments):
    return request_for(
        'transfer.status',
        arguments,
        app.settings.service_url,
        signer=key,
        subject=subject,
        certificates=(cert,),
        expires_at=NOW + timedelta(seconds=120),
    )


@pytest.mark.parametrize('kind', ['known', 'unknown', 'sealed', 'download', 'cancelled'])
async def test_status_rejects_invalid_cursors_before_any_numeric_backend_use(
    installed, monkeypatch, kind
):
    app, _ = installed
    _, _, _, tid, invoke = await prepared_transfer(app, kind)
    async with readonly_evidence(app, monkeypatch):
        for cursor in INVALID_CURSORS:
            result = await invoke('transfer.status', {'transfer_id': tid, 'cursor': cursor})
            assert result.status == 'error', (kind, cursor, wire(result))
            assert result.error.code == 'invalid_cursor', (kind, cursor, wire(result))
            assert 'offset-sentinel' not in canonical(result).decode()


@pytest.mark.parametrize('transport_class', TRANSPORTS)
async def test_status_cursor_errors_match_real_protocol_adapters(
    installed, monkeypatch, transport_class
):
    app, _ = installed
    key, subject, cert, tid, _ = await prepared_transfer(app, 'unknown')
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        transport = transport_class(app.settings.service_url, http=http)
        async with readonly_evidence(app, monkeypatch):
            initial = await transport.call(
                status_packet(app, key, subject, cert, {'transfer_id': tid})
            )
            assert initial.status == 'ok', wire(initial)
            for cursor in INVALID_CURSORS:
                result = await transport.call(
                    status_packet(app, key, subject, cert, {'transfer_id': tid, 'cursor': cursor})
                )
                assert result.status == 'error', (cursor, wire(result))
                assert result.error.code == 'invalid_cursor', (cursor, wire(result))


@pytest.mark.parametrize('known_size', [True, False])
async def test_status_pagination_preserves_all_ranges_across_protocols(
    installed, monkeypatch, known_size
):
    app, _ = installed
    key, subject, cert, tid, invoke = await prepared_transfer(
        app, 'known' if known_size else 'unknown'
    )
    for offset in (5, 1, 3):
        data = bytes([offset])
        put = await invoke(
            'transfer.part_put',
            {'transfer_id': tid, 'offset': offset, 'data': b64(data), 'digest': digest(data)},
        )
        assert put.status == 'ok', wire(put)
    expected = [(0, 1), (2, 3), (4, 5), (6, 7)] if known_size else [(1, 2), (3, 4), (5, 6)]
    field = 'missing' if known_size else 'received'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            for transport_class in TRANSPORTS:
                transport = transport_class(app.settings.service_url, http=http)
                args = {'transfer_id': tid, 'limit': 1}
                seen, cursors = [], set()
                for index in range(len(expected)):
                    result = await transport.call(status_packet(app, key, subject, cert, args))
                    assert result.status == 'ok', wire(result)
                    page = [tuple(item) for item in result.data[field]]
                    assert page == [expected[index]]
                    seen.extend(page)
                    # Compact wire responses omit null values on the last page.
                    cursor = result.data.get('next_cursor')
                    expected_cursor = (
                        str(expected[index + 1][0]) if index + 1 < len(expected) else None
                    )
                    assert cursor == expected_cursor
                    if cursor is None:
                        break
                    assert cursor not in cursors
                    cursors.add(cursor)
                    args = {**args, 'cursor': cursor}
                assert seen == expected, (transport_class.name, seen)
                assert cursor is None
            # Preserve the largest supported byte offset and leading-zero
            # numeric spelling without allowing larger-than-int64 values.
            end = '7' if known_size else str(2**63 - 1)
            result = await invoke('transfer.status', {'transfer_id': tid, 'cursor': end})
            assert result.status == 'ok' and not result.data[field], wire(result)
            zero = await invoke('transfer.status', {'transfer_id': tid, 'cursor': '000'})
            assert zero.status == 'ok', wire(zero)
            assert [tuple(item) for item in zero.data[field]] == expected


@pytest.mark.parametrize('kind', ['known', 'sealed', 'download'])
async def test_fixed_status_endpoint_rejects_cursor_beyond_known_size(installed, monkeypatch, kind):
    app, _ = installed
    key, subject, cert, tid, _ = await prepared_transfer(app, kind)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        async with readonly_evidence(app, monkeypatch):
            for cursor in ('0', '7'):
                response = await http.post(
                    '/-/transfer',
                    content=canonical(
                        status_packet(
                            app, key, subject, cert, {'transfer_id': tid, 'cursor': cursor}
                        )
                    ),
                )
                assert response.status_code == 200, response.text
                assert response.json()['status'] == 'ok', response.text
            rejected = await http.post(
                '/-/transfer',
                content=canonical(
                    status_packet(app, key, subject, cert, {'transfer_id': tid, 'cursor': '8'})
                ),
            )
            assert rejected.status_code == 400, rejected.text
            assert rejected.json()['error']['code'] == 'invalid_cursor', rejected.text


async def test_status_cursor_validation_does_not_replace_current_identity_checks(
    installed, monkeypatch
):
    app, _ = installed
    key, subject, cert, tid, invoke = await prepared_transfer(app, 'unknown')
    other, other_subject, other_cert = await register(app, 'cursor-other')
    args = {'transfer_id': tid, 'cursor': 'offset-sentinel'}
    async with readonly_evidence(app, monkeypatch):
        denied = await call(
            app, 'transfer.status', args, key=other, subject=other_subject, certs=(other_cert,)
        )
        assert denied.error.code == 'transfer_owner_required', wire(denied)
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        tx.execute(
            'UPDATE credentials SET body=? WHERE id=?',
            (canonical(replace(credential, revoked_at=NOW)).decode(), credential.id),
            write=True,
        )
    async with readonly_evidence(app, monkeypatch):
        for cursor in ('0', 'offset-sentinel'):
            revoked = await invoke('transfer.status', {'transfer_id': tid, 'cursor': cursor})
            assert revoked.error.code == 'credential_revoked', wire(revoked)
