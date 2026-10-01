"""Database-backed checks for the real OAuth-to-protocol transport boundary."""

import gzip
from datetime import timedelta

import pytest
from test_oauth import device_tokens, oauth as oauth

from msg.core.codec import canonical
from msg.core.requests import request_for


@pytest.mark.asyncio
@pytest.mark.parametrize('compressed', [False, True])
async def test_real_oauth_bearer_rebinds_body_framing(oauth, compressed):
    app, _, subject, http = oauth
    tokens = await device_tokens(oauth)
    request = request_for(
        'discovery.get',
        {'id': '/main'},
        app.settings.service_url,
        subject=subject,
        expires_at=app.clock() + timedelta(seconds=120),
    )
    raw = canonical(request)
    headers = {'Authorization': 'Bearer ' + tokens['access_token']}
    if compressed:
        raw = gzip.compress(raw)
        headers['Content-Encoding'] = 'gzip'
    response = await http.post('/-/p/discovery.get', content=raw, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'ok' and response.json()['actor'] == subject


@pytest.mark.asyncio
async def test_non_ascii_csrf_is_bad_request_not_server_error(oauth):
    _, _, _, http = oauth
    assert (await http.get('/login')).status_code == 200
    response = await http.post(
        '/oauth/login/poll',
        json={'csrf': '非ASCII'},
        headers={'Origin': str(http.base_url).rstrip('/')},
    )
    assert response.status_code == 400 and response.json() == {'error': 'invalid_request'}
