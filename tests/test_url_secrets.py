"""No reusable authentication or recovery material may enter a GET URL."""

import gzip
import os
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, register

from msg.core.codec import b64, canonical, wire
from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.transports.client import PathGETTransport
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


def _path(packet, *, zipped=False):
    raw = canonical(packet)
    return (
        '/-/g/'
        + packet.operation
        + ('/gz/' if zipped else '/j/')
        + b64(gzip.compress(raw, mtime=0) if zipped else raw)
    )


@pytest.mark.asyncio
async def test_full_get_packet_rejects_token_proof_and_recovery_claim_without_state_change(
    installed, caplog
):
    app, _ = installed
    key, subject, _ = await register(app, 'url-secret-subject')
    token = os.urandom(32)
    recover = b64(os.urandom(32))
    packets = [
        request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'must not post'},
            app.settings.service_url,
            subject=subject,
            token=('fake-credential', token),
            request_id='secret-token-get',
            expires_at=NOW + timedelta(seconds=90),
        ),
        request_for(
            'identity.token_recover',
            {
                'credential_id': 'fake-credential',
                'recovery_secret': recover,
                'new_recovery_secret': b64(os.urandom(32)),
                'nonce': b64(os.urandom(32)),
            },
            app.settings.service_url,
            signer=key,
            subject=subject,
            request_id='secret-recovery-get',
            expires_at=NOW + timedelta(seconds=90),
        ),
        request_for(
            'identity.temporary',
            {'nonce': b64(os.urandom(32))},
            app.settings.service_url,
            request_id='secret-bootstrap-get',
            expires_at=NOW + timedelta(seconds=90),
        ),
    ]
    async with app.metadata.transaction(write=False) as tx:
        before = (
            tx.one('SELECT COUNT(*) FROM resources')[0],
            tx.one('SELECT COUNT(*) FROM credentials')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for packet in packets:
            for zipped in (False, True):
                path = _path(packet, zipped=zipped)
                for method in ('GET', 'HEAD'):
                    result = await http.request(
                        method, path, headers={'User-Agent': 'AgentRuntime/1'}
                    )
                    assert result.status_code == 400
                    if method == 'GET':
                        assert result.json()['error']['code'] == 'secure_channel_required'
                        assert b64(token) not in result.text
                        assert recover not in result.text
                    assert result.headers['cache-control'] == 'no-store'
                    assert result.headers['referrer-policy'] == 'no-referrer'
        for path in (
            '/-/g/content.post_create/j/invalid?token=secret-value',
            '/?recovery_secret=secret-value',
            '/?NEW_RECOVERY_SECRET=secret-value',
            '/?%72ecovery_secret=secret-value',
            '/_money?api_key=secret-value',
        ):
            result = await http.get(path)
            assert result.status_code == 400
            assert result.json()['error']['code'] == 'secure_channel_required'
            assert 'secret-value' not in result.text
    async with app.metadata.transaction(write=False) as tx:
        after = (
            tx.one('SELECT COUNT(*) FROM resources')[0],
            tx.one('SELECT COUNT(*) FROM credentials')[0],
            tx.one('SELECT COUNT(*) FROM events')[0],
        )
    assert after == before
    assert b64(token) not in caplog.text and recover not in caplog.text


@pytest.mark.asyncio
async def test_rejected_query_secrets_are_not_echoed_or_logged(installed, caplog):
    app, _ = installed
    values = {
        'token': 'QUERY_TOKEN_SECRET_8b01',
        'recovery_secret': 'QUERY_RECOVERY_SECRET_9c12',
        'new_recovery_secret': 'QUERY_NEW_RECOVERY_SECRET_ad23',
        'bootstrap_claim': 'QUERY_BOOTSTRAP_CLAIM_be34',
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for name, secret in values.items():
            response = await http.get('/?' + name + '=' + secret)
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'secure_channel_required'
            assert secret not in response.text
            assert secret not in caplog.text


@pytest.mark.asyncio
async def test_legacy_short_get_secret_routes_are_disabled_and_not_advertised(installed):
    app, _ = installed
    dictionary = build_dictionary(app.registry)
    code = dictionary.code_for('operation', 'content.post_create@1')
    secret = b64(os.urandom(32))
    path = f'/-/g/{code}/token/credential/{secret}/u_somebody/request/2026-09-27T00:05:00Z/args'
    with pytest.raises(Failure, match='secure_channel_required'):
        dictionary.decode_direct_write_path(code, ['token', 'credential', secret])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for method in ('GET', 'HEAD'):
            response = await http.request(method, path, headers={'User-Agent': 'AgentRuntime/1'})
            assert response.status_code == 400
            if method == 'GET':
                assert response.json()['error']['code'] == 'secure_channel_required'
                assert secret not in response.text
        document = await http.get('/-/d/content.post_create')
        assert document.status_code == 200
        for row in document.json()['operations']:
            assert row['direct_write_template'] is None
            assert row['direct_expected_template'] is None
            assert '/token/' not in str(row) and '/bootstrap/' not in str(row)
        issuer = await http.get('/-/d/identity.temporary')
        assert issuer.status_code == 200
        for row in issuer.json()['operations']:
            assert row['shortest_template'] == '/-/p/identity.temporary'
            assert row['requires_secure_channel'] is True


@pytest.mark.asyncio
async def test_path_client_rejects_secrets_locally_but_signed_nonsecret_get_works(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'url-path-client')
    transport = PathGETTransport(
        app.settings.service_url,
        http=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
        ),
    )
    try:
        token_packet = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'no token URL'},
            app.settings.service_url,
            subject=subject,
            token=('fake', os.urandom(32)),
        )
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(token_packet)
        claim_packet = request_for(
            'identity.temporary', {'nonce': b64(os.urandom(32))}, app.settings.service_url
        )
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(claim_packet)
        recovery_packet = request_for(
            'identity.token_recover',
            {
                'credential_id': 'fake-credential',
                'recovery_secret': b64(os.urandom(32)),
                'new_recovery_secret': b64(os.urandom(32)),
                'nonce': b64(os.urandom(32)),
            },
            app.settings.service_url,
            signer=key,
            subject=subject,
            request_id='secret-recovery-pathget',
            expires_at=NOW + timedelta(seconds=90),
        )
        with pytest.raises(Failure, match='secure_channel_required'):
            await transport.call(recovery_packet)
        assert transport.calls == 0
        signed = request_for(
            'content.post_create',
            {'parent': '/main', 'body': 'signed proof remains usable'},
            app.settings.service_url,
            signer=key,
            subject=subject,
            request_id='signed-get-safe',
            expires_at=NOW + timedelta(seconds=90),
        )
        result = await transport.call(signed)
        assert result.status == 'ok', wire(result)
        assert (await transport.call(signed)).replayed
    finally:
        await transport.close()
