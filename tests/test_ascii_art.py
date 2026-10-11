"""Visual rendering must not expand machine reads or execute shared user art."""

import pytest
from test_browser_sessions import signed_browser as signed_browser

from msg.plugins.public_board import default, projection
from msg.transports.ascii_art import HASH, SCRIPT
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html
from msg.transports.public_board import html


def test_visual_bundle_is_pinned_and_scoped_to_home():
    assert 'sha256-' + HASH in HOME_BROWSER_HEADERS['Content-Security-Policy']
    assert SCRIPT not in document_html('# A plain post').decode()
    custom = projection(default())
    custom['svg'] = '<svg viewBox="0 0 960 300"><text>user content</text></svg>'
    page = html(custom)
    assert 'data-ascii-default="false"' in page
    # SVG bytes stay in the escaped editor; they never enter a live inline SVG.
    assert custom['svg'] not in page
    assert '&lt;text&gt;user content&lt;/text&gt;' in page


@pytest.mark.asyncio
async def test_machine_home_and_public_board_have_no_visual_payload(signed_browser):
    _, _, _, http = signed_browser
    browser = await http.get('/', headers={'Accept': 'text/html'})
    assert 'data-ascii-canvas' in browser.text and SCRIPT in browser.text
    machine = await http.get('/', headers={'Accept': 'text/markdown'})
    raw = await http.get('/?format=raw', headers={'Accept': 'text/html'})
    for response in (machine, raw):
        assert response.status_code == 200
        assert 'data-ascii' not in response.text and SCRIPT not in response.text
        assert 'Galaxy / 星系' not in response.text
    board = await http.get('/_public-board')
    assert set(board.json()) == set(projection(default()))
    assert 'ascii' not in board.json()


def test_home_art_has_ordered_copy_and_a_stable_viewport():
    page = html()
    assert page.index('class="signal-copy"') < page.index('class="signal-stage"')
    assert page.index('class="signal-meta"') < page.index('data-content')
    assert page.count('class="signal-viewport"') == 1
    assert 'data-ascii-canvas hidden aria-hidden="true"' in page
    for piece, label in (
        ('galaxy', 'Galaxy / 星系'),
        ('flow', 'Flow / 信号流'),
        ('aurora', 'Aurora / 极光'),
        ('community', 'Shared SVG / 公共作品'),
    ):
        assert f'data-ascii-piece="{piece}"' in page
        assert f'aria-label="{label}"' in page
    assert page.count('class="ascii-piece-name"') == 4
    assert page.count('class="ascii-piece-label"') == 4


def test_restyled_home_keeps_shared_text_and_svg_escaped():
    board = projection(default())
    board['text'] = '<img src=x onerror=alert(1)> 字符仍然是文本'
    board['svg'] = '<svg viewBox="0 0 960 300"><text>Shared &amp; public</text></svg>'
    page = html(board)
    assert board['text'] not in page
    assert '&lt;img src=x onerror=alert(1)&gt; 字符仍然是文本' in page
    assert board['svg'] not in page
    assert 'data-ascii-default="false"' in page
