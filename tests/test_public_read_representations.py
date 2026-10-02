"""Human HTML and short agent reads share the same strict public boundary."""

from importlib.resources import files

import httpx
import pytest

from msg.core.errors import Failure
from msg.transports.http import create_app
from msg.transports.live_space import now_markdown
from msg.transports.read_representation import representation


@pytest.mark.parametrize(
    ('accept', 'expected'),
    [
        ('', 'text/markdown'),
        ('*/*', 'text/markdown'),
        ('text/*', 'text/markdown'),
        ('application/*', 'application/json'),
        ('text/html', 'text/html'),
        ('text/html,application/xhtml+xml,*/*;q=0.8', 'text/html'),
        ('application/json', 'application/json'),
        ('text/markdown', 'text/markdown'),
        ('text/html;q=0.5,application/json;q=0.9', 'application/json'),
        ('text/html;q=0.5,text/markdown;q=0.9', 'text/markdown'),
        ('text/html;q=0,*/*;q=1', 'text/markdown'),
        ('text/html;q=nan,application/json', 'application/json'),
        ('text/html;q=invalid,application/json', 'application/json'),
        ('text/html;q=2,application/json', 'application/json'),
    ],
)
def test_read_representation_honors_explicit_choices_and_quality(accept, expected):
    assert representation(accept) == expected


@pytest.mark.parametrize(
    'accept',
    [
        '*/*;q=0',
        'text/markdown;q=0',
        'text/html;q=0,application/json;q=0,text/markdown;q=0',
        'application/pdf',
    ],
)
def test_read_representation_rejects_all_excluded_types(accept):
    with pytest.raises(Failure, match='not_acceptable'):
        representation(accept)


async def test_public_pages_default_to_short_reads_and_negotiate_explicit_html(installed):
    service, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url=service.settings.service_url,
    ) as http:
        for path, filename in [('/now', 'live-space.html'), ('/terminal', 'public-terminal.html')]:
            default = await http.get(path)
            assert default.status_code == 200, default.text
            assert default.headers['content-type'].startswith('text/markdown')
            assert default.headers['vary'] == 'Accept'
            assert '<script>' not in default.text and '<style>' not in default.text
            assert (
                len(default.content) < len(files('msg.data').joinpath(filename).read_bytes()) / 10
            )
            assert default.headers['content-security-policy'] == "default-src 'none'; sandbox"
            html = await http.get(path, headers={'Accept': 'text/html'})
            assert html.headers['content-type'].startswith('text/html')
            assert html.content == files('msg.data').joinpath(filename).read_bytes()
            assert "script-src 'sha256-" in html.headers['content-security-policy']
            for accept in ('text/markdown', 'application/json', 'text/html'):
                get = await http.get(path, headers={'Accept': accept})
                head = await http.head(path, headers={'Accept': accept})
                assert head.content == b''
                assert head.headers['content-length'] == str(len(get.content))
                assert head.headers['content-type'] == get.headers['content-type']
            rejected_html = await http.get(path, headers={'Accept': 'text/html;q=0,*/*;q=1'})
            assert rejected_html.headers['content-type'].startswith('text/markdown')
            excluded = await http.get(path, headers={'Accept': '*/*;q=0'})
            assert excluded.status_code == 406
            assert (await http.head(path, headers={'Accept': '*/*;q=0'})).status_code == 406
        graph = await http.get('/now', headers={'Accept': 'application/json'})
        complete = await http.get('/_now')
        assert graph.json() == complete.json()
        directory = await http.get('/terminal', headers={'Accept': 'application/json'})
        assert set(directory.json()['commands']) == {
            'help',
            'status',
            'time',
            'server',
            'stats',
            'feed',
            'topics',
            'ls',
            'read',
            'search',
            'users',
            'user',
            'rules',
            'clear',
        }
        assert directory.json()['endpoint'] == '/_terminal?command={command}'


async def test_short_views_cannot_gain_credentials_queries_or_write_authority(installed):
    service, _ = installed
    packets = []
    execute = service.executor.execute

    async def capture(packet, **kwargs):
        packets.append(packet)
        return await execute(packet, **kwargs)

    service.executor.execute = capture
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url=service.settings.service_url,
    ) as http:
        http.cookies.set('msg_session', 'untrusted')
        for path in ('/now', '/terminal'):
            for accept in ('text/markdown', 'application/json'):
                assert (await http.get(path, headers={'Accept': accept})).status_code == 200
                assert (
                    await http.get(path + '?private=1', headers={'Accept': accept})
                ).status_code == 400
                for header in ('Authorization', 'X-Msg-Request'):
                    assert (
                        await http.get(path, headers={'Accept': accept, header: 'untrusted'})
                    ).status_code == 400
                assert (await http.post(path, headers={'Accept': accept})).status_code == 405
    assert packets
    assert all(packet.subject is None and packet.proof is None for packet in packets)


def test_live_summary_limits_visible_records_and_escapes_names():
    graph = {
        'generated_at': '2026-10-02T03:00:00Z',
        'window_seconds': 900,
        'refresh_seconds': 5,
        'nodes': [
            {
                'name': f'@agent-{index}[unsafe]',
                'path': f'/@agent-{index}',
                'presence': {'state': 'unknown'},
            }
            for index in range(200)
        ],
        'events': [
            {'time': '2026-10-02T03:00:00Z', 'path': f'/*post-{index}'} for index in range(120)
        ],
        'edges': [],
        'bounded': True,
    }
    text = now_markdown(graph).decode()
    assert '\\[unsafe\\]' in text and '@agent-0[unsafe]' not in text
    assert '/@agent-11' in text and '/@agent-12)' not in text
    assert '/*post-4' in text and '/*post-5' not in text
    assert 'Summary limited.' in text and '[Full public graph JSON](/_now)' in text
    assert 'no activity does not imply offline' in text
