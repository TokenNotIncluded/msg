"""Public SVG/text editing uses a single quota gate across browser, CLI and batch."""

import re
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import NOW, call, register

from msg.core.errors import Failure
from msg.core.requests import request_for
from msg.plugins.public_board import DEFAULT_SVG, KEY, LIMITS, validate_svg
from msg.transports.home_page import home_html
from msg.transports.oauth_http import csrf


@pytest.mark.parametrize(
    'svg',
    [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 300"><script>alert(1)</script></svg>',
        DEFAULT_SVG.replace('<g ', '<g onload="alert(1)" ', 1),
        DEFAULT_SVG.replace('<g ', '<g style="fill:red" ', 1),
        DEFAULT_SVG.replace('<g ', '<g href="https://outside.invalid" ', 1),
        DEFAULT_SVG.replace('attributeName="fill"', 'attributeName="href"', 1),
        DEFAULT_SVG.replace('dur="12s"', 'dur="0.001s"', 1),
        DEFAULT_SVG.replace('viewBox="0 0 960 300"', 'viewBox="0 0 1000000 1000000"', 1),
        '<!DOCTYPE svg [<!ENTITY a "a">]>' + DEFAULT_SVG,
        re.sub(r'values="[^"]+"', 'values="url(https://outside.invalid)"', DEFAULT_SVG, count=1),
    ],
)
def test_svg_rejects_active_or_unbounded_content(svg):
    with pytest.raises(Failure):
        validate_svg(svg)


def test_shared_editor_is_escaped_and_replaces_token_hero():
    html = home_html(
        public_board={
            'generation': 1,
            'svg': DEFAULT_SVG,
            'text': '</p><script>bad()</script>',
            'quota': None,
        }
    ).decode()
    assert '公共栏 / Shared board' in html and 'class="token-cloud"' not in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html
    assert 'SVG 源码 / SVG source' in html and '文本 / Text' in html
    assert '每账号每小时 5 次' in html and '禁止批量' in html
    assert 'data-refresh' in html
    assert '<animate ' not in html
    assert '&lt;svg' in html


async def test_every_user_edits_both_parts_and_limits_are_atomic(installed):
    app, _ = installed
    key, first, _ = await register(app, 'shared-first')
    other_key, second, _ = await register(app, 'shared-second')
    before = await call(app, 'discovery.public_board', {})
    assert before.status == 'ok', before.error
    denied = await call(app, 'content.public_board_update', {'generation': 0, 'text': 'anonymous'})
    assert denied.error.code == 'authentication_required'
    svg = DEFAULT_SVG.replace('>svg + text<', '>shared + text<')
    assert svg != DEFAULT_SVG
    changed = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'svg': svg, 'text': '一起修改'},
        key=key,
        subject=first,
        rid='shared-idempotent',
    )
    assert changed.status == 'ok', changed.error
    assert changed.data['svg'] == svg and changed.data['text'] == '一起修改'
    assert changed.data['quota']['hour_count'] == 1
    retry = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'svg': svg, 'text': '一起修改'},
        key=key,
        subject=first,
        rid='shared-idempotent',
    )
    assert retry.status == 'ok', retry.error
    conflict = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'text': 'stale'},
        key=other_key,
        subject=second,
    )
    assert conflict.error.code == 'public_board_conflict'
    own = await call(app, 'discovery.public_board', {}, key=other_key, subject=second)
    assert own.data['quota']['hour_count'] == 0
    changed = await call(
        app,
        'content.public_board_update',
        {'generation': 1, 'text': '另一个用户'},
        key=other_key,
        subject=second,
    )
    assert changed.status == 'ok', changed.error
    assert changed.data['svg'] == svg
    cooldown = await call(
        app,
        'content.public_board_update',
        {'generation': 2, 'text': 'too soon'},
        key=key,
        subject=first,
    )
    assert cooldown.error.code == 'public_board_cooldown'
    app.authenticator.clock = lambda: app.clock()
    generation = 2
    for minute in range(1, 5):
        app.executor.clock = app.clock = lambda minute=minute: NOW + timedelta(seconds=minute * 60)
        packet = request_for(
            'content.public_board_update',
            {'generation': generation, 'text': f'edit {minute}'},
            app.settings.service_url,
            signer=key,
            subject=first,
            expires_at=app.clock() + timedelta(seconds=120),
        )
        changed = await app.executor.execute(packet)
        assert changed.status == 'ok', changed.error
        generation += 1
    app.executor.clock = app.clock = lambda: NOW + timedelta(minutes=5)
    packet = request_for(
        'content.public_board_update',
        {'generation': generation, 'text': 'over quota'},
        app.settings.service_url,
        signer=key,
        subject=first,
        expires_at=app.clock() + timedelta(seconds=120),
    )
    refused = await app.executor.execute(packet)
    assert refused.error.code == 'public_board_rate_limited'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting(KEY)['generation'] == generation
        assert tx.setting('public_board_quota:' + first)['hour_count'] == 5


