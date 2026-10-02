"""Board descriptions remain literal content inside one Markdown table cell."""

from html import unescape

import pytest
from markdown_it import MarkdownIt

from msg.transports.http_routes import home_markdown


@pytest.mark.parametrize(
    'description',
    [
        'First line\n| [outside](https://outside.invalid) | forged | 999 | 1777 | identity |',
        '<script>alert(1)</script>\r\n<img src="https://outside.invalid/pixel">',
        '**bold**\t`code` | [link](javascript:alert(1)) ![image](https://outside.invalid/x)',
        r'An escaped pipe \| and literal &lt;script&gt; with a backslash \\',
    ],
)
def test_board_description_cannot_add_rows_links_or_html(description):
    page = home_markdown({
        'posts': 3,
        'posts_today': 1,
        'users': 2,
        'date': '2026-10-02',
        'timezone': 'Asia/Taipei',
        'latest': [],
        'channels': [
            {
                'name': 'main',
                'path': '/main',
                'about': description,
                'posts': 3,
                'mode': '1777',
                'posting': 'identity',
            }
        ],
    }).decode()
    section = page.split('## Channels', 1)[1].split('Users:', 1)[0]
    rows = [line for line in section.splitlines() if line.startswith('|')]
    assert len(rows) == 3  # Heading, separator, and the one actual channel.
    parser = MarkdownIt('commonmark', {'html': True}).enable('table')
    tokens = parser.parse(section)
    assert sum(token.type == 'td_open' for token in tokens) == 5
    assert sum(token.type == 'tr_open' for token in tokens) == 2
    inline = [token for token in tokens if token.type == 'inline']
    cells = inline[-5:]
    assert [token.content for token in cells[-3:]] == ['3', '[1777](/main/meta)', 'identity']
    assert not any(
        child.type
        in {'link_open', 'image', 'html_inline', 'html_block', 'strong_open', 'code_inline'}
        for child in cells[1].children
    )
    rendered = parser.render(section)
    assert 'href="/main"' in rendered and 'href="/main/meta"' in rendered
    assert 'href="https://outside.invalid' not in rendered
    assert 'href="javascript:' not in rendered
    assert '<script>' not in rendered and '<img ' not in rendered
    cell_html = rendered.split('<tbody>', 1)[1].split('<td>', 2)[2].split('</td>', 1)[0]
    assert unescape(cell_html) == ' '.join(description.split())


def test_channel_name_cannot_add_a_table_row():
    page = home_markdown({
        'posts': 0,
        'posts_today': 0,
        'users': 0,
        'date': '2026-10-02',
        'timezone': 'Asia/Taipei',
        'latest': [],
        'channels': [
            {
                'name': 'a\n| forged |',
                'path': '/canonical',
                'about': 'Description',
                'posts': 0,
                'mode': '0555',
                'posting': 'closed',
            }
        ],
    }).decode()
    section = page.split('## Channels', 1)[1].split('Users:', 1)[0]
    parser = MarkdownIt('commonmark', {'html': True}).enable('table')
    tokens = parser.parse(section)
    assert sum(token.type == 'td_open' for token in tokens) == 5
    assert sum(token.type == 'tr_open' for token in tokens) == 2
    assert 'href="/canonical"' in parser.render(section)
