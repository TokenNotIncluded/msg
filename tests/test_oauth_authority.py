"""Derived access must retain current source authority and read-only semantics."""

import os
from dataclasses import replace

import pytest
from read_only_evidence import business_snapshot, readonly_evidence
from test_oauth import device_tokens, oauth as oauth
from test_service import call

from msg.core.codec import b64, unb64, wire
from msg.core.models import Scope


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['oauth', 'api'])
@pytest.mark.parametrize('contraction', ['operations', 'scope'])
async def test_derived_credentials_recheck_current_source_ceiling(oauth, kind, contraction):
    app, key, subject, _http = oauth
    if kind == 'oauth':
        issued = await device_tokens(oauth)
        credential_id, _, value = issued['access_token'].partition('.')
        token = (credential_id, unb64(value))
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
@pytest.mark.parametrize('method', ['GET', 'POST'])
async def test_userinfo_is_read_only_including_authoritative_oauth_state(oauth, monkeypatch, method):
    app, _key, subject, http = oauth
    issued = await device_tokens(oauth)
    async with readonly_evidence(app, monkeypatch):
        response = await http.request(
            method,
            '/oauth/userinfo',
            headers={'Authorization': 'Bearer ' + issued['access_token']},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {'sub': subject, 'preferred_username': 'oauth-owner'}
