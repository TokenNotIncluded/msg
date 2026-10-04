"""Readable thread rows use the authorized posts without changing API data."""

import json
from dataclasses import replace
from html import unescape
from urllib.parse import quote

import httpx
import pytest
from test_service import call, register

from msg.core.codec import canonical, wire
from msg.core.identifiers import hex_id
from msg.transports.http import create_app
from msg.transports.post_read_pages import thread_preview, thread_read_html


def test_thread_preview_uses_h1_and_the_first_plain_paragraph_only():
    item = {
        'name': '2026-10-02-ai-tech-brief.md',
        'content': 'PS5 **官宣** [AI 超分](https://example.invalid)。\n\n'
        '# AI 科技简报｜2026-10-02\n\n## Another heading\nDo not combine this paragraph.',
    }
    before = canonical(item)
    assert thread_preview(item) == {
        'title': 'AI 科技简报｜2026-10-02',
        'excerpt': 'PS5 官宣 AI 超分。',
    }
    assert canonical(item) == before


@pytest.mark.parametrize(
    'body',
    [
        'First paragraph.\n\n## A section, not a title',
        '---\nsecret: metadata\n---\n```md\n# A fake title\n```\nFirst paragraph.',
    ],
)
def test_missing_h1_uses_the_safe_filename_and_skips_metadata_and_code(body):
    assert thread_preview({'name': 'Readable brief.md', 'content': body}) == {
        'title': 'Readable brief',
        'excerpt': 'First paragraph.',
    }
    assert (
        thread_preview({'name': 'p_' + 'a' * 32 + '.md', 'content': ''})['title'] == 'Untitled post'
    )


def test_thread_html_escapes_rows_keeps_data_collapsed_and_uses_readable_post_links():
    item = {
        'id': 'r_' + 'a' * 32,
        'revision': 'v_' + 'b' * 32,
        'name': 'fallback.md',
        'path': '//outside.invalid',
        'stable_path': '/_r/r_' + 'a' * 32 + '/json',
        'content': '# Safe <img src=x onerror=alert(1)> title\n\n[Text](javascript:alert(1)) & **more**.',
    }
    data = {
        'root': item['id'],
        'items': [
            {'id': item['id'], 'revision': item['revision'], 'summary': '<script>unsafe</script>'}
        ],
    }
    page = thread_read_html(
        [item], data, path='/*' + 'a' * 32 + '/thread', raw_query='limit=1&preview=8'
    ).decode()
    article = page.split('<article>', 1)[1].split('</article>', 1)[0]
    assert 'href="/*' + 'a' * 32 + '"' in article and '/json' not in article
    assert '<img ' not in article and 'href="javascript:' not in article
    assert '[Text](javascript:alert(1))' in article
    assert 'Safe  title' in article or 'Safe title' in article
    assert 'Text' in article and '&amp;' in article
    raw = page.split('<pre id="thread-raw-data"><code>', 1)[1].split('</code>', 1)[0]
    assert json.loads(unescape(raw)) == data
    assert '<details><summary>原始数据 / Raw data</summary>' in page
    assert '<details open' not in page and '<script>unsafe</script>' not in page
    assert (
        'class="raw-link" href="/*' + 'a' * 32 + '/thread?limit=1&amp;preview=8&amp;format=raw"'
        in page
    )


@pytest.mark.asyncio
async def test_thread_browser_html_preserves_api_pagination_acl_and_read_only_state(installed):
    app, _ = installed
    key, subject, cert = await register(app, 'thread-display')

    async def write(operation, arguments):
        result = await call(app, operation, arguments, key=key, subject=subject, certs=(cert,))
        assert result.status == 'ok', wire(result)
        return result.resources[0]

    root = await write(
        'content.post_create',
        {
            'parent': '/main',
            'name': 'date-brief',
            'body': 'PS5 官宣 AI 超分。\n\n# AI 科技简报\n\nThe complete body remains here.',
        },
    )
    reply = await write(
        'discussion.reply', {'target': {'id': root.id}, 'body': '# 可读回复\n\nReply text.'}
    )
    secret = await write(
        'discussion.reply',
        {'target': {'id': root.id}, 'body': '# Hidden title\n\nHidden paragraph.'},
    )
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(secret.id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    full = await call(app, 'discussion.thread', {'id': root.id})
    post_path = wire(full.data)['items'][0]['path']
    before = canonical(full.data)
    async with app.metadata.transaction(write=False) as tx:
        events_before = tx.one('SELECT COUNT(*) FROM events')[0]
    short = '/*' + hex_id(root.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        api = await http.get(short + '/thread')
        html = await http.get(short + '/thread', headers={'Accept': 'text/html'})
        assert html.status_code == 200 and html.headers['content-type'].startswith('text/html')
        assert api.headers['content-type'].startswith('application/json')
        assert html.headers['etag'] != api.headers['etag']
        assert (
            html.headers['vary'] == 'Accept, Cookie' and 'no-store' in html.headers['cache-control']
        )
        assert "default-src 'none'" in html.headers['content-security-policy']
        assert html.text.count('<article>') == 2 and 'Hidden title' not in html.text
        assert 'href="' + quote(post_path, safe='/@*') + '"' in html.text
        assert '<p>PS5 官宣 AI 超分。</p>' in html.text and 'AI 科技简报</a>' in html.text
        raw = html.text.split('<pre id="thread-raw-data"><code>', 1)[1].split('</code>', 1)[0]
        assert json.loads(unescape(raw)) == api.json()
        for accept in (
            'application/json',
            'application/json,text/html;q=0.1',
            'text/html;q=0,application/json',
            'text/html;q=0',
        ):
            response = await http.get(short + '/thread', headers={'Accept': accept})
            assert response.json() == api.json()
        first = await http.get(short + '/thread?limit=1&preview=8', headers={'Accept': 'text/html'})
        raw = first.text.split('<pre id="thread-raw-data"><code>', 1)[1].split('</code>', 1)[0]
        next_path = json.loads(unescape(raw))['next']
        assert 'limit=1&amp;preview=8&amp;format=raw' in first.text
        raw_page = await http.get(
            short + '/thread?limit=1&preview=8&format=raw', headers={'Accept': 'text/html'}
        )
        assert raw_page.json() == json.loads(unescape(raw))
        rest = await http.get(next_path, headers={'Accept': 'text/html'})
        assert first.text.count('<article>') == 1 and rest.text.count('<article>') == 1
        assert '可读回复</a>' in rest.text and 'preview=8' in next_path
        head = await http.head(short + '/thread', headers={'Accept': 'text/html'})
        assert (
            head.status_code == 200
            and not head.content
            and head.headers['etag'] == html.headers['etag']
        )
        raw_response = await http.get(short + '/thread?format=raw', headers={'Accept': 'text/html'})
        assert raw_response.status_code == 200 and raw_response.json() == api.json()
        assert raw_response.headers['content-type'].startswith('text/plain')
        assert canonical((await call(app, 'discussion.thread', {'id': root.id})).data) == before
        async with app.metadata.transaction(write=False) as tx:
            assert tx.one('SELECT COUNT(*) FROM events')[0] == events_before
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(root.id)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        for method in (http.get, http.head):
            denied = await method(
                short + '/thread',
                headers={'Accept': 'text/html', 'If-None-Match': html.headers['etag']},
            )
            assert denied.status_code == 403 and 'etag' not in denied.headers
        assert hex_id(reply.id) in api.text and hex_id(secret.id) not in api.text
