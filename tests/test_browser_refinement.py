"""Browser details must not compromise rendering or the raw resource contract."""

from html.parser import HTMLParser

import pytest

from msg.transports.home_page import document_html, home_html, mailbox_html


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, value):
        self.text.append(value)


@pytest.mark.parametrize(
    'markdown', ['---\nunfinished metadata', '---\njust a separator\n\n# Body']
)
def test_unclosed_frontmatter_remains_visible_instead_of_crashing(markdown):
    html = document_html(markdown, resource={'type': 'post'}).decode()
    assert markdown.split('\n')[1] in html


def test_only_a_complete_frontmatter_envelope_is_removed():
    html = document_html(
        '---\ninternal: private\n---\n\n# Real body', resource={'type': 'post'}
    ).decode()
    assert '<h1>Real body</h1>' in html
    assert 'internal: private' not in html


def test_mailbox_labels_and_excerpts_are_literal_markdown_text():
    label = 'name](https://outside.invalid) [click'
    excerpt = '![image](https://outside.invalid/pixel) and **not markup**'
    html = mailbox_html(
        {
            'path': '/@reader/in',
            'items': [
                {
                    'sender_contact': {'name': label},
                    'path': '/main/1',
                    'preview': {'excerpt': excerpt},
                }
            ],
        },
        'in',
    ).decode()
    parsed = Elements(html)
    assert not any(a.get('href', '').startswith('https://outside.invalid') for _, a in parsed.tags)
    assert not any(tag == 'img' for tag, _ in parsed.tags)
    assert label in ''.join(parsed.text) and excerpt in ''.join(parsed.text)


def test_home_preferences_are_collapsed_and_content_is_keyboard_reachable():
    parsed = Elements(home_html().decode())
    assert any(
        tag == 'details' and a.get('class') == 'preferences' and 'open' not in a
        for tag, a in parsed.tags
    )
    assert any(
        tag == 'a' and a.get('class') == 'skip-link' and a.get('href') == '#content'
        for tag, a in parsed.tags
    )
    assert any(a.get('id') == 'content' and a.get('tabindex') == '-1' for _, a in parsed.tags)
    for name in ('msg-language', 'msg-accent', 'msg-theme'):
        assert sum(a.get('id') == name for _, a in parsed.tags) == 1


def test_home_counters_are_translated_without_invented_activity():
    html = home_html({
        'posts': 0,
        'posts_today': 0,
        'users': 0,
        'date': '2026-10-01',
        'timezone': 'Asia/Taipei',
        'latest': [],
    }).decode()
    parsed = Elements(html)
    markers = {a.get('data-i18n') for _, a in parsed.tags}
    assert {'public_posts', 'today', 'users', 'no_posts'} <= markers
    assert (
        'Statistics and latest posts are temporarily unavailable.' not in html.split('<script>')[0]
    )


def test_unavailable_activity_is_not_displayed_as_zero():
    html = home_html().decode().split('<script>')[0]
    assert 'Statistics and latest posts are temporarily unavailable.' in html
    assert '<strong>0</strong>' not in html


def test_shared_page_css_does_not_need_important_overrides():
    html = document_html('# Test').decode()
    css = html.split('<style>')[1].split('</style>')[0]
    assert '!important' not in css
    assert ':focus-visible' in css and 'prefers-reduced-motion' in css


@pytest.mark.parametrize(
    'excerpt', ['# Heading', '- item', '+ item', '1. item', '---', '> quote', '`code`']
)
def test_mailbox_preview_cannot_create_markdown_blocks(excerpt):
    html = mailbox_html(
        {
            'path': '/@reader/in',
            'items': [{'path': '/main/1', 'preview': {'excerpt': excerpt}}],
        },
        'in',
    ).decode()
    parsed = Elements(html)
    # The mailbox itself has exactly one heading and one message list.
    assert sum(tag == 'h1' for tag, _ in parsed.tags) == 1
    assert not any(tag in {'hr', 'blockquote', 'pre', 'ol', 'code'} for tag, _ in parsed.tags)
    assert excerpt in ''.join(parsed.text)


@pytest.mark.parametrize('render', [home_html, lambda **kw: document_html('# Hello', **kw)])
def test_current_account_group_links_survive_browser_refinement(render):
    html = render(
        account={'name': '@reader', 'groups': [{'name': '&admins', 'path': '/&admins'}]}
    ).decode()
    assert 'href="/&amp;admins"' in html and '&amp;admins' in html


@pytest.mark.parametrize(
    'value,expected',
    [
        ('2026-10-01T00:00:00', '2026-10-01 08:00'),
        ('2026-10-01T00:00:00Z', '2026-10-01 08:00'),
        ('9999-12-31T23:59:59Z', '9999-12-31T23:59:59Z'),
        ('invalid timestamp', 'invalid timestamp'),
    ],
)
def test_display_timestamps_are_deterministic_and_tolerate_bad_metadata(value, expected):
    from msg.transports.home_page import display_time

    assert display_time(value) == expected


def test_document_keeps_html_and_script_urls_inert():
    html = document_html(
        '# Title\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1))'
    ).decode()
    assert '<h1>Title</h1>' in html
    assert '<script>alert(1)</script>' not in html and 'href="javascript:' not in html
