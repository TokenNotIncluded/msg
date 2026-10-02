"""A limited browser can refresh public art without acquiring personal authority."""

from datetime import timedelta

import pytest
from test_oauth import browser_login, oauth as oauth
from test_post_read_views import restrict
from test_service import call

from msg.core.codec import wire
from msg.core.requests import request_for
from msg.plugins.public_board import KEY


@pytest.mark.asyncio
async def test_browser_board_refresh_survives_old_ceiling_without_granting_writes(oauth):
    app, _, subject, http = oauth
    cookie = await browser_login(oauth)
    credential_id, secret, restricted = await restrict(
        oauth, cookie, {'discovery.public_board', 'content.public_board_update'}
    )
    async with app.metadata.transaction(write=False) as tx:
        before = tx.setting(KEY)
        quota_before = tx.setting('public_board_quota:' + subject)
    anonymous = await call(app, 'discovery.public_board', {})
    assert anonymous.status == 'ok'
    refreshed = await http.get('/_public-board')
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == wire(anonymous.data)
    assert refreshed.json()['quota'] is None
    assert refreshed.headers['x-msg-public-fallback'] == 'credential-ceiling'
    head = await http.head('/_public-board')
    assert head.status_code == 200 and not head.content
    assert head.headers['content-length'] == str(len(refreshed.content))
    page = await http.get('/', headers={'Accept': 'text/html'})
    assert page.status_code == 200 and '不代表额度已用完' in page.text
    denied = await http.post(
        '/-/p/discovery.public_board',
        json=wire(
            request_for(
                'discovery.public_board',
                {},
                app.settings.service_url,
                subject=subject,
                token=(credential_id, secret),
                expires_at=app.clock() + timedelta(minutes=3),
            )
        ),
    )
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'credential_ceiling'
    write = await call(
        app,
        'content.public_board_update',
        {'generation': refreshed.json()['generation'], 'text': 'forbidden'},
        subject=subject,
        token=(credential_id, secret),
    )
    assert write.error.code == 'credential_ceiling'
    for header in ('Authorization', 'X-Msg-Request'):
        for value in ('invalid', ''):
            explicit = await http.get('/_public-board', headers={header: value})
            assert explicit.status_code == 400
            assert explicit.json()['error']['code'] in {'ambiguous_credentials', 'invalid_request'}
    async with app.metadata.transaction(write=False) as tx:
        assert (await tx.credential(credential_id)).ceiling == restricted.ceiling
        assert tx.setting(KEY) == before
        assert tx.setting('public_board_quota:' + subject) == quota_before
