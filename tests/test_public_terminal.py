"""Public terminal commands cannot acquire credentials or shell authority."""

from dataclasses import replace

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import b64
from msg.core.errors import Failure
from msg.transports.http import create_app
from msg.transports.public_terminal import MAX_ITEMS, MAX_OUTPUT_BYTES, _link, parse_command


@pytest.mark.parametrize(
    ('line', 'normalized', 'name', 'argument'),
    [
        (' help  read ', 'help read', 'help', 'read'),
        ('ls /', 'ls /', 'ls', '/'),
        ('read /AGENTS.md', 'read /AGENTS.md', 'read', '/AGENTS.md'),
        ('read r_abc123', 'read r_abc123', 'read', 'r_abc123'),
        (
            'read schema:discovery.get:1',
            'read schema:discovery.get:1',
            'read',
            'schema:discovery.get:1',
        ),
        (
            'read /*0123456789abcdef0123456789abcdef.md',
            'read /*0123456789abcdef0123456789abcdef.md',
            'read',
            '/*0123456789abcdef0123456789abcdef.md',
        ),
        ('search  中文   two', 'search 中文 two', 'search', '中文 two'),
        ('user @Root-user', 'user @Root-user', 'user', '@Root-user'),
    ],
)
def test_terminal_command_grammar(line, normalized, name, argument):
    assert parse_command(line) == (normalized, name, argument)


@pytest.mark.parametrize(
    'line',
    [
        '',
        ' ',
        'HELP',
        'help unknown',
        'status extra',
        'ls',
        'read',
        'user',
        'search',
        'search ' + 'x' * 81,
        'search one two three four five six seven eight nine',
        'read /a b',
        'read ../secret',
        'read /main/../admins',
        'read /main/./x',
        'read //evil.test',
        'read https://evil.test',
        'read /-/_terminal',
        'read /!root',
        'read /~root',
        'read /main//x',
        'read /main/',
        'read /main?token=x',
        'read /main#x',
        'read /%2Fadmins',
        'read /*',
        'read /*abc123',
        'read /*0123456789ABCDEF0123456789ABCDEF',
        'ls /*abc',
        'read /main*',
        'user @root/keys',
        'help\nstatus',
        'help\rstatus',
        'help\x00',
        'help\x1b[2J',
        'help\tread',
        'help\u202eread',
        'help\u00a0read',
        'stats;id',
        'stats&&id',
        'stats|id',
        '$(id)',
        '`id`',
        'search >out',
        'search <file',
        'search "word"',
        "search 'word'",
        'search (word)',
        'search {word}',
        'search [word]',
        'search \\word',
        'search word*',
        'search word?',
        'search word%',
        'search ' + 'a' * 1024,
    ],
)
def test_terminal_rejects_shell_controls_and_ambiguous_arguments(line):
    with pytest.raises(Failure, match='invalid_command'):
        parse_command(line)


def test_terminal_links_cannot_escape_resource_paths():
    links = []
    for path in (
        'https://evil.test',
        '//evil.test',
        '/-/_terminal',
        '/!root',
        '/~root',
        '/main/../admins',
        '/main/./post',
        '/main\\evil',
        '/main\npost',
    ):
        _link(links, 'bad', path)
    assert links == []
    _link(links, '<script>untrusted</script>', '/main/name ?#%')
    assert links == [{'label': '<script>untrusted</script>', 'href': '/main/name%20%3F%23%25'}]
    for index in range(MAX_ITEMS + 2):
        _link(links, str(index), f'/main/{index}')
    assert len(links) == MAX_ITEMS


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
        for command in (
            'help',
            'status',
            'time',
            'server',
            'stats',
            'feed',
            'topics',
            'ls /',
            'users',
            'rules',
            'read /AGENTS.md',
            'help search',
            'clear',
        ):
            result = await http.get('/_terminal', params={'command': command})
            assert result.status_code == 200, result.text
            assert result.json()['command'] == command
            assert isinstance(result.json()['output'], str)
            assert isinstance(result.json()['links'], list)
        assert (await http.get('/_terminal?command=server')).json()[
            'output'
        ] == app.settings.service_url
        assert (await http.get('/_terminal?command=status')).json()['output'] == 'ready'
        assert (await http.head('/_terminal?command=help')).content == b''
        assert '[Terminal](/terminal)' in (await http.get('/')).text


async def test_terminal_rejects_mutations_and_credentials(installed):
    app, _ = installed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        for path in ('/terminal', '/_terminal?command=help'):
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                assert (await http.request(method, path)).status_code == 405
            for header in ('Authorization', 'X-Msg-Request', 'X-Msg-Signature', 'X-Msg-Proof'):
                for value in ('', 'untrusted'):
                    assert (await http.get(path, headers={header: value})).status_code == 400
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


