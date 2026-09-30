"""The homepage is Markdown even for browsers; hosted files keep their own media type."""

import re
from dataclasses import replace
from datetime import timedelta
from importlib.resources import files

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import canonical, wire
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
        assert browser.headers['content-type'].startswith('text/plain')
        assert b'<html' not in browser.content and b'msg.lmm.best' in browser.content
        assert b'<script' not in browser.content
        assert 'sandbox' in browser.headers['content-security-policy']
        agent = await http.get('/', headers={'Accept': 'text/markdown'})
        assert agent.status_code == 200
        assert agent.headers['content-type'].startswith('text/markdown')
        assert '/AGENTS.md' in agent.text
        assert 'Open-source instant messaging built for agents.' in agent.text
        assert '[Platform rules](/_rules)' in agent.text
        assert '[Source code](https://github.com/TokenNotIncluded/msg.lmm.best)' in agent.text
        assert 'Total public posts: 0' in agent.text
        assert 'Posts today: 0' in agent.text
        assert 'Public users: 0' in agent.text
        assert 'No public posts yet.' in agent.text
        assert 'Asia/Taipei' in agent.text
        assert browser.headers['cache-control'] == 'no-store'
        assert 'sandbox' in agent.headers['content-security-policy']
        assert browser.content == agent.content
        assert not re.search(r'[\u3400-\u9fff]', browser.text)
        browser_head = await http.head('/', headers={'Accept': 'text/html'})
        assert browser_head.content == b''
        assert browser_head.headers['content-type'].startswith('text/plain')
        assert browser_head.headers['content-length'] == str(len(browser.content))
        assert (await http.post('/', content=b'overwrite')).status_code == 405
        for path in ('/AGENTS.md', '/main', '/_rules'):
            document = await http.get(path, headers={'Accept': 'text/html'})
            assert document.status_code == 200
            assert document.headers['content-type'].startswith('text/plain')
            markdown = await http.get(path, headers={'Accept': 'text/markdown'})
            assert markdown.headers['content-type'].startswith('text/markdown')
            assert document.content == markdown.content
            assert 'sandbox' in document.headers['content-security-policy']
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
        assert 'Your agents.<br>In the loop.' in hosted.text
        assert not re.search(r'[\u3400-\u9fff]', hosted.text)
        csp = hosted.headers['content-security-policy']
        assert "style-src 'unsafe-inline'" in csp
        assert 'font-src data:' in csp and 'img-src data:' in csp
        assert 'sandbox allow-scripts' in csp
        assert "script-src 'sha256-" in csp and "connect-src 'none'" in csp
        assert 'allow-same-origin' not in csp
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
            paths[name] = await tx.path(resource.id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/')
        assert response.status_code == 200, response.text
        assert 'Total public posts: 7' in response.text
        assert 'Posts today: 6' in response.text
        assert 'Public users: 1' in response.text
        assert 'Today: 2026-09-27 (Asia/Taipei)' in response.text
        latest = response.text.split('## Latest posts')[1].split('## Before posting')[0]
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
    assert '[topic rules](/_rules/topics)' in page
    assert 'Total public posts: 0' not in page
    assert 'No public posts yet.' not in page


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
