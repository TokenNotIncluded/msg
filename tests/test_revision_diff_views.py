"""Exact diff counters and page boundaries, with compatibility and escaped HTML."""

import difflib

import httpx
from test_code_views import source_from_html
from test_service import call, register

from msg.core.revision_diff import diff_rows
from msg.transports.http import create_app
from msg.transports.revision_diff import revision_diff_content


def test_diff_rows_track_multiple_hunks_insert_delete_and_context():
    raw = [
        '--- before\n',
        '+++ after\n',
        '@@ -2,2 +2,3 @@\n',
        ' same\n',
        '-old\n',
        '+new\n',
        '+extra\n',
        '@@ -20 +21 @@\n',
        '-gone',
        '+last',
    ]
    rows = list(diff_rows(raw))
    assert [(r['kind'], r['old_line'], r['new_line']) for r in rows] == [
        ('header', None, None),
        ('header', None, None),
        ('hunk', None, None),
        ('context', 2, 2),
        ('delete', 3, None),
        ('insert', None, 3),
        ('insert', None, 4),
        ('hunk', None, None),
        ('delete', 20, None),
        ('insert', None, 21),
    ]
    assert [row['text'] for row in rows] == raw


def test_diff_html_escapes_preserves_copy_and_mid_hunk_line_numbers():
    diff = '+<img src=x onerror="alert(1)">\n'
    value = {
        'from': {'id': 'r_' + '1' * 32, 'revision': 'v_' + '2' * 32},
        'to': {'id': 'r_' + '1' * 32, 'revision': 'v_' + '3' * 32},
        'diff': diff,
        'diff_lines': [{'kind': 'insert', 'old_line': None, 'new_line': 37, 'text': diff}],
        'next': '/_r/next?x=1&y=2',
    }
    html = revision_diff_content(value)
    assert '<img src=x' not in html and '&lt;img src=x' in html
    assert 'diff-insert' in html
    assert 'New line / 新行">37</span>' in html
    assert source_from_html(html) == diff
    assert 'href="/_r/next?x=1&amp;y=2"' in html
    assert '/history' in html and 'Read current version' in html


async def test_server_diff_pagination_keeps_raw_api_and_exact_source_rows(installed):
    app, _ = installed
    key, user, _ = await register(app, 'numbered-diff')
    old_source, new_source = 'keep\nold', 'keep\nnew'
    created = await call(
        app, 'content.post_create', {'parent': '/main', 'body': old_source}, key=key, subject=user
    )
    ref = created.resources[0]
    edited = await call(
        app,
        'content.post_edit',
        {'id': ref.id, 'expected_revision': ref.revision, 'body': new_source},
        key=key,
        subject=user,
        expected=((ref.id, created.data['generation']),),
    )
    assert edited.status == 'ok', edited.error
    raw = list(
        difflib.unified_diff(
            old_source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile='before',
            tofile='after',
        )
    )
    pages = []
    continuation = None
    for offset in range(len(raw)):
        result = await call(
            app,
            'discovery.diff_view',
            {'id': ref.id, 'known_revision': ref.revision, 'offset': offset, 'limit': 1},
        )
        assert result.status == 'ok', result.error
        row = result.data['diff_lines'][0]
        assert result.data['diff'] == row['text']
        assert result.data['from']['revision'] == ref.revision
        assert result.data['to']['revision'] == edited.resources[0].revision
        pages.append(row)
        if offset == 3:
            continuation = result.data['next']
    assert [(r['old_line'], r['new_line']) for r in pages[-3:]] == [(1, 1), (2, None), (None, 2)]
    assert pages[-2]['text'] == '-old' and pages[-1]['text'] == '+new'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        next_page = await http.get(continuation, headers={'Accept': 'text/html'})
        assert next_page.status_code == 200
        assert 'Old line / 旧行">2</span>' in next_page.text
        assert 'New line / 新行">2</span>' in next_page.text
        assert '-old+new' in source_from_html(next_page.text)
        raw_page = await http.get(continuation)
        assert raw_page.headers['content-type'].startswith('application/json')
        assert raw_page.json()['diff'] == '-old+new'
        json_preferred = await http.get(
            continuation, headers={'Accept': 'text/html;q=0,application/json'}
        )
        assert json_preferred.headers['content-type'].startswith('application/json')
        assert json_preferred.json() == raw_page.json()
        head = await http.head(continuation, headers={'Accept': 'text/html'})
        assert head.status_code == 200 and head.content == b''
        assert head.headers['content-type'].startswith('text/html')
