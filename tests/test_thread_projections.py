"""Thread pages use the existing signed field selector without changing full reads."""

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.client import ClientState, MsgClient
from msg.core.codec import canonical, decode, loads, unb64, wire
from msg.core.models import HandlerOutput, OperationRequest, ResourcePageOutput
from msg.core.requests import request_for
from msg.plugins.discovery import next_link
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app

FIELDS = ('id', 'revision', 'content', 'relations', 'stable_path', 'revision_path')


def test_page_marker_has_exactly_the_existing_handler_wire_shape():
    assert canonical(ResourcePageOutput(data={'root': 'r_root'})) == canonical(
        HandlerOutput(data={'root': 'r_root'})
    )


async def post(app, key, subject, *, target=None, body='root'):
    result = await call(
        app,
        'discussion.reply' if target else 'content.post_create',
        {'target': wire(target), 'body': body} if target else {'parent': '/main', 'body': body},
        key=key,
        subject=subject,
    )
    assert result.status == 'ok', wire(result)
    return result


async def project(app, arguments, fields=FIELDS):
    return await app.executor.execute(
        request_for(
            'discussion.thread',
            arguments,
            app.settings.service_url,
            return_fields=fields,
            expires_at=NOW + timedelta(seconds=120),
        )
    )


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('events', 'jobs', 'revisions', 'results')
        )


@pytest.mark.asyncio
async def test_signed_client_projection_keeps_default_reads_and_request_bytes(installed, tmp_path):
    app, _ = installed
    key, subject, cert = await register(app, 'thread-selector')
    root = await post(app, key, subject)
    reply = await post(app, key, subject, target=root.resources[0], body='reply')
    rid = root.resources[0].id
    full = await call(app, 'discussion.thread', {'id': rid}, key=key, subject=subject)
    assert full.status == 'ok' and not full.resources, wire(full)
    before = await business_state(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        transport = HTTPTransport(app.settings.service_url, http=http)
        client = MsgClient(
            ClientState(tmp_path / 'selector-client', server=app.settings.service_url),
            transport,
            clock=app.clock,
        )
        packet = client.prepare(
            'discussion.thread',
            {'id': rid},
            signer=key,
            subject=subject,
            certificates=(cert,),
            return_fields=FIELDS,
        )
        await transport.description()
        before_calls = transport.calls
        signed_bytes = canonical(packet)
        result = client.checked(await client.send(packet))
        assert transport.calls - before_calls == 1 and canonical(packet) == signed_bytes
        assert result.resources == (root.resources[0], reply.resources[0])
        assert result.data['root'] == rid and 'items' not in result.data
        assert wire(result.data['projection']) == [
            {field: item[field] for field in FIELDS} for item in wire(full.data['items'])
        ]
        changed = await client.send(replace(packet, return_fields=('id',)))
        assert changed.error.code == 'payload_digest_mismatch', wire(changed)
    repeated = await call(app, 'discussion.thread', {'id': rid}, key=key, subject=subject)
    assert wire(repeated.data) == wire(full.data)
    for item in full.data['items']:
        assert item['links']['self']['address']['path'] == item['stable_path']
        assert item['links']['v']['address']['path'] == item['revision_path']
        assert item['created_at'] and item['modified_at'] and item['name']
    assert await business_state(app) == before


@pytest.mark.asyncio
async def test_projection_pagination_focus_ancestors_and_current_acl(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'thread-page-owner')
    root = await post(app, key, subject)
    reply = await post(app, key, subject, target=root.resources[0], body='private next')
    rid = root.resources[0].id
    first = await project(app, {'id': rid, 'limit': 1})
    assert first.status == 'ok', wire(first)
    assert first.resources == root.resources and first.data['cursor']
    next_packet = decode(OperationRequest, loads(unb64(first.data['next'].rsplit('/', 1)[1])))
    assert next_packet.return_fields == FIELDS
    assert next_packet.arguments['cursor'] == first.data['cursor']
    full_next = next_link(app, 'discussion.thread', wire(next_packet.arguments))
    assert full_next != first.data['next']
    next_default = decode(OperationRequest, loads(unb64(full_next.rsplit('/', 1)[1])))
    assert next_default.return_fields == () and next_default.request_id != next_packet.request_id
    # Selectors choose representation, not membership or authority; a cursor
    # can be reused by the full representation and both still recheck each item.
    full_page = await call(app, 'discussion.thread', wire(next_packet.arguments))
    compact_page = await project(app, wire(next_packet.arguments))
    assert [item['id'] for item in full_page.data['items']] == [reply.resources[0].id]
    assert compact_page.resources == reply.resources
    focus = await project(app, {'id': reply.resources[0].id})
    assert focus.status == 'ok' and wire(focus.data['ancestors']) == wire(root.resources), wire(
        focus
    )
    unknown = await project(app, {'id': rid}, ('unknown_field',))
    assert unknown.error.code == 'unknown_projection_field', wire(unknown)
    locked = await call(
        app,
        'content.chmod',
        {'id': reply.resources[0].id, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((reply.resources[0].id, reply.data['generation']),),
    )
    assert locked.status == 'ok', wire(locked)
    before = await business_state(app)
    hidden_page = await project(app, wire(next_packet.arguments))
    assert hidden_page.status == 'ok' and not hidden_page.resources, wire(hidden_page)
    assert hidden_page.data['root'] == rid and hidden_page.data['projection'] == ()
    assert 'items' not in hidden_page.data and 'private next' not in canonical(hidden_page).decode()
    empty_unknown = await project(app, wire(next_packet.arguments), ('unknown_field',))
    assert empty_unknown.error.code == 'unknown_projection_field', wire(empty_unknown)
    denied_focus = await project(app, {'id': reply.resources[0].id})
    assert denied_focus.error.code == 'permission_denied', wire(denied_focus)
    assert await business_state(app) == before
