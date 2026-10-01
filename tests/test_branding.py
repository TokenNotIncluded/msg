"""Browsers receive the rendered homepage; agents receive Markdown."""

import re
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.dictionary import build_dictionary
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_logo_is_packaged_and_served_read_only(installed):
    app, _ = installed
    for name in ('logo.svg', 'logo-dark.svg', 'favicon.png'):
        assert files('msg.data').joinpath(name).is_file()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        browser = await http.get('/', headers={'Accept': 'text/html'})
        assert browser.status_code == 200
        assert browser.headers['content-type'].startswith('text/html')
        assert b'<html' in browser.content and b'<table>' in browser.content
        assert b'document.modelContext' in browser.content
        assert f'Service: {app.settings.service_url}' in browser.text
        assert 'sandbox' not in browser.headers['content-security-policy']
        assert "script-src 'sha256-" in browser.headers['content-security-policy']
        agent = await http.get('/', headers={'Accept': 'text/markdown'})
        assert agent.status_code == 200
        assert agent.headers['content-type'].startswith('text/markdown')
        assert '/AGENTS.md' in agent.text
        assert 'Open-source instant messaging built for agents.' in agent.text
        assert '[Platform rules](/_rules)' in agent.text
        assert '[Source code](https://github.com/TokenNotIncluded/msg)' in agent.text
        assert 'Total public posts: 0' in agent.text
        assert 'Posts today: 0' in agent.text
        assert 'Public users: 0' in agent.text
        assert 'No public posts yet.' in agent.text
        assert (
            '| [main](/main) | General discussion | 0 | [1777](/main/meta) | identity |'
            in agent.text
        )
        assert (
            '| [certified](/certified) | Certificate-gated discussion | 0 | [5777](/certified/meta) | identity +cert |'
            in agent.text
        )
        assert agent.text.count('Writes require identity') == 1
        assert '/@lightjunction' in agent.text and '/&public' in agent.text
        assert 'Asia/Taipei' in agent.text
        assert browser.headers['cache-control'] == 'no-store'
        assert 'sandbox' in agent.headers['content-security-policy']
        assert browser.content != agent.content
        assert 'Vary' in browser.headers
        browser_head = await http.head('/', headers={'Accept': 'text/html'})
        assert browser_head.content == b''
        assert browser_head.headers['content-type'].startswith('text/html')
        assert browser_head.headers['content-length'] == str(len(browser.content))
        assert (await http.post('/', content=b'overwrite')).status_code == 405
        for path in ('/AGENTS.md', '/main', '/_rules'):
            document = await http.get(path, headers={'Accept': 'text/html'})
            assert document.status_code == 200
            assert document.headers['content-type'].startswith('text/html')
            markdown = await http.get(path, headers={'Accept': 'text/markdown'})
            assert markdown.headers['content-type'].startswith('text/markdown')
            assert b'<main>' in document.content and document.content != markdown.content
            assert 'sandbox' not in document.headers['content-security-policy']
            assert document.headers['x-content-type-options'] == 'nosniff'
            assert 'sandbox' in markdown.headers['content-security-policy']
        favicon = await http.get('/favicon.png')
        assert favicon.status_code == 200 and favicon.content.startswith(b'\x89PNG\r\n\x1a\n')
        head = await http.head('/favicon.png')
        assert head.status_code == 200 and head.content == b''
        assert head.headers['content-length'] == str(len(favicon.content))
        assert (await http.post('/favicon.png')).status_code == 405
        hosted = await http.get('/@root/web/index.html')
        assert hosted.status_code == 200 and b'<svg' in hosted.content
        assert '<html lang="en">' in hosted.text
        assert 'a conversation begins.' in hosted.text
        assert not re.search(r'[\u3400-\u9fff]', hosted.text)
        csp = hosted.headers['content-security-policy']
        assert "style-src 'unsafe-inline'" in csp
        assert 'font-src data:' in csp and 'img-src data:' in csp
        assert 'sandbox allow-scripts' in csp
        assert "script-src 'sha256-" in csp and "connect-src 'self'" in csp
        assert 'allow-same-origin' in csp
        assert 'data:font/woff2;base64,' in hosted.text
        assert (
            '__SANS_FONT__' not in hosted.text
            and '__SANS_BOLD_FONT__' not in hosted.text
            and '<script>' in hosted.text
        )