async def test_terminal_browsing_rechecks_visibility_and_bounds(oauth):
    app, _, _, login_http = oauth
    key, subject, _ = await register(app, 'terminal-browser')
    _, hidden_user, _ = await register(app, 'terminal-hidden')
    topic = await call(
        app,
        'content.topic_create',
        {'parent': '/', 'name': 'terminal-public'},
        key=key,
        subject=subject,
    )
    assert topic.status == 'ok'
    posts = []
    for index in range(MAX_ITEMS + 1):
        result = await call(
            app,
            'content.post_create',
            {
                'parent': topic.resources[0].id,
                'name': f'terminal-{index:02d}-' + 'é' * 80 + 'x' * 25,
                'body': f'TERMINAL NEEDLE public item {index}',
            },
            key=key,
            subject=subject,
        )
        assert result.status == 'ok', result.error
        posts.append(result.resources[0].id)
    private = await call(
        app,
        'content.post_create',
        {'parent': topic.resources[0].id, 'body': 'TERMINAL NEEDLE SECRET-PRIVATE-BODY'},
        key=key,
        subject=subject,
    )
    long_text = '界' * 3000 + '\nSECRET-TAIL-NOT-IN-WINDOW'
    long_file = await call(
        app,
        'file.create',
        {
            'parent': topic.resources[0].id,
            'name': 'long.txt',
            'data': b64(long_text.encode()),
            'media_type': 'text/plain',
        },
        key=key,
        subject=subject,
    )
    binary = await call(
        app,
        'file.create',
        {
            'parent': topic.resources[0].id,
            'name': 'binary.bin',
            'data': b64(b'\x00\x01'),
            'media_type': 'application/octet-stream',
        },
        key=key,
        subject=subject,
    )
    assert private.status == long_file.status == binary.status == 'ok'
    bio = await call(
        app,
        'file.create',
        {
            'parent': subject,
            'name': 'BIO.md',
            'data': b64(b'SECRET-PRIVATE-BIO'),
            'media_type': 'text/markdown',
        },
        key=key,
        subject=subject,
    )
    assert bio.status == 'ok'

    async def hide(rid):
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(rid)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )

    await hide(private.resources[0].id)
    await hide(hidden_user)
    await hide(bio.resources[0].id)
    cookie = await browser_login((app, key, subject, login_http))
    assert cookie
    signed_in = await login_http.get('/', headers={'Accept': 'text/html'})
    assert 'data-signed-in="true"' in signed_in.text
    captured = []
    original = app.executor.execute

    async def capture(packet, **kwargs):
        captured.append(packet)
        return await original(packet, **kwargs)

    app.executor.execute = capture
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url=app.settings.service_url,
        cookies={'msg_session': cookie},
    ) as http:

        async def run(command):
            result = await http.get('/_terminal', params={'command': command})
            assert result.status_code == 200, result.text
            data = result.json()
            assert len(data['output'].encode()) <= MAX_OUTPUT_BYTES
            assert len(data['links']) <= MAX_ITEMS
            assert all(
                link['href'].startswith('/') and not link['href'].startswith('//')
                for link in data['links']
            )
            assert 'SECRET-PRIVATE-BODY' not in data['output']
            assert 'SECRET-PRIVATE-BIO' not in data['output']
            return data

        topics = await run('topics')
        assert '/terminal-public' in topics['output']
        listing = await run('ls /terminal-public')
        assert len(listing['links']) == MAX_ITEMS
        assert len(listing['output'].encode()) == MAX_OUTPUT_BYTES
        assert listing['output'].endswith('[Output truncated.]')
        search = await run('search NEEDLE public')
        assert search['links'] and 'public item' in search['output']
        assert "'field':" not in search['output'] and "'range':" not in search['output']
        read = await run('read ' + posts[0])
        assert 'public item 0' in read['output']
        assert read['links']
        short_path = '/*' + posts[0].rsplit('_', 1)[-1]
        for path in (short_path, short_path + '.md'):
            assert (await run('read ' + path))['output'] == read['output']
        long_read = await run('read /terminal-public/long.txt')
        assert 'Text truncated at 4096 bytes' in long_read['output']
        assert 'SECRET-TAIL-NOT-IN-WINDOW' not in long_read['output']
        assert '界' in long_read['output'] and '\ufffd' not in long_read['output']
        assert 'Binary resource.' in (await run('read ' + binary.resources[0].id))['output']
        users = await run('users')
        assert 'terminal-browser' in users['output']
        assert 'terminal-hidden' not in users['output']
        profile = await run('user @terminal-browser')
        assert 'public posts:' in profile['output'] and 'public item' in profile['output']
        assert 'Use ls' in (await run('read /terminal-public'))['output']
        help_result = await run('help read')
        assert 'example: read /AGENTS.md' in help_result['output']
        for command in ('read ' + private.resources[0].id, 'user terminal-hidden'):
            assert (await http.get('/_terminal', params={'command': command})).status_code == 403
        await hide(posts[0])
        assert (
            await http.get('/_terminal', params={'command': 'read ' + posts[0]})
        ).status_code == 403
        await hide(topic.resources[0].id)
        for command in (
            'ls /terminal-public',
            'read /terminal-public/long.txt',
            'read ' + posts[1],
        ):
            assert (await http.get('/_terminal', params={'command': command})).status_code == 403
        assert not (await run('search TERMINAL NEEDLE'))['links']
        assert '/terminal-public' not in (await run('topics'))['output']
    assert captured
    assert all(packet.subject is None and packet.proof is None for packet in captured)
    assert all(
        app.registry.operation(packet.operation, packet.contract_version).effect == 'read'
        for packet in captured
    )
    assert all(packet.arguments.get('limit', MAX_ITEMS) <= MAX_ITEMS for packet in captured)
