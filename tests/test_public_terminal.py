"""Public terminal commands cannot acquire credentials or shell authority."""

from dataclasses import replace

import httpx
import pytest
from test_service import call, register

from msg.transports.http import create_app


async def test_terminal_page_and_fixed_commands(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = await http.get('/terminal', headers={'Accept': 'text/html'})
        assert page.status_code == 200
        assert 'Public terminal' in page.text
        assert "script-src 'sha256-" in page.headers['content-security-policy']
        assert 'form-action' in page.headers['content-security-policy']
        assert (await http.head('/terminal')).content == b''
        for command in ('help', 'status', 'time', 'server', 'stats', 'feed', 'clear'):
            result = await http.get('/_terminal', params={'command': command})
            assert result.status_code == 200, result.text
            assert result.json()['command'] == command
            assert isinstance(result.json()['output'], str)
        assert (await http.get('/_terminal?command=server')).json()[
            'output'
        ] == app.settings.service_url
        assert (await http.get('/_terminal?command=status')).json()['output'] == 'ready'
        assert (await http.head('/_terminal?command=help')).content == b''
        assert '[Terminal](/terminal)' in (await http.get('/')).text


@pytest.mark.parametrize('path', ['/terminal', '/_terminal?command=help'])
async def test_terminal_rejects_mutations_and_credentials(installed, path):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            assert (await http.request(method, path)).status_code == 405
        for header in ('Authorization', 'X-Msg-Request'):
            assert (await http.get(path, headers={header: 'untrusted'})).status_code == 400
        for command in ('ls', 'cat /etc/passwd', 'status;id', '$(id)', 'stats\nserver'):
            assert (await http.get('/_terminal', params={'command': command})).status_code == 400
        for query in ('', '?command=help&command=stats', '?command=help&path=/etc/passwd'):
            assert (await http.get('/_terminal' + query)).status_code == 400
        assert (await http.get('/terminal?command=help')).status_code == 400


async def test_terminal_feed_uses_only_anonymous_data(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'terminal-author')
    public = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'PUBLIC TERMINAL POST'},
        key=key,
        subject=subject,
    )
    private = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'PRIVATE TERMINAL POST'},
        key=key,
        subject=subject,
    )
    assert public.status == private.status == 'ok'
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(private.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    captured = []
    original = app.executor.execute

    async def capture(packet, **kwargs):
        captured.append(packet)
        return await original(packet, **kwargs)

    app.executor.execute = capture
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        http.cookies.set('msg_session', 'untrusted')
        feed = await http.get('/_terminal?command=feed')
        assert feed.status_code == 200, feed.text
        assert 'PUBLIC TERMINAL POST' in feed.text
        assert 'PRIVATE TERMINAL POST' not in feed.text
        stats = await http.get('/_terminal?command=stats')
        assert stats.status_code == 200
        assert 'public posts  1' in stats.json()['output']
    assert captured
    assert all(packet.subject is None and packet.proof is None for packet in captured)
