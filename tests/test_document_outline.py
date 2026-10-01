from base64 import b64encode
from hashlib import sha256

from msg.transports.document_outline import HASH, SCRIPT, outline_html
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html


def test_outline_preserves_ids_and_handles_duplicate_and_hostile_titles():
    body, nav = outline_html(
        '<h1 id="section-guide">Guide</h1><h2>Guide</h2>'
        '<h3>Guide</h3><h2>&lt;script&gt;&amp;你好</h2>'
    )
    assert 'id="section-guide-2"' in body
    assert 'id="section-guide-3"' in body
    assert 'href="#section-guide"' in nav
    assert '&lt;script&gt;&amp;你好' in nav
    assert '<script>' not in nav
    assert '--depth:2' in nav


def test_outline_rendering_and_csp():
    html = document_html('# Guide\n\n## Install\n\n### Linux\n\n## Use').decode()
    assert 'class="page-document has-outline"' in html
    assert 'href="#section-install"' in html
    assert 'id="section-linux"' in html
    assert SCRIPT in html
    assert HASH == b64encode(sha256(SCRIPT.encode()).digest()).decode()
    assert f"'sha256-{HASH}'" in HOME_BROWSER_HEADERS['Content-Security-Policy']
    assert 'document-outline' not in document_html('# One heading').decode()
