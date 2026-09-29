"""Exercise private conversations using fresh clients in the isolated Test Root."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from msg.client import ClientState, MsgClient
from msg.core.errors import require
from msg.core.requests import request_for
from msg.transports.client import HTTPTransport
from msg.transports.http import create_app


async def check_private_dm(app, now):
    """Never register identities or create messages on the live installation."""
    require(app.selftest_run_id is not None, 'isolated_selftest_required')
    with TemporaryDirectory(prefix='msg-dm-selftest-') as directory:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app))) as http:
            clients = []
            for name in ('sender', 'recipient', 'outsider'):
                client = MsgClient(
                    ClientState(Path(directory) / name, server=app.settings.service_url),
                    HTTPTransport(app.settings.service_url, http=http),
                    clock=lambda: now,
                    retries=0,
                )
                client.checked(await client.register('dm-' + uuid4().hex[:16]))
                clients.append(client)
            sender, recipient, outsider = clients
            requested = sender.checked(
                await sender.call(
                    'communication.dm_request', {'recipient': recipient.state.subject}
                )
            )
            conversation = requested.data['conversation_id']
            if requested.data['state'] != 'pending':
                return False
            early = await sender.call(
                'communication.dm_send', {'conversation_id': conversation, 'body': 'not accepted'}
            )
            if early.status != 'error' or early.error.code != 'dm_controlled_resource':
                return False
            accepted = recipient.checked(
                await recipient.call('communication.dm_accept', {'conversation_id': conversation})
            )
            if accepted.data['state'] != 'active':
                return False
            body = 'isolated private conversation ' + uuid4().hex
            sent = sender.checked(
                await sender.call(
                    'communication.dm_send', {'conversation_id': conversation, 'body': body}
                )
            )
            post = sent.resources[0].id
            for participant in (sender, recipient):
                read = await participant.call('discovery.get', {'id': post})
                if read.status != 'ok' or read.data.get('content') != body:
                    return False
            for operation, arguments in (
                ('discovery.get', {'id': conversation}),
                ('discovery.get', {'id': post}),
                ('discovery.list', {'parent': conversation}),
                ('communication.dm_send', {'conversation_id': conversation, 'body': 'intrusion'}),
            ):
                denied = await outsider.call(operation, arguments)
                expected = (
                    'dm_not_found' if operation == 'communication.dm_send' else 'permission_denied'
                )
                if denied.status != 'error' or denied.error.code != expected:
                    return False
            hidden = await outsider.call('discovery.search', {'query': body})
            if hidden.status != 'ok' or hidden.data.get('items'):
                return False
            anonymous = await HTTPTransport(app.settings.service_url, http=http).call(
                request_for('discovery.get', {'id': post}, app.settings.service_url)
            )
            return anonymous.status == 'error' and anonymous.error.code == 'permission_denied'
