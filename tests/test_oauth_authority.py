"""Derived access must retain current source authority and read-only semantics."""

import os
from dataclasses import replace

import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_browser_sessions import browser_login, signed_browser as signed_browser
from test_service import call

from msg.core.codec import b64, unb64, wire
from msg.core.models import Scope


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['browser', 'api'])
@pytest.mark.parametrize('contraction', ['operations', 'scope'])
async def test_derived_credentials_recheck_current_source_ceiling(
    signed_browser, kind, contraction
):
    app, key, subject, _http = signed_browser
    if kind == 'browser':
        cookie = await browser_login(signed_browser)
        from msg.security.sessions import BrowserSessionService

        async with app.metadata.transaction(write=False) as tx:
            _, credential_id, secret = await BrowserSessionService(app).browser_credentials(
                tx, cookie
            )
        token = (credential_id, secret)
    else:
        issued = await call(
            app,
            'identity.token_create',
            {'nonce': b64(os.urandom(32)), 'ttl': 60, 'recovery_secret': b64(os.urandom(32))},
            key=key,
            subject=subject,
            contract_version=3,
        )
        assert issued.status == 'ok', wire(issued)
        token = (issued.data['credential_id'], unb64(issued.data['token']))
    allowed = await call(app, 'discovery.get', {'id': '/main'}, subject=subject, token=token)
    assert allowed.status == 'ok', wire(allowed)
    async with app.metadata.transaction(write=True) as tx:
        parent = await tx.credential(key.key_id)
        owner = await tx.subject(subject)
        # Keep the signer, subject/auth_version, revocation and deadlines intact.
        # Only the authoritative source ceiling changes, just as in #76 vectors.
        ceiling = (
            ()
            if contraction == 'operations'
            else tuple(
                replace(grant, scope=Scope(resource_id='t_store', descendants=True))
                for grant in parent.ceiling
            )
        )
        await tx.save_credential(replace(parent, ceiling=ceiling), owner.auth_version)
    before = await business_snapshot(app)
    denied = await call(app, 'discovery.get', {'id': '/main'}, subject=subject, token=token)
    assert denied.status == 'error' and denied.error.code == 'invalid_grant', wire(denied)
    assert await business_snapshot(app) == before


@pytest.mark.asyncio
async def test_private_browser_get_preserves_read_only_authority(signed_browser, monkeypatch):
    app, _, _, http = signed_browser
    await browser_login(signed_browser)
    async with readonly_evidence(app, monkeypatch):
        response = await http.get('/@key-owner/in')
        assert response.status_code == 200, response.text
