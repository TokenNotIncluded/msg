"""Small, inert previews of already-authorized, revision-bound attachments."""

import re
from collections.abc import Mapping
from html import escape
from string import punctuation

MAX_ATTACHMENTS = 32
_RESOURCE_ID = re.compile(r'r_[A-Za-z0-9_-]+\Z')
_REVISION_ID = re.compile(r'v_[A-Za-z0-9_-]+\Z')
_IMAGES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif'}
_VIDEOS = {'video/mp4', 'video/webm', 'video/ogg', 'video/mpeg', 'video/quicktime'}
_AUDIO = {
    'audio/mpeg',
    'audio/mp4',
    'audio/ogg',
    'audio/wav',
    'audio/x-wav',
    'audio/webm',
    'audio/flac',
}

ATTACHMENT_CSS = """
.attachments {
  margin-block: 2.5rem 0; padding: 1rem; min-width: 0;
  background: #111113; color: #f5f5f7; border: 1px solid #39393f;
  color-scheme: dark; font: 14px/1.6 var(--mono);
}
.attachments .attachment-heading {
  margin: 0 0 .75rem; font: 600 16px/1.5 var(--mono); letter-spacing: 0;
}
.attachments .attachment-list {
  list-style: none; margin: 0; padding: 0; max-block-size: 32rem;
  overflow: auto; scrollbar-color: #a5a5ae #111113; overscroll-behavior: contain;
}
.attachments .attachment-item { min-width: 0; margin: 0; padding-block: .75rem; }
.attachments .attachment-item + .attachment-item { border-block-start: 1px solid #39393f; }
.attachments .attachment-caption { display: flex; flex-wrap: wrap; align-items: center; gap: 0 1rem; }
.attachments .attachment-name { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere; }
.attachments .attachment-download {
  display: inline-flex; align-items: center; min-height: 44px;
  color: #94bfff; text-underline-offset: .22em;
}
.attachments .attachment-info, .attachments .attachment-unavailable {
  color: #a5a5ae; font-size: 14px; margin: 0 0 .5rem; overflow-wrap: anywhere;
}
.attachments .attachment-summary { margin: .25rem 0 .75rem; font-size: 16px; line-height: 1.65; }
.attachments .attachment-image, .attachments .attachment-video {
  display: block; width: 100%; max-width: 100%; height: 18rem;
  object-fit: contain; background: #111113; margin: .5rem 0;
}
.attachments .attachment-audio { display: block; width: 100%; max-width: 100%; margin-block: .5rem; }
.attachments .attachment-text { margin: 0; padding: 0; }
.attachments .attachment-text summary { min-height: 44px; padding-block: .5rem; }
.attachments .attachment-preview {
  margin: .5rem 0; white-space: pre-wrap; overflow-wrap: anywhere;
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.attachments :focus-visible { outline: 2px solid #94bfff; outline-offset: 3px; }
.attachments ::selection { background: #f5f5f7; color: #111113; }
.attachments .attachments-more { margin: .75rem 0 0; color: #a5a5ae; }
@media (max-width: 640px) {
  .attachments { padding: .75rem; }
  .attachments .attachment-image, .attachments .attachment-video { height: 14rem; }
}
@media (forced-colors: active) {
  .attachments { border-color: CanvasText; }
}
"""


def _raw_url(item):
    """Only the exact same-origin raw path bound to the supplied revision is usable."""
    ref = item.get('ref')
    if not isinstance(ref, Mapping) or item.get('available') is not True:
        return None
    resource, revision = ref.get('id'), ref.get('revision')
    if not isinstance(resource, str) or not _RESOURCE_ID.fullmatch(resource):
        return None
    if not isinstance(revision, str) or not _REVISION_ID.fullmatch(revision):
        return None
    expected = f'/_id/{resource}/revisions/{revision}/raw'
    return expected if item.get('raw_url') == expected else None


def _short(value, limit):
    return str(value or '')[:limit]


def _file_info(item):
    media = _short(item.get('media_type'), 160)
    size = item.get('size')
    suffix = f' · {size:,} bytes' if type(size) is int and size >= 0 else ''
    return escape(media + suffix)


