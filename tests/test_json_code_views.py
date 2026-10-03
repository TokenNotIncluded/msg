"""人类 JSON 布局不改写字面量、复制原文或机器表示。"""

import re
from base64 import b64decode
from html.parser import HTMLParser

import pytest

from msg.core.codec import canonical
from msg.transports.code_views import markdown_renderer, render_code
from msg.transports.home_page import document_html
from msg.transports.http_routes import describe_resource
from msg.transports.json_layout import json_display


class DisplayText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lines = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == 'span':
            if ('class', 'code-text') in attrs:
                self.lines.append('')
                self.depth = 1
            elif self.depth:
                self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'span' and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.lines[-1] += data


def displayed(html):
    parser = DisplayText()
    parser.feed(html)
    return '\n'.join(parser.lines)


def copied(html):
    return b64decode(re.search(r'data-code-source="([^"]*)"', html)[1]).decode()


def test_compact_nested_json_is_readable_and_copy_remains_exact():
    source = '{"items":[{"title":"中文","flags":[true,null]}],"empty":{}}\r\n'
    html = render_code(source, 'json')
    assert displayed(html) == (
        '{\n'
        '  "items": [\n'
        '    {\n'
        '      "title": "中文",\n'
        '      "flags": [\n'
        '        true,\n'
        '        null\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "empty": {}\n'
        '}'
    )
    assert copied(html) == source
    assert html.count('class="code-line"') == 12
    assert 'class="code-number" aria-hidden="true">12</span>' in html


def test_json_layout_preserves_numeric_spelling_precision_and_duplicate_keys():
    source = (
        '{"n":1.2345678901234567890123456789,"exp":1e+10000,"neg":-0,'
        '"same":1,"same":2,"int":' + '9' * 5000 + '}'
    )
    html = render_code(source, 'json')
    assert displayed(html) == (
        '{\n  "n": 1.2345678901234567890123456789,\n  "exp": 1e+10000,\n'
        '  "neg": -0,\n  "same": 1,\n  "same": 2,\n  "int": ' + '9' * 5000 + '\n}'
    )
    assert copied(html) == source


def test_json_strings_escape_html_and_keep_escaped_punctuation():
    source = r'{"a": "</textarea><script>alert(1)</script>","b":"{[,:]} \n \" \\ \u4e2d"}'
    html = render_code(source, 'json')
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    assert displayed(html) == (
        '{\n  "a": "</textarea><script>alert(1)</script>",\n'
        r'  "b": "{[,:]} \n \" \\ \u4e2d"' + '\n}'
    )
    assert copied(html) == source


@pytest.mark.parametrize(
    'source',
    [
        '{"unfinished":',
        '{unquoted:1}',
        '{"trailing":1,}',
        '[1,]',
        '{"number":NaN}',
        '{"number":Infinity}',
        '{"number":-Infinity}',
        '{"number":01}',
        '{"number":+1}',
        '{"a":1} {"b":2}',
        '{"a": [1,2}',
        '{/* comment */"a":1}',
        '{"text":"literal\nnewline"}',
        '\ufeff{"bom":1}',
    ],
)
def test_invalid_or_incomplete_json_falls_back_to_original(source):
    assert json_display(source) == source
    html = render_code(source, 'json')
    assert displayed(html) == source
    assert copied(html) == source


@pytest.mark.parametrize('source', ['"scalar"', '1.00', 'true', 'null', '{}', '[]'])
def test_scalars_and_empty_containers_keep_their_literal_display(source):
    assert displayed(render_code(source, 'json')) == source


def test_depth_input_size_and_expanded_layout_are_bounded():
    allowed_depth = '[' * 64 + '0' + ']' * 64
    too_deep = '[' * 65 + '0' + ']' * 65
    too_big = '["' + 'x' * 200000 + '"]'
    expanded = '[' * 60 + ','.join(['0'] * 5000) + ']' * 60
    assert '\n' in json_display(allowed_depth)
    for source in (too_deep, too_big, expanded):
        assert json_display(source) == source


def test_input_and_expanded_json_limits_count_utf8_bytes():
    too_big = '{"x":"' + '😀' * 51000 + '"}'
    expansion_too_big = '{"x":"' + 'é' * 99996 + '"}'
    assert len(too_big) < 200000 < len(too_big.encode())
    assert len(expansion_too_big.encode()) == 200000
    for source in (too_big, expansion_too_big):
        assert json_display(source) == source


def test_only_json_fences_receive_json_layout():
    source = '{"a":1,"b":2}\n'
    for language in ('text', 'python', 'jsonc', ''):
        assert displayed(render_code(source, language)) == source.rstrip('\n')
    html = markdown_renderer().render('```json example\n' + source + '```\n')
    assert displayed(html) == '{\n  "a": 1,\n  "b": 2\n}'
    assert copied(html) == source


def test_structured_resource_html_expands_json_but_keeps_machine_markdown():
    content = {'items': [{'title': '可读 JSON', 'active': True}], 'root': 'unchanged'}
    data = {'type': 'file', 'name': 'sample.json', 'content': content}
    source = canonical(content).decode()
    markdown = describe_resource(data)
    assert markdown == '```json\n' + source + '\n```\n'
    html = document_html(markdown, resource=data).decode()
    assert '\n    {\n' in displayed(html)
    assert copied(html) == source + '\n'
    assert canonical(content).decode() == source
