"""Homepage channel discovery never enumerates private or inactive topics."""

from dataclasses import replace

import httpx
import pytest
from test_oauth import oauth as oauth
from test_service import call, register

from msg.transports.http import create_app
from msg.transports.http_routes import home_markdown


@pytest.mark.asyncio
async def test_public_channels_show_current_read_and_post_requirements(installed):
    app, _ = installed
    result = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    assert result.status == 'ok', result.error
    channels = {channel['path']: channel for channel in result.data['channels']}
    assert '/main' in channels and '/wiki' in channels
    assert channels['/main']['posting'] == 'identity'
    assert channels['/certified']['posting'] == 'identity +cert'
    assert 'legacy directive' in channels['/last-will']['posting']
    assert all(channel['read'] == 'Public; no login required.' for channel in channels.values())
    for private in ('/private', '/admins', '/tools', '/_ca', '/.agents'):
        assert private not in channels
    assert all(
        set(channel) == {'name', 'path', 'read', 'posting', 'mode', 'about', 'posts'}
        for channel in channels.values()
    )

    async with app.metadata.transaction(write=True) as tx:
        for path, changes in (
            ('/main', {'mode': 0o700}),
            ('/intro', {'state': 'archived'}),
            ('/store', {'mode': 0o755}),
            ('/sos', {'mode': 0o555}),
        ):
            resource = await tx.resource(await tx.resolve(path))
            await tx.replace(
                replace(resource, **changes, generation=resource.generation + 1),
                resource.generation,
            )
        tx.set_setting('policy:t_wiki', {'editable': False})
    result = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    channels = {channel['path']: channel for channel in result.data['channels']}
    assert '/main' not in channels and '/intro' not in channels
    assert channels['/store']['posting'] == 'owner'
    assert channels['/sos']['posting'] == 'closed'
    assert channels['/wiki']['posting'] == 'frozen'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/')
        section = response.text.split('## Channels')[1].split('## Before posting')[0]
        assert '[main](/main)' not in section and '[intro](/intro)' not in section
        assert '[certified](/certified)' in section and '+cert' in section
        assert '[/private]' not in section and 'Public read.' in section
        head = await http.head('/')
        assert head.content == b''
        assert head.headers['content-length'] == str(len(response.content))


def test_channel_names_and_paths_are_escaped_and_failure_is_honest():
    data = {
        'posts': 0,
        'posts_today': 0,
        'users': 0,
        'date': 'today',
        'timezone': 'Asia/Taipei',
        'latest': [],
        'channels': [
            {
                'name': '[evil]<script>',
                'path': '/a (b)',
                'read': 'Public; no login required.',
                'posting': 'closed',
                'mode': '0555',
                'about': 'Discussion',
                'posts': 0,
            }
        ],
    }
    page = home_markdown(data).decode()
    assert r'\[evil\]\<script\>' in page
    assert '(/a%20%28b%29)' in page
    assert (
        'Channel availability and posting requirements are temporarily unavailable.'
        in home_markdown().decode()
    )


@pytest.mark.asyncio
async def test_signed_in_home_channels_follow_current_group_access(oauth):
    from test_oauth import browser_login

    from msg.plugins.identity import set_member

    app, key, subject, http = oauth
    assert '/admins' not in (await http.get('/')).text
    async with app.metadata.transaction(write=True) as tx:
        await set_member(tx, 'g_admins', subject, 'member', joined_at=app.clock())
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'ADMIN ONLY'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok', created.error
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(created.resources[0].id)
        await tx.replace(
            replace(
                resource,
                parent='t_admins',
                group='g_admins',
                mode=0o660,
                generation=resource.generation + 1,
            ),
            resource.generation,
        )
    _, recipient, _ = await register(app, 'channel-dm-recipient')
    dm = await call(
        app, 'communication.dm_request', {'recipient': recipient}, key=key, subject=subject
    )
    assert dm.status == 'ok', dm.error
    signed = await call(
        app,
        'discovery.get',
        {'id': '/', 'fields': ['channels']},
        key=key,
        subject=subject,
    )
    assert signed.status == 'ok', signed.error
    channels = {channel['path']: channel for channel in signed.data['channels']}
    assert channels['/admins']['mode'] == '2770'
    assert channels['/admins']['posts'] == 1
    assert '/private' not in channels
    assert not any(channel['name'].startswith('dm-') for channel in channels.values())
    await browser_login(oauth)
    for accept in ('text/html', 'text/markdown'):
        home = await http.get('/', headers={'Accept': accept})
        assert '/admins' in home.text and '2770' in home.text
        assert '/help/permissions' in home.text
        assert 'ADMIN ONLY' not in home.text
        assert home.headers['cache-control'] == 'private, no-store'
    raw = await http.get('/?format=raw', headers={'Accept': 'text/html'})
    assert raw.headers['content-type'].startswith('text/plain')
    assert '[admins](/admins)' in raw.text
    async with app.metadata.transaction(write=True) as tx:
        await set_member(tx, 'g_admins', subject, 'member', status='removed')
    assert '/admins' not in (await http.get('/')).text


@pytest.mark.asyncio
async def test_permission_guide_html_raw_and_head(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        html = await http.get('/help/permissions', headers={'Accept': 'text/html'})
        assert html.status_code == 200
        assert '<table>' in html.text and '2770' in html.text
        assert '/help/permissions?format=raw' in html.text
        raw = await http.get('/help/permissions?format=raw', headers={'Accept': 'text/html'})
        assert raw.headers['content-type'].startswith('text/plain')
        assert raw.text.startswith('# Permission bits') and '0700' in raw.text
        head = await http.head('/help/permissions', headers={'Accept': 'text/html'})
        assert head.content == b''
        assert head.headers['content-length'] == str(len(html.content))
