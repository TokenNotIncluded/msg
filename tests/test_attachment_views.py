"""Attachment previews keep source bytes and active documents outside the page."""

from html.parser import HTMLParser

import pytest

from msg.transports.attachment_views import attachment_markdown, attachments_html
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html

RAW = '/_id/r_media/revisions/v_published/raw'


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def attachment(**changes):
    return {
        'ref': {'id': 'r_media', 'revision': 'v_published'},
        'available': True,
        'name': 'Published attachment',
        'media_type': 'application/octet-stream',
        'size': 2048,
        'digest': 'sha256:test',
        'kind': 'file',
        'raw_url': RAW,
        **changes,
    }


def resource(*items, **changes):
    return {'type': 'post', 'attachments': list(items), **changes}


@pytest.mark.parametrize(
    ('kind', 'media', 'tag'),
    [
        ('image', 'image/png', 'img'),
        ('video', 'video/mp4', 'video'),
        ('audio', 'audio/mpeg', 'audio'),
        ('text', 'text/plain', 'details'),
        ('file', 'application/zip', None),
    ],
)
def test_five_media_classes_have_explicit_read_and_native_controls(kind, media, tag):
    html = attachments_html(resource(attachment(kind=kind, media_type=media, preview='Short text')))
    parsed = Elements(html)
    download = parsed.find('a')[0]
    assert download['href'] == RAW and download['download'] == 'Published attachment'
    assert '2,048 bytes' in html
    if tag:
        element = parsed.find(tag)[0]
        if tag in {'video', 'audio'}:
            assert element['src'] == RAW
            assert 'controls' in element and element['preload'] == 'none'
            assert 'autoplay' not in element
        elif tag == 'img':
            assert element['src'] == RAW and element['loading'] == 'lazy'
            assert element['alt'] == 'Published attachment'
        else:
            assert 'open' not in element
            assert parsed.find('a')[1]['href'] == RAW
            assert 'Short text' in html
    else:
        assert not any(parsed.find(name) for name in ('img', 'video', 'audio', 'iframe', 'object'))


@pytest.mark.parametrize('media', ['image/gif', 'image/jpeg', 'image/webp', 'image/avif'])
def test_passive_image_formats_can_be_previewed(media):
    assert Elements(attachments_html(resource(attachment(kind='image', media_type=media)))).find(
        'img'
    )


@pytest.mark.parametrize('media', ['image/svg+xml', 'text/html', 'application/xhtml+xml'])
def test_active_documents_remain_download_only_even_with_a_mislabeled_kind(media):
    html = attachments_html(resource(attachment(kind='image', media_type=media)))
    parsed = Elements(html)
    assert parsed.find('a')[0]['download'] == 'Published attachment'
    assert not any(parsed.find(tag) for tag in ('img', 'iframe', 'object', 'embed', 'script'))


@pytest.mark.parametrize(
    'source',
    [
        'https://outside.example/raw',
        '//outside.example/raw',
        'javascript:alert(1)',
        'data:image/png;base64,AAAA',
        'blob:https://msg.lmm.best/source',
        RAW + '?next=outside',
        RAW + '#fragment',
        RAW.replace('v_published', 'v_new'),
        RAW.replace('r_media', 'r_other'),
        '/_id/r_media/raw',
        '/_id/r_media/revisions/../raw',
        '/_id/r_media/revisions/v_published%2Fother/raw',
    ],
)
def test_only_exact_published_same_origin_raw_url_can_reach_an_attribute(source):
    html = attachments_html(
        resource(attachment(kind='image', media_type='image/png', raw_url=source))
    )
    parsed = Elements(html)
    assert 'Unavailable attachment' in html
    assert not parsed.find('a') and not parsed.find('img')


@pytest.mark.parametrize(
    'ref',
    [
        None,
        {},
        {'id': 'r_media'},
        {'id': '//evil', 'revision': 'v_published'},
        {'id': 'r_media', 'revision': 'v_published/elsewhere'},
        {'id': 'r_media', 'revision': None},
    ],
)
def test_floating_or_malformed_reference_has_no_source(ref):
    html = attachments_html(resource(attachment(ref=ref)))
    assert not Elements(html).find('a')


