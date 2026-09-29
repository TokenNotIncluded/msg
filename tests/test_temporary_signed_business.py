"""Signed temporary subjects and their bootstrap token have distinct limits."""

import pytest
from test_service import call, temporary_v3_args

from msg.core.codec import unb64, wire


@pytest.mark.asyncio
async def test_temporary_signing_key_can_create_low_risk_content(installed):
    app, _ = installed
    args, request_id, signer, _ = temporary_v3_args()
    created = await call(app, 'identity.temporary', args, rid=request_id, contract_version=3)
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']

    post = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'signed temporary post'},
        key=signer,
        subject=subject,
    )
    assert post.status == 'ok', f'{post.error.code}: {wire(post)}'


@pytest.mark.asyncio
async def test_temporary_token_allows_low_risk_write_but_not_permission_changes(installed):
    app, _ = installed
    args, request_id, _, _ = temporary_v3_args()
    created = await call(app, 'identity.temporary', args, rid=request_id, contract_version=3)
    assert created.status == 'ok', wire(created)
    subject = created.data['subject_id']
    token = (created.data['credential_id'], unb64(created.data['token']))

    post = await call(
        app,
        'content.post_create',
        {'parent': '/tmp', 'body': 'token temporary post'},
        subject=subject,
        token=token,
    )
    assert post.status == 'ok', wire(post)

    chmod = await call(
        app,
        'content.chmod',
        {'id': post.resources[0].id, 'mode': '0777'},
        subject=subject,
        token=token,
        expected=((post.resources[0].id, post.data['generation']),),
    )
    assert chmod.status == 'error'
    assert chmod.error.code == 'credential_ceiling', wire(chmod)