@pytest.mark.asyncio
async def test_home_counts_dates_recent_posts_and_current_public_access(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'home-author')
    _, hidden_uid, _ = await register(app, 'hidden-profile')
    async with app.metadata.transaction(write=True) as tx:
        user = await tx.resource(hidden_uid)
        await tx.replace(replace(user, mode=0o700, generation=user.generation + 1), user.generation)

    # Taipei midnight is 16:00 UTC on the previous calendar day.
    midnight = NOW.replace(hour=16) - timedelta(days=1)
    cases = [
        ('old', midnight - timedelta(microseconds=1), 0o644, 'active'),
        ('at-midnight', midnight, 0o644, 'active'),
        *[(f'recent-{i}', midnight + timedelta(minutes=i), 0o644, 'active') for i in range(1, 6)],
        ('secret', NOW, 0o600, 'active'),
        ('archived', NOW, 0o644, 'archived'),
    ]
    paths = {}
    for name, created_at, mode, state in cases:
        result = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'name': name, 'body': name},
            key=key,
            subject=uid,
            certs=(cert,),
        )
        assert result.status == 'ok', result.error
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(result.resources[0].id)
            # Backdate fixture data directly; ordinary writes preserve creation facts.
            resource = replace(resource, created_at=created_at)
            tx.execute(
                'UPDATE resources SET created_at=?,body=? WHERE id=?',
                (wire(created_at), canonical(resource).decode(), resource.id),
                write=True,
            )
            await tx.replace(
                replace(
                    resource,
                    created_at=created_at,
                    mode=mode,
                    state=state,
                    generation=resource.generation + 1,
                ),
                resource.generation,
            )
            paths[name] = '/*' + resource.id.split('_')[-1]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/')
        assert response.status_code == 200, response.text
        assert 'Total public posts: 7' in response.text
        assert 'Posts today: 6' in response.text
        assert 'Public users: 1' in response.text
        assert '| [main](/main) | General discussion | 7 |' in response.text
        assert '| [intro](/intro) | Introductions | 0 |' in response.text
        assert 'Today: 2026-09-27 (Asia/Taipei)' in response.text
        latest = response.text.split('## Latest posts')[1]
        assert all(paths[f'recent-{i}'] in latest for i in range(1, 6))
        assert paths['at-midnight'] not in latest and paths['old'] not in latest
        assert paths['secret'] not in response.text and paths['archived'] not in response.text
        assert latest.index('recent-5') < latest.index('recent-4') < latest.index('recent-1')
        assert (await http.get(paths['recent-5'])).status_code == 200
        head = await http.head('/')
        assert head.content == b''
        assert head.headers['content-length'] == str(len(response.content))

        # Recompute on every visit; an ancestor becoming private hides its posts too.
        async with app.metadata.transaction(write=True) as tx:
            topic = await tx.resource(await tx.resolve('/main'))
            await tx.replace(
                replace(topic, mode=0o700, generation=topic.generation + 1), topic.generation
            )
        hidden = await http.get('/')
        assert hidden.status_code == 200
        assert 'Total public posts: 0' in hidden.text
        assert 'Posts today: 0' in hidden.text
        assert 'No public posts yet.' in hidden.text
        assert '| [main](/main)' not in hidden.text


def test_home_escapes_post_names_and_paths():
    from msg.transports.http_routes import home_markdown

    page = home_markdown({
        'posts': 1,
        'posts_today': 1,
        'users': 1,
        'date': '2026-09-27',
        'timezone': 'Asia/Taipei',
        'latest': [
            {'name': '[click]<script>&copy;', 'path': '/main/a (b).md', 'created_at': 'now'}
        ],
    }).decode()
    assert r'\[click\]\<script\>' in page
    assert r'\&copy;' in page
    assert '(/main/a%20%28b%29.md)' in page


def test_home_statistics_failure_keeps_rules_and_does_not_report_false_counts():
    from msg.transports.http_routes import home_markdown

    page = home_markdown().decode()
    assert 'temporarily unavailable' in page
    assert '[Platform rules](/_rules)' in page
    assert '[WebSub / RSS](/rss.xml)' in page
    assert 'Total public posts: 0' not in page
    assert 'No public posts yet.' not in page


@pytest.mark.parametrize(
    ('name', 'body', 'title', 'excerpt'),
    [
        (
            'p_' + 'a' * 32 + '.md',
            '# Real **title**\n\nA [useful](https://example.org) preview.',
            'Real title',
            'A useful preview.',
        ),
        (
            'p_' + 'a' * 32 + '.md',
            'A post without a heading.\n\nMore context.',
            'A post without a heading.',
            'More context.',
        ),
        ('p_' + 'a' * 32 + '.md', '', 'Untitled post', ''),
        (
            'Named post.md',
            '---\nsecret: metadata\n---\n```md\n# Code\n```\n# Actual title\nText',
            'Actual title',
            'Text',
        ),
    ],
)
def test_post_previews(name, body, title, excerpt):
    from msg.core.post_preview import post_preview

    assert post_preview(name, body) == {'title': title, 'excerpt': excerpt}


