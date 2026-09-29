"""/@user/receipts projects the caller's persisted OperationResults read-only."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.http import create_app

PROJECTION = {'request_id', 'operation', 'status', 'committed_at', 'resources', 'signed'}


def _headers(app, operation, args, key, subject):
    packet = request_for(
        operation,
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    return {'X-Msg-Request': b64(canonical(packet))}


def _state(tx):
    counts = tuple(
        tx.one(f'SELECT COUNT(*) FROM {table}')[0]
        for table in ('results', 'audit', 'jobs', 'events', 'messages', 'revisions')
    )
    results = digest([
        list(row)
        for row in tx.rows(
            'SELECT subject,request_id,digest,body FROM results ORDER BY subject,request_id'
        )
    ])
    generations = tx.one('SELECT SUM(generation) FROM resources')[0]
    return counts, results, generations


@pytest.mark.asyncio
async def test_subject_receipts_are_owner_only_read_only_projection(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'receipt-alice')
    bob_key, bob, _ = await register(app, 'receipt-bob')
    written = []
    for index in range(3):
        post = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'receipt {index}'},
            key=alice_key,
            subject=alice,
        )
        assert post.status == 'ok', wire(post)
        written.append(post)
    bob_post = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'bob receipt'},
        key=bob_key,
        subject=bob,
    )
    assert bob_post.status == 'ok', wire(bob_post)
    async with app.metadata.transaction(write=False) as tx:
        expected = [
            row[0]
            for row in tx.rows(
                'SELECT request_id FROM results WHERE subject=? ORDER BY request_id', (alice,)
            )
        ]
        before = _state(tx)
    assert {post.request_id for post in written} <= set(expected) and len(expected) >= 4
    assert bob_post.request_id not in expected

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        list_headers = _headers(app, 'communication.receipt_list', {}, alice_key, alice)
        listed = await http.get('/@receipt-alice/receipts', headers=list_headers)
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body['path'] == '/@receipt-alice/receipts'
        assert [item['request_id'] for item in body['items']] == expected
        assert 'cursor' not in body
        for item in body['items']:
            assert set(item) == PROJECTION
            assert item['signed'] is True and item['status'] == 'ok'
        slashed = await http.get('/@receipt-alice/receipts/', headers=list_headers)
        suffixed = await http.get('/@receipt-alice/receipts/json', headers=list_headers)
        assert slashed.content == suffixed.content == listed.content
        cached = await http.get(
            '/@receipt-alice/receipts',
            headers={**list_headers, 'If-None-Match': listed.headers['etag']},
        )
        assert cached.status_code == 304

        target = written[1]
        item_headers = _headers(
            app, 'communication.receipt_get', {'request_id': target.request_id}, alice_key, alice
        )
        single = await http.get(
            f'/@receipt-alice/receipts/{target.request_id}', headers=item_headers
        )
        assert single.status_code == 200, single.text
        receipt = single.json()['receipt']
        assert single.json()['path'] == f'/@receipt-alice/receipts/{target.request_id}'
        assert receipt == next(
            item for item in body['items'] if item['request_id'] == target.request_id
        )
        assert receipt['operation'] == 'content.post_create'
        assert receipt['resources'] == [wire(ref) for ref in target.resources]
        assert receipt['committed_at'] == wire(target.committed_at)
        for secret in ('data', 'output', 'receipt', 'cli_url', 'prefer_cli', 'actor', 'subject'):
            assert secret not in receipt
        suffixed = await http.get(
            f'/@receipt-alice/receipts/{target.request_id}/json', headers=item_headers
        )
        assert suffixed.content == single.content
        head = await http.head(
            f'/@receipt-alice/receipts/{target.request_id}', headers=item_headers
        )
        assert head.status_code == 200 and head.content == b''
        assert head.headers['etag'] == single.headers['etag']

        missing_headers = _headers(
            app, 'communication.receipt_get', {'request_id': 'r_missing_receipt'}, alice_key, alice
        )
        missing = await http.get(
            '/@receipt-alice/receipts/r_missing_receipt', headers=missing_headers
        )
        assert missing.status_code == 404 and missing.json()['error']['code'] == 'not_found'

        bob_list = _headers(app, 'communication.receipt_list', {}, bob_key, bob)
        cross = await http.get('/@receipt-alice/receipts', headers=bob_list)
        assert cross.status_code == 403 and cross.json()['error']['code'] == 'permission_denied'
        assert target.request_id not in cross.text
        for request_id in (target.request_id, bob_post.request_id, 'r_missing_receipt'):
            bob_get = _headers(
                app, 'communication.receipt_get', {'request_id': request_id}, bob_key, bob
            )
            denied = await http.get(f'/@receipt-alice/receipts/{request_id}', headers=bob_get)
            assert denied.status_code == 403, denied.text
            assert denied.json()['error']['code'] == 'permission_denied'
        own = await http.get('/@receipt-bob/receipts', headers=bob_list)
        assert own.status_code == 200
        assert bob_post.request_id in [item['request_id'] for item in own.json()['items']]
        assert not set(expected) & {item['request_id'] for item in own.json()['items']}

        for path in ('/@receipt-alice/receipts', f'/@receipt-alice/receipts/{target.request_id}'):
            anonymous = await http.get(path)
            assert anonymous.status_code == 401
            assert anonymous.json()['error']['code'] == 'authentication_required'
            assert 'items' not in anonymous.json() and 'receipt' not in anonymous.json()

        unknown = await http.get('/@receipt-alice/receipts?after=x', headers=list_headers)
        assert unknown.status_code == 400
        assert unknown.json()['error']['code'] == 'unknown_query_parameter'
        detail_query = await http.get(
            f'/@receipt-alice/receipts/{target.request_id}?limit=1', headers=item_headers
        )
        assert detail_query.json()['error']['code'] == 'unknown_query_parameter'
        for bad in (0, 201):
            bad_headers = _headers(
                app, 'communication.receipt_list', {'limit': bad}, alice_key, alice
            )
            rejected = await http.get(f'/@receipt-alice/receipts?limit={bad}', headers=bad_headers)
            assert rejected.status_code == 400, rejected.text
        for path in ('/@receipt-alice/receipts', f'/@receipt-alice/receipts/{target.request_id}'):
            written_path = await http.post(path, json={})
            assert written_path.status_code in {404, 405}

        pages = []
        cursor = None
        while True:
            args = {'limit': 1} if cursor is None else {'limit': 1, 'cursor': cursor}
            page = await http.get(
                '/@receipt-alice/receipts',
                params=args,
                headers=_headers(app, 'communication.receipt_list', args, alice_key, alice),
            )
            assert page.status_code == 200, page.text
            data = page.json()
            assert len(data['items']) == 1
            pages.append(data['items'][0]['request_id'])
            cursor = data.get('cursor')
            if cursor is None:
                break
            assert data['next_requires_auth'] is True
            assert data['next'].startswith('/-/g/communication.receipt_list/j/')
            stolen_args = {'limit': 1, 'cursor': cursor}
            stolen = await http.get(
                '/@receipt-bob/receipts',
                params=stolen_args,
                headers=_headers(app, 'communication.receipt_list', stolen_args, bob_key, bob),
            )
            assert stolen.status_code == 400
            assert stolen.json()['error']['code'] == 'cursor_query_mismatch'
        assert pages == expected

    async with app.metadata.transaction(write=False) as tx:
        assert _state(tx) == before


@pytest.mark.asyncio
async def test_receipt_projection_hides_resources_no_longer_readable(installed):
    app, _ = installed
    alice_key, alice, _ = await register(app, 'receipt-share-alice')
    bob_key, bob, _ = await register(app, 'receipt-share-bob')
    note = await call(
        app,
        'identity.note_put',
        {'name': 'receipt-note', 'body': 'Private'},
        key=alice_key,
        subject=alice,
    )
    assert note.status == 'ok', wire(note)
    rid = note.resources[0].id
    grant = await call(
        app,
        'sharing.grant',
        {'resource': rid, 'grantee': bob, 'expires_at': wire(NOW + timedelta(days=1))},
        key=alice_key,
        subject=alice,
    )
    assert grant.status == 'ok', wire(grant)
    sent = await call(
        app,
        'communication.send',
        {'recipient': alice, 'resource': wire(note.resources[0])},
        key=bob_key,
        subject=bob,
    )
    assert sent.status == 'ok', wire(sent)
    assert rid in {ref.id for ref in sent.resources}
    shown = await call(
        app, 'communication.receipt_get', {'request_id': sent.request_id}, key=bob_key, subject=bob
    )
    assert shown.status == 'ok', wire(shown)
    assert rid in {ref['id'] for ref in shown.data['receipt']['resources']}
    revoked = await call(
        app, 'sharing.revoke', {'grant_id': grant.data['grant']['id']}, key=alice_key, subject=alice
    )
    assert revoked.status == 'ok', wire(revoked)
    async with app.metadata.transaction(write=False) as tx:
        stored = tx.one(
            'SELECT body FROM results WHERE subject=? AND request_id=?', (bob, sent.request_id)
        )[0]
    hidden = await call(
        app, 'communication.receipt_get', {'request_id': sent.request_id}, key=bob_key, subject=bob
    )
    assert hidden.status == 'ok', wire(hidden)
    assert rid not in {ref['id'] for ref in hidden.data['receipt']['resources']}
    assert hidden.data['receipt']['signed'] is True
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tx.one(
                'SELECT body FROM results WHERE subject=? AND request_id=?', (bob, sent.request_id)
            )[0]
            == stored
        )
