"""Transfer authorization keeps nameless uploads and managed files distinct."""

import time
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.admin.boards import appoint_administrator
from msg.core.codec import b64, canonical, digest, wire
from msg.core.errors import Failure
from msg.core.models import AccessRequirement, ExecutionContext
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_named_board_protection_does_not_break_http_upload_to_topic(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'topic-upload')
    data = b'An ordinary board attachment.'

    def packet(operation, arguments):
        return request_for(
            operation,
            arguments,
            app.settings.service_url,
            signer=key,
            subject=subject,
            certificates=(cert,),
            expires_at=NOW + timedelta(seconds=120),
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        opened = await http.post(
            '/-/transfer',
            content=canonical(
                packet(
                    'transfer.open',
                    {'direction': 'upload', 'target': {'id': '/main'}, 'size': len(data)},
                )
            ),
        )
        assert opened.status_code == 200, opened.text
        transfer = opened.json()['data']['transfer_id']
        uploaded = await http.post(
            '/-/transfer',
            content=canonical(
                packet(
                    'transfer.part_put',
                    {
                        'transfer_id': transfer,
                        'offset': 0,
                        'data': b64(data),
                        'digest': digest(data),
                    },
                )
            ),
        )
        assert uploaded.status_code == 200, uploaded.text
        sealed = await http.post(
            '/-/transfer',
            content=canonical(
                packet(
                    'transfer.seal',
                    {
                        'transfer_id': transfer,
                        'final_size': len(data),
                        'final_digest': digest(data),
                    },
                )
            ),
        )
        assert sealed.status_code == 200, sealed.text
        async with app.metadata.transaction(write=False) as tx:
            resource = await tx.resource(sealed.json()['output']['id'])
            assert resource.type == 'file' and resource.parent == 't_main'
            assert resource.owner == subject and resource.name not in {'ABOUT.md', 'HEADER.svg'}


@pytest.mark.asyncio
async def test_internal_no_request_checks_still_protect_managed_resources(installed):
    app, root = installed
    editor_key, editor, _ = await register(app, 'board-file-manager')
    outsider_key, outsider, outsider_cert = await register(app, 'board-file-outsider')
    await appoint_administrator(app, '/wiki', '/@board-file-manager', root, operator='test')
    created = await call(
        app,
        'content.file_put',
        {
            'parent': '/wiki',
            'name': 'ABOUT.md',
            'media_type': 'text/plain',
            'data': b64(b'Board description'),
        },
        key=editor_key,
        subject=editor,
    )
    assert created.status == 'ok', wire(created)
    denied = await call(
        app,
        'content.file_put',
        {
            'parent': '/wiki',
            'name': 'HEADER.svg',
            'media_type': 'image/svg+xml',
            'data': b64(b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        },
        key=outsider_key,
        subject=outsider,
    )
    assert denied.error.code == 'topic_admin_required', wire(denied)

    packet = request_for(
        'transfer.open',
        {'direction': 'upload'},
        app.settings.service_url,
        signer=outsider_key,
        subject=outsider,
        certificates=(outsider_cert,),
        expires_at=NOW + timedelta(seconds=120),
    )
    async with app.metadata.transaction(write=False) as tx:
        principal = await app.authenticator.authenticate(packet, tx, entry='network')
        context = ExecutionContext(
            request_id=packet.request_id,
            principal=principal,
            entry='network',
            now=NOW,
            deadline_monotonic=time.monotonic() + 30,
        )
        for resource, check, expected in (
            (created.resources[0].id, 'write', 'topic_admin_required'),
            ('r_agents', 'write', 'system_managed_resource'),
            ('r_rules', 'write', 'system_managed_resource'),
        ):
            with pytest.raises(Failure, match='^' + expected + '$'):
                await app.authorizer.require(
                    context,
                    None,
                    (
                        AccessRequirement(
                            resource_id=resource, operation='transfer.open@1', check=check
                        ),
                    ),
                    tx,
                )
