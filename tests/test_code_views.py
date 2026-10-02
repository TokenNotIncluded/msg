"""Code surfaces must keep literal source separate from presentation."""

import re
from base64 import b64decode
from html import unescape

import pytest

from msg.transports.code_views import CODE_HASH, CODE_SCRIPT, markdown_renderer, render_code


def source_from_html(html):
    return b64decode(re.search(r'data-code-source="([^"]*)"', html)[1]).decode()


@pytest.mark.parametrize('language', ['python', 'javascript', 'not-installed', '<img src=x>'])
def test_literal_code_and_exact_copy_never_turn_into_html(language):
    source = 'if value < 3:\r\n\tprint("中文 </textarea><script>alert(1)</script> &")\r\n'
    html = render_code(source, language)
    assert '<script>alert(1)</script>' not in html and '<img src=x>' not in html
    assert '&lt;/textarea&gt;' in html
    assert source_from_html(html) == source
    assert html.count('class="code-line"') == 2
    assert 'class="code-number" aria-hidden="true">1</span>' in html
    assert 'class="code-number" aria-hidden="true">2</span>' in html
    if language == 'not-installed':
        assert 'plain text' in html and 'class="code-string"' not in html
    assert 'src="http' not in html


@pytest.mark.parametrize(
    'source,count',
    [
        ('', 1),
        ('\n', 1),
        ('a\n\n', 2),
        ('a\nb', 2),
        pytest.param('\ufeffprint(1)\n', 1, id='leading-bom'),
        pytest.param('\nprint(1)\n', 2, id='leading-blank'),
        pytest.param('print(1)\r\nprint(2)\r\n', 2, id='crlf'),
    ],
)
def test_blank_lines_and_missing_final_newline_are_counted(source, count):
    html = render_code(source, 'text')
    assert html.count('class="code-line"') == count
    assert source_from_html(html) == source


def test_fenced_markdown_retains_source_and_highlights_locally():
    source = 'def greet(name):\n    return "你好 <script>"\n'
    html = markdown_renderer().render('```python\n' + source + '```\n')
    assert source_from_html(html) == source
    assert 'class="code-kw"' in html and 'class="code-fn"' in html
    assert '&lt;script&gt;' in html and '<script>' not in html
    assert 'data-copy-code' in html
    assert CODE_HASH and 'navigator.clipboard.writeText(original)' in CODE_SCRIPT
    literal = markdown_renderer().render('<script>alert(1)</script>\n[bad](javascript:alert(1))')
    assert '<script>' not in literal and 'href="javascript:' not in literal


def test_highlighting_does_not_drop_source_characters():
    source = '\t# comment\nprint("é &"); return 1\n'
    html = render_code(source, 'python')
    text = ''.join(re.findall(r'<span class="code-text">(.*?)</span></span>', html))
    assert '\t' in unescape(text) and 'é' in unescape(text)
    assert source_from_html(html) == source
