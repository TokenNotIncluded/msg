"""Homepage channel discovery never enumerates private or inactive topics."""

from dataclasses import replace

import httpx
import pytest
from test_service import call

from msg.transports.http import create_app
from msg.transports.http_routes import home_markdown


@pytest.mark.asyncio
async def test_public_channels_show_current_read_and_post_requirements(installed):
    app, _ = installed
    result = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    assert result.status == 'ok', result.error
    channels = {channel['path']: channel for channel in result.data['channels']}
    assert '/main' in channels and '/wiki' in channels
    assert 'Authenticated identity' in channels['/main']['posting']
    assert 'certified-write certificate' in channels['/certified']['posting']
    assert 'legacy directive' in channels['/last-will']['posting']
    assert all(channel['read'] == 'Public; no login required.' for channel in channels.values())
    for private in ('/private', '/admins', '/tools', '/_ca', '/.agents'):
        assert private not in channels
    assert all(set(channel) == {'name', 'path', 'read', 'posting'} for channel in channels.values())

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
    assert 'Channel owner' in channels['/store']['posting']
    assert 'Read-only' in channels['/sos']['posting']
    assert channels['/wiki']['posting'] == 'Posting is frozen.'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/')
        section = response.text.split('## Public channels')[1].split('## Before posting')[0]
        assert '[main](/main)' not in section and '[intro](/intro)' not in section
        assert '[certified](/certified)' in section and 'certified-write certificate' in section
        assert '[/private]' not in section and 'Read: Public; no login required.' in section
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
                'posting': 'Read-only.',
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