def test_names_summary_and_preview_are_literal_not_attributes_or_markup():
    name = '报告 " onerror="alert(1) <img> & 📎'
    html = attachments_html(
        resource(
            attachment(
                name=name,
                kind='text',
                media_type='text/markdown',
                summary='<script>summary()</script>',
                preview='<svg onload="run()">preview</svg>',
            )
        )
    )
    parsed = Elements(html)
    assert parsed.find('a')[0]['download'] == name
    assert '<script>' not in html and '<svg' not in html and '<img>' not in html
    assert not any('onerror' in attrs or 'onload' in attrs for _, attrs in parsed.tags)
    assert '&lt;script&gt;summary()&lt;/script&gt;' in html
    assert '&lt;svg onload=&quot;run()&quot;&gt;preview&lt;/svg&gt;' in html


def test_unavailable_attachment_does_not_publish_stale_descriptor_details():
    html = attachments_html(
        resource(
            attachment(
                available=False,
                name='PRIVATE NAME',
                summary='PRIVATE SUMMARY',
                preview='PRIVATE CONTENT',
            )
        )
    )
    assert 'Unavailable attachment' in html and 'PRIVATE' not in html
    assert RAW not in html and not Elements(html).find('a')


def test_text_full_payload_is_not_embedded_or_copied_into_document_source():
    item = attachment(
        kind='text',
        media_type='text/plain',
        preview='p' * 400,
        summary='s' * 500,
        content='FULL_PRIVATE_PAYLOAD' * 1000,
    )
    html = document_html('Short post body', resource=resource(item)).decode()
    assert 'FULL_PRIVATE_PAYLOAD' not in html
    assert 'p' * 180 in html and 'p' * 181 not in html
    assert 's' * 280 in html and 's' * 281 not in html
    assert html.count('<section class="attachments"') == 1
    assert html.index('Short post body</p>') < html.index('<section class="attachments"')
    source = html.split('<textarea id="msg-document-source"', 1)[1].split('</textarea>', 1)[0]
    assert 'Short post body' in source and 'p' * 180 not in source
    assert (
        'attachments'
        not in document_html('Other resource', resource=resource(item, type='file')).decode()
    )


def test_many_attachments_are_bounded_and_do_not_inline_source_content():
    html = attachments_html(resource(*(attachment(name=f'File {i}') for i in range(35))))
    assert len(Elements(html).find('li')) == 32
    assert 'File 31' in html and 'File 32' not in html
    assert 'Showing the first 32 attachments.' in html
    assert attachments_html(resource()) == ''
    assert attachments_html({'type': 'post', 'attachments': None}) == ''


def test_browser_document_csp_allows_only_same_origin_media():
    policy = HOME_BROWSER_HEADERS['Content-Security-Policy']
    media = next(part.strip() for part in policy.split(';') if part.strip().startswith('media-src'))
    assert media == "media-src 'self'"


def test_markdown_metadata_has_explicit_published_source_but_never_source_body():
    item = attachment(
        kind='text', media_type='text/plain', preview='p' * 400, content='FULL_SOURCE_BYTES' * 1000
    )
    markdown = attachment_markdown(resource(item))
    assert '](' + RAW + ')' in markdown
    assert r'r\_media\@v\_published' in markdown
    assert r'sha256\:test' in markdown
    assert '2,048 bytes' in markdown
    assert 'p' * 180 in markdown and 'p' * 181 not in markdown
    assert 'FULL_SOURCE_BYTES' not in markdown
    assert attachment_markdown(resource()) == ''
    assert attachment_markdown(resource(item, type='file')) == ''


def test_markdown_fields_cannot_add_links_headings_html_or_new_lines():
    from markdown_it import MarkdownIt

    markdown = attachment_markdown(
        resource(
            attachment(
                name='[bad](javascript:run())\n# forged <img>',
                media_type='text/plain\n# forged',
                summary='[outside](https://outside.example) <script>',
            )
        )
    )
    html = MarkdownIt('commonmark', {'html': False}).render(markdown)
    parsed = Elements(html)
    assert parsed.find('a') == [{'href': RAW}]
    assert not parsed.find('img') and not parsed.find('script')
    assert len(parsed.find('h2')) == 1 and not parsed.find('h1')
    assert '&lt;script&gt;' in html


def test_markdown_unavailable_and_item_limit_keep_links_and_metadata_private():
    item = attachment(available=False, name='PRIVATE NAME', summary='PRIVATE SUMMARY')
    markdown = attachment_markdown(resource(item))
    assert 'Unavailable attachment' in markdown
    assert 'PRIVATE' not in markdown and RAW not in markdown
    markdown = attachment_markdown(resource(*(attachment(name=f'File {i}') for i in range(35))))
    assert 'File 31' in markdown and 'File 32' not in markdown
    assert 'Showing the first 32 attachments.' in markdown