def test_post_preview_limits_and_home_escaping():
    from msg.core.post_preview import post_preview
    from msg.transports.http_routes import home_markdown

    preview = post_preview('generated.md', '# ' + '标' * 120 + '\n' + '文' * 240)
    assert len(preview['title']) == 96 and preview['title'].endswith('…')
    assert len(preview['excerpt']) == 180 and preview['excerpt'].endswith('…')
    page = home_markdown({
        'posts': 1,
        'posts_today': 1,
        'users': 1,
        'date': '2026-10-01',
        'timezone': 'Asia/Taipei',
        'latest': [
            {
                'name': 'internal.md',
                'title': '[title]<script>',
                'excerpt': '[preview](evil) &copy;\n# injected',
                'path': '/*abc',
                'created_at': '2025-10-01T14:29:39.770001+08:00',
            }
        ],
    }).decode()
    assert r'\[title\]\<script\>' in page
    assert r'\[preview\](evil) \&copy; # injected' in page
    assert '2025-10-01 14:29' in page and '770001' not in page


@pytest.mark.asyncio
async def test_home_preview_and_channel_counts_include_public_replies(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'preview-author')
    parent = await call(
        app,
        'content.post_create',
        {
            'parent': '/main',
            'body': '# Human title\n\nReadable preview with [link](https://example.org).',
        },
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert parent.status == 'ok', parent.error
    rid = parent.resources[0].id
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': rid}, 'body': '# A reply\n\nReply preview.'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert reply.status == 'ok', reply.error
    secret = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# Private title\n\nPrivate preview.'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert secret.status == 'ok', secret.error
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(secret.resources[0].id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = (await http.get('/')).text
        assert '[Human title](/*' in page and '[A reply](/*' in page
        assert 'Readable preview with link.' in page
        assert 'Private title' not in page and 'Private preview' not in page
        assert '| [main](/main) | General discussion | 2 |' in page
        latest = page.split('## Latest posts')[1].split('## Channels')[0]
        assert '.md]' not in latest and '+08:00' not in latest
        assert '09-27 08:00' in latest


def test_game_script_permission_is_pinned_to_exact_bundled_root_page():
    from msg.bootstrap import ROOT_WEB_SAMPLE
    from msg.core.codec import digest
    from msg.extensions.hosting import HOSTED_HEADERS, hosted_headers

    released = digest(ROOT_WEB_SAMPLE)
    assert (
        'allow-scripts'
        in hosted_headers('w_root_web', 'index.html', released)['Content-Security-Policy']
    )
    for site, path, content in (
        ('another_site', 'index.html', released),
        ('w_root_web', 'other.html', released),
        ('w_root_web', 'index.html', digest(ROOT_WEB_SAMPLE + b'<script>changed()</script>')),
    ):
        assert hosted_headers(site, path, content) == HOSTED_HEADERS


@pytest.mark.asyncio
async def test_home_summary_uses_existing_read_authority_and_remains_public(installed):
    app, _ = installed
    assert all(operation.name != 'discovery.home' for operation in app.registry.operations())
    key, uid, cert = await register(app, 'home-summary-reader')
    anonymous = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    assert anonymous.status == 'ok' and 'posts' in anonymous.data
    authenticated = await call(
        app,
        'discovery.read_query',
        {'home_summary': True},
        key=key,
        subject=uid,
        certs=(cert,),
        contract_version=4,
    )
    assert authenticated.error.code == 'credential_ceiling'
    mixed = await call(
        app, 'discovery.read_query', {'home_summary': True, 'parent': '/main'}, contract_version=4
    )
    assert mixed.error.code == 'schema_validation'
    for args in ({}, {'home_summary': False}):
        invalid = await call(app, 'discovery.read_query', args, contract_version=4)
        assert invalid.error.code == 'schema_validation'
    legacy = await call(app, 'discovery.read_query', {'home_summary': True})
    assert legacy.error.code == 'schema_validation'
    dictionary = build_dictionary(app.registry)
    direct_path = next(
        row['example']
        for row in dictionary.document['operations']
        if row['name'] == 'discovery.read_query' and row['version'] == 4
    )
    wrong_version = request_for(
        'discovery.read_query', {'home_summary': True}, app.settings.service_url
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        direct = await http.get(direct_path)
        assert direct.status_code == 200 and 'posts' in direct.json()['data']
        mismatched = await http.get(
            direct_path, headers={'X-Msg-Request': b64(canonical(wrong_version))}
        )
        assert mismatched.status_code == 400
        assert mismatched.json()['error']['code'] == 'representation_mismatch'
