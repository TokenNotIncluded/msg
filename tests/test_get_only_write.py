"""GET-only execution keeps signed proofs, not reusable URL credentials."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app
from test_service import NOW, register


@pytest.mark.asyncio
async def test_legacy_token_and_bootstrap_urls_reject_without_business_write(installed):
    app, _ = installed
    code = build_dictionary(app.registry).code_for('operation', 'content.post_create@1')
    token = b64(b't' * 32)
    path = f'/-/g/{code}/token/cred/{token}/u_someone/once/2026-09-27T00:05:00Z/args'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        for verb in ('GET', 'HEAD'):
            result = await http.request(verb, path, headers={'User-Agent':'AgentRuntime/1'})
            assert result.status_code == 400
            if verb == 'GET':
                assert result.json()['error']['code'] == 'secure_channel_required'
                assert token not in result.text
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one("SELECT COUNT(*) FROM resources WHERE type='post'")[0] == 0


@pytest.mark.asyncio
async def test_signed_get_still_obeys_idempotency_and_expected_generation(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'get-proof-agent')
    packet = request_for('content.post_create', {'parent':'/main',
        'body':'signed GET proof'}, app.settings.service_url, signer=key,
        subject=subject, request_id='signed-get-once',
        expires_at=NOW+timedelta(seconds=90), expected=(('t_main',999),))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url='http://testserver') as http:
        stale = await http.get('/-/g/content.post_create/j/'+b64(canonical(packet)),
                               headers={'User-Agent':'AgentRuntime/1'})
        assert stale.status_code == 409
        assert stale.json()['error']['code'] == 'generation_conflict'
        async with app.metadata.transaction(write=False) as tx:
            generation = (await tx.resource('t_main')).generation
        valid = request_for('content.post_create', {'parent':'/main',
            'body':'signed GET proof'}, app.settings.service_url, signer=key,
            subject=subject, request_id='signed-get-valid',
            expires_at=NOW+timedelta(seconds=90), expected=(('t_main',generation),))
        path = '/-/g/content.post_create/j/'+b64(canonical(valid))
        first = await http.get(path, headers={'User-Agent':'AgentRuntime/1'})
        assert first.status_code == 200, first.text
        replay = await http.get(path, headers={'User-Agent':'AgentRuntime/1'})
        assert replay.status_code == 200 and replay.json()['replayed']
        assert replay.headers['cache-control'] == 'no-store'
        assert replay.headers['referrer-policy'] == 'no-referrer'
