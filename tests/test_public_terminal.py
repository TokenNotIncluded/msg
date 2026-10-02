"""Public terminal commands cannot acquire credentials or shell authority."""

from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import b64
from msg.core.errors import Failure
from msg.transports import public_terminal
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
    _link(links, '<script>untrusted</script>', '/main/name ?#%', 'r_example')
    assert links == [{'label': '<script>untrusted</script>', 'href': '/_id/r_example'}]
    for index in range(MAX_ITEMS + 2):
        _link(links, str(index), f'/main/{index}')
    assert len(links) == MAX_ITEMS


def test_terminal_long_path_links_use_stable_resource_ids():
    links = []
    long_path = '/main/' + 'a' * 160
    _link(links, 'long', long_path, 'r_example')
    _link(links, 'unsafe', '//evil.test/' + 'a' * 160, 'r_example')
    assert links == [{'label': 'long', 'href': '/_id/r_example'}]
    for path in ('/main/spaces here', '/main/semi;colon', '/main/question?mark'):
        _link(links, 'same resource', path, 'r_example')
    assert len(links) == 1
    assert parse_command('read /main/' + '界' * 100)[1] == 'read'
    with pytest.raises(Failure, match='invalid_command'):
        parse_command('read ' + long_path)


@pytest.mark.parametrize('small_segments', [False, True])
async def test_terminal_read_caps_segment_calls_and_utf8_window(monkeypatch, small_segments):
    calls = []
    service = SimpleNamespace(
        settings=SimpleNamespace(
            server=SimpleNamespace(limits=SimpleNamespace(max_response_bytes=65536))
        )
    )

    async def read(_service, operation, arguments, version=1):
        if operation == 'discovery.get':
            return {
                'name': 'long.md',
                'path': '/main/long.md',
                'id': 'r_test',
                'type': 'file',
                'revision': 'v_test',
            }
        calls.append(arguments)
        text = 'a\n\n' if small_segments else '# Title\n\n' if len(calls) == 1 else '界' * 1365
        return {'text': text, 'next': '/_r/c/token' + str(len(calls))}

    monkeypatch.setattr(public_terminal, '_read', read)
    output = await public_terminal._content(service, 'r_test', [])
    text = output.split('\n\n', 1)[1].split('\n[Text truncated', 1)[0]
    assert len(text.encode()) <= public_terminal.MAX_READ_BYTES
    assert '\ufffd' not in text
    assert len(calls) == (public_terminal.MAX_READ_SEGMENTS if small_segments else 2)
    assert calls[0]['revision'] == 'v_test'
    assert all(set(arguments) == {'cursor'} for arguments in calls[1:])
    assert ('8 segments' if small_segments else '4096 bytes') in output


async def test_terminal_read_collects_markdown_blocks_and_long_paths(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'terminal-segments')
    body = '# Terminal browser post\n\nTERMINAL_PUBLIC_BROWSER\n<script>window.__TERMINAL_XSS=1</script>'
    short = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'terminal-blocks', 'body': body},
        key=key,
        subject=subject,
    )
    wide = await call(
        app,
        'file.create',
        {
            'parent': '/main',
            'name': '界' * 100,
            'data': b64(body.encode()),
            'media_type': 'text/markdown',
        },
        key=key,
        subject=subject,
    )
    first = await call(
        app,
        'content.topic_create',
        {'parent': '/main', 'name': 'a' * 80},
        key=key,
        subject=subject,
    )
    second = await call(
        app,
        'content.topic_create',
        {'parent': first.resources[0].id, 'name': 'b' * 80},
        key=key,
        subject=subject,
    )
    long = await call(
        app,
        'file.create',
        {
            'parent': second.resources[0].id,
            'name': 'long.md',
            'data': b64(body.encode()),
            'media_type': 'text/markdown',
        },
        key=key,
        subject=subject,
    )
    spaced = await call(
        app,
        'file.create',
        {
            'parent': '/main',
            'name': 'spaces and;punctuation.md',
            'data': b64(body.encode()),
            'media_type': 'text/markdown',
        },
        key=key,
        subject=subject,
    )
    assert (
        short.status
        == wide.status
        == first.status
        == second.status
        == long.status
        == spaced.status
        == 'ok'
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
        for reference in (
            '/main/terminal-blocks.md',
            short.resources[0].id,
            '/main/' + '界' * 100,
            long.resources[0].id,
            spaced.resources[0].id,
        ):
            response = await http.get('/_terminal', params={'command': 'read ' + reference})
            assert response.status_code == 200, response.text
            data = response.json()
            assert data['output'].endswith(body)
            assert 'Text truncated' not in data['output']
            assert data['links']
            read_here = await http.get(
                '/_terminal', params={'command': 'read ' + unquote(data['links'][0]['href'])}
            )
            assert read_here.status_code == 200, read_here.text
            assert read_here.json()['output'] == data['output']
            linked = await http.get(data['links'][0]['href'])
            assert linked.status_code == 200, linked.text
            if reference in (long.resources[0].id, spaced.resources[0].id):
                assert data['links'][0]['href'] == '/_id/' + reference
    reads = [packet for packet in captured if packet.operation == 'discovery.read_segment']
    assert any(set(packet.arguments) == {'cursor'} for packet in reads)
    assert all(packet.subject is None and packet.proof is None for packet in captured)


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
