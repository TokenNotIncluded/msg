"""Small, plain-text previews for public post listings."""

import re


def plain_text(value):
    value = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'<[^>]*>', '', value)
    value = re.sub(r'[`*_~]', '', value)
    return ' '.join(value.split())


def shorten(value, limit):
    return value if len(value) <= limit else value[: limit - 1].rstrip() + '…'


def post_preview(name, body):
    """Prefer a heading, then prose; never expose generated names as titles."""
    title = ''
    prose = []
    fence = None
    frontmatter = body.startswith(('---\n', '---\r\n'))
    for index, line in enumerate(body.splitlines()):
        line = line.strip()
        if frontmatter:
            if index and line in {'---', '...'}:
                frontmatter = False
            continue
        marker = re.match(r'(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence is not None or not line or re.fullmatch(r'[-=*_ ]{3,}', line):
            continue
        heading = re.match(r'#{1,6}\s+(.+?)\s*#*$', line)
        text = plain_text(heading[1] if heading else re.sub(r'^(?:>\s*|[-+]\s+)', '', line))
        if not text:
            continue
        if heading:
            if not title:
                title = text
        else:
            prose.append(text)
    if not title:
        if prose:
            title = prose.pop(0)
        elif re.fullmatch(r'p_[0-9a-f]{32}(?:\.md)?', name):
            title = 'Untitled post'
        else:
            title = plain_text(re.sub(r'\.md$', '', name)) or 'Untitled post'
    excerpt = ' '.join(text for text in prose if text != title)
    return {'title': shorten(title, 96), 'excerpt': shorten(excerpt, 180)}