def attachments_html(resource):
    """Consume descriptors only; never fetch bytes or serialize attachment content."""
    if not resource or resource.get('type') != 'post':
        return ''
    items = resource.get('attachments')
    if not isinstance(items, list) or not items:
        return ''
    cards = []
    for item in items[:MAX_ATTACHMENTS]:
        if not isinstance(item, Mapping):
            continue
        raw_url = _raw_url(item)
        if raw_url is None:
            cards.append(
                '<li class="attachment-item">'
                '<p class="attachment-unavailable">Unavailable attachment</p></li>'
            )
            continue
        name = _short(item.get('name'), 240) or 'Attachment'
        shown = escape(name)
        source = escape(raw_url, quote=True)
        media = _short(item.get('media_type'), 160).partition(';')[0].strip().lower()
        kind = item.get('kind') if isinstance(item.get('kind'), str) else 'file'
        cards.append(
            '<li class="attachment-item">'
            '<div class="attachment-caption">'
            f'<span class="attachment-name" dir="auto">{shown}</span>'
            f'<a class="attachment-download" href="{source}" '
            f'download="{escape(name, quote=True)}" aria-label="Download {escape(name, quote=True)}">'
            'Download</a></div>'
            f'<p class="attachment-info">{_file_info(item)}</p>'
        )
        if item.get('summary'):
            cards.append(
                '<p class="attachment-summary" dir="auto">'
                + escape(_short(item['summary'], 280))
                + '</p>'
            )
        if kind == 'image' and media in _IMAGES:
            cards.append(
                f'<img class="attachment-image" src="{source}" alt="{escape(name, quote=True)}" '
                'loading="lazy" decoding="async">'
            )
        elif kind in {'video', 'audio'} and media in (_VIDEOS if kind == 'video' else _AUDIO):
            cards.append(
                f'<{kind} class="attachment-{kind}" src="{source}" controls preload="none" '
                f'aria-label="{escape(name, quote=True)}"'
                + (' playsinline' if kind == 'video' else '')
                + f'>Download the attachment to {"watch" if kind == "video" else "listen"}.</{kind}>'
            )
        elif kind == 'text' and media in {'text/plain', 'text/markdown'}:
            preview = escape(_short(item.get('preview'), 180))
            cards.append(
                '<details class="attachment-text"><summary>Text preview</summary>'
                + (f'<p class="attachment-preview" dir="auto">{preview}</p>' if preview else '')
                + f'<a class="attachment-download" href="{source}">Read original</a></details>'
            )
        cards.append('</li>')
    if not cards:
        return ''
    more = (
        '<p class="attachments-more">Showing the first 32 attachments.</p>'
        if resource.get('attachments_more') or len(items) > MAX_ATTACHMENTS
        else ''
    )
    return (
        '<section class="attachments" aria-label="Attachments">'
        '<h2 class="attachment-heading">Attachments</h2>'
        '<ul class="attachment-list" tabindex="0" aria-label="Attachment list">'
        + ''.join(cards)
        + '</ul>'
        + more
        + '</section>'
    )


def _markdown_text(value):
    text = str(value).replace('\n', ' ').replace('\r', ' ')
    return re.sub('([' + re.escape(punctuation) + '])', r'\\\1', text)


def attachment_markdown(resource):
    """Explicit, bounded metadata for ordinary Markdown reads; no source-byte reads."""
    if not resource or resource.get('type') != 'post':
        return ''
    items = resource.get('attachments')
    if not isinstance(items, list) or not items:
        return ''
    lines = []
    for item in items[:MAX_ATTACHMENTS]:
        if not isinstance(item, Mapping):
            continue
        raw_url = _raw_url(item)
        if raw_url is None:
            lines.append('- Unavailable attachment')
            continue
        name = _markdown_text(_short(item.get('name'), 240) or 'Attachment')
        media = _markdown_text(_short(item.get('media_type'), 160))
        size = item.get('size')
        suffix = f' · {size:,} bytes' if type(size) is int and size >= 0 else ''
        ref = item['ref']
        source = _markdown_text(ref['id'] + '@' + ref['revision'])
        digest = _markdown_text(_short(item.get('digest'), 100))
        lines.extend([
            f'- [{name}]({raw_url}) — {media}{suffix}',
            '  Source: ' + source + (' · ' + digest if digest else ''),
        ])
        detail = (
            _short(item['summary'], 280)
            if item.get('summary')
            else _short(item.get('preview'), 180)
        )
        if detail:
            lines.append('  ' + _markdown_text(detail))
    if not lines:
        return ''
    if resource.get('attachments_more') or len(items) > MAX_ATTACHMENTS:
        lines.extend(['', 'Showing the first 32 attachments.'])
    return '\n\n## Attachments\n\n' + '\n'.join(lines) + '\n'