async def test_release_default_never_overwrites_an_existing_user_board(installed):
    from msg.core.public_board_art import (
        DEFAULT_SVG as ART_SVG,
        DEFAULT_TEXT as ART_TEXT,
        default_art as art,
    )
    from msg.plugins.public_board import default, default_art

    assert default_art is art and default()['svg'] == ART_SVG
    assert default()['text'] == ART_TEXT
    app, _ = installed
    saved = {
        **default(),
        'generation': 23,
        'svg': DEFAULT_SVG.replace('>svg + text<', '>user artwork<'),
        'text': '用户保存的正文',
        'updated_by': 'existing-account',
        'history': [{**default(), 'generation': 22, 'text': 'previous user text'}],
    }
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(KEY, saved)
    read = await call(app, 'discovery.public_board', {})
    assert read.status == 'ok', read.error
    assert read.data['generation'] == 23 and read.data['svg'] == saved['svg']
    assert read.data['text'] == saved['text'] and read.data['updated_by'] == saved['updated_by']
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting(KEY) == saved


async def test_batch_size_and_payload_limits_cannot_bypass_gate(installed):
    from test_batch import packet

    app, _ = installed
    key, user, _ = await register(app, 'shared-batch')
    child = packet(
        app,
        key,
        user,
        'content.public_board_update',
        {'generation': 0, 'text': 'batch'},
        'public-child',
    )
    batch = await call(app, 'batch.atomic', {'requests': [child]}, key=key, subject=user)
    assert batch.error.code == 'operation_not_batchable'
    large = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'svg': DEFAULT_SVG + ' ' * 16384},
        key=key,
        subject=user,
    )
    assert large.status == 'error'
    text = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'text': 'a' * 2001},
        key=key,
        subject=user,
    )
    assert text.status == 'error'
    invalid = await call(
        app,
        'content.public_board_update',
        {
            'generation': 0,
            'svg': DEFAULT_SVG.replace('attributeName="fill"', 'attributeName="href"'),
        },
        key=key,
        subject=user,
    )
    assert invalid.error.code == 'public_board_unsafe_svg'
    own = await call(app, 'discovery.public_board', {}, key=key, subject=user)
    assert own.data['generation'] == 0 and own.data['quota']['hour_count'] == 0


async def test_browser_save_requires_csrf_and_uses_same_quota(oauth):
    app, _, user, http = oauth
    await browser_login(oauth)
    cookie = http.cookies.get('msg_session')
    page = await http.get('/', headers={'Accept': 'text/html'})
    assert page.status_code == 200, page.text
    assert '公共栏 / Shared board' in page.text
    assert 'data-signed-in="true"' in page.text
    art = await http.get('/_public-board/art.svg')
    assert art.status_code == 200 and '<animate' in art.text
    assert "default-src 'none'" in art.headers['content-security-policy']
    still = await http.get('/_public-board/art.svg?motion=still')
    assert still.status_code == 200
    assert not any(
        element.tag.rsplit('}', 1)[-1] in {'animate', 'animateTransform'}
        for element in ET.fromstring(still.content).iter()
    )
    args = {
        'operation': 'content.public_board_update',
        'generation': 0,
        'text': '<img src=x onerror=bad()>',
        'request_id': 'public-browser',
    }
    assert (await http.post('/oauth/post-action', json=args)).status_code == 400
    changed = await http.post(
        '/oauth/post-action',
        json={**args, 'csrf': csrf(cookie)},
        headers={'Origin': app.settings.service_url},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()['data']['updated_by'] == user
    assert changed.json()['data']['quota']['hour_count'] == 1
    read = await http.get('/_public-board')
    assert read.status_code == 200 and read.json()['quota']['hour_count'] == 1
    page = await http.get('/', headers={'Accept': 'text/html'})
    assert '&lt;img src=x onerror=bad()&gt;' in page.text
    assert (await http.head('/_public-board/art.svg')).content == b''


async def test_daily_global_quota_and_history_recovery(installed):
    from msg.admin.recovery_proof import capture
    from msg.plugins.public_board import periods

    app, root = installed
    key, user, _ = await register(app, 'shared-daily')
    hour, day = periods(NOW)
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting(
            'public_board_quota:' + user,
            {'hour': hour, 'day': day, 'hour_count': 0, 'day_count': 20},
        )
    refused = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'text': 'day limit'},
        key=key,
        subject=user,
    )
    assert refused.error.code == 'public_board_rate_limited'
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('public_board_quota:' + user, {})
        tx.set_setting(
            'public_board_global_quota',
            {'hour': hour, 'day': day, 'hour_count': LIMITS['global_hour'], 'day_count': 0},
        )
    refused = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'text': 'global limit'},
        key=key,
        subject=user,
    )
    assert refused.error.code == 'public_board_global_rate_limited'
    async with app.metadata.transaction(write=True) as tx:
        tx.set_setting('public_board_global_quota', {})
    changed = await call(
        app,
        'content.public_board_update',
        {'generation': 0, 'text': 'saved to recovery'},
        key=key,
        subject=user,
    )
    assert changed.status == 'ok', changed.error
    captured = await capture(app, root, source_backup_sha256='a' * 64, sequence=1)
    from msg.admin.recovery_state import snapshot

    before_digest = captured['state']['metadata']['tables']['settings']['sha256']
    async with app.metadata.transaction(write=True) as tx:
        record = tx.setting(KEY)
        tx.set_setting(KEY, {**record, 'text': 'changed after proof'})
        after_digest = snapshot(tx)['tables']['settings']['sha256']
        tx.set_setting(KEY, record)
    assert before_digest != after_digest
    after = await call(app, 'discovery.public_board', {})
    assert after.data['history'][0]['generation'] == 0
    assert 'svg' not in after.data['history'][0]
