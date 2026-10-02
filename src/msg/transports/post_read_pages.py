"""Small human views of already-authorized post branches and claims."""

from html import escape
from urllib.parse import quote, urlencode

from msg.core.codec import canonical
from msg.core.identifiers import hex_id
from msg.core.post_preview import plain_text, post_preview, shorten
from msg.transports.home_page import display_time, markdown_text


def thread_preview(item):
    """Use authored H1 and a plain first paragraph without changing the post."""
    from markdown_it import MarkdownIt

    content = item.get('content', '')
    if isinstance(content, dict) and 'template_id' in content and 'values' in content:
        from msg.core.template_dsl import render_values

        content = render_values(content['values'])
    elif not isinstance(content, str):
        content = ''

    def tokens(text):
        lines = text[:8192].splitlines()
        if lines and lines[0] == '---':
            for index, line in enumerate(lines[1:], 1):
                if line in {'---', '...'}:
                    lines = lines[index + 1 :]
                    break
            else:
                lines = []
        return MarkdownIt('commonmark', {'html': False}).parse('\n'.join(lines))

    body = tokens(content)
    title = post_preview(item.get('name', ''), '')['title']
    for index, token in enumerate(body[:-1]):
        if token.type == 'heading_open' and token.tag == 'h1':
            title = shorten(plain_text(body[index + 1].content), 96) or title
            break
    summary = item.get('summary')
    source = tokens(summary) if isinstance(summary, str) and summary else body
    excerpt = ''
    for index, token in enumerate(source[:-1]):
        if token.type == 'paragraph_open':
            excerpt = shorten(plain_text(source[index + 1].content), 180)
            break
    return {'title': title, 'excerpt': excerpt}


def thread_read_html(items, data, *, path, raw_query='', account=None, service_url=None):
    """Render only items the thread operation has already authorized."""
    from msg.transports.home_page import document_html

    root = '/*' + quote(hex_id(data['root']), safe='')
    parts = ['<h1>Thread / 讨论串</h1>', f'<p><a href="{root}">返回帖子 / Back to post</a></p>']
    for item in items:
        preview = thread_preview(item)
        target = item.get('path', '')
        if not isinstance(target, str) or not target.startswith('/') or target.startswith('//'):
            target = '/*' + hex_id(item['id'])
        href = escape(quote(target, safe='/@*'), quote=True)
        parts.append(
            '<article><h2><a href="' + href + '">' + escape(preview['title']) + '</a></h2>'
        )
        if preview['excerpt']:
            parts.append('<p>' + escape(preview['excerpt']) + '</p>')
        parts.append('</article>')
    if not items:
        parts.append('<p>暂无可读帖子。 / No readable posts.</p>')
    if data.get('next'):
        href = escape(quote(data['next'], safe='/@*?=&'), quote=True)
        parts.append(f'<p><a href="{href}">更多 / More</a></p>')
    parts.append(
        '<details><summary>原始数据 / Raw data</summary><pre id="thread-raw-data"><code>'
        + escape(canonical(data).decode())
        + '</code></pre></details>'
    )
    return document_html(
        '',
        title='Thread / 讨论串',
        account=account,
        raw_path=path,
        raw_query=raw_query,
        body_html=''.join(parts),
        service_url=service_url,
    )


def post_read_markdown(path, query, data=None, *, public_fallback=False, error=None):
    proofs = path == '/_post/proofs'
    title = 'Proof records / 证明记录' if proofs else 'Thread branches / 帖子分叉'
    lines = [
        '# ' + title,
        '',
        '[Back to post / 返回帖子](/_id/' + quote(query['id'], safe='') + ')',
        '',
    ]
    if error:
        if error == 'credential_ceiling':
            message = 'Current authorization does not include this read. / 当前授权未包含此项读取。'
        elif error in {'permission_denied', 'certificate_gate'}:
            message = 'You cannot read these records. / 你没有读取这些记录的权限。'
        elif error in {'not_found', 'revision_not_found', 'resource_purged'}:
            message = 'These records are unavailable. / 找不到可读取的记录。'
        else:
            message = 'Could not load these records. / 暂时无法加载这些记录。'
        lines.extend([message, '', 'Error / 错误：`' + markdown_text(error) + '`'])
        return '\n'.join(lines)
    if public_fallback:
        lines.extend(['Showing public records only. / 当前仅显示公开可读的记录。', ''])
    if proofs:
        lines.extend([
            'These are participant claims about a specific revision. / 这些是参与者对此版本的声明。',
            '',
        ])
        for item in data['items']:
            lines.extend(['## ' + markdown_text(item['kind']), '', display_time(item['time']), ''])
            if item.get('note'):
                lines.extend([markdown_text(item['note']), ''])
        cursor_key = 'cursor'
        empty = 'No claims for this revision yet. / 此版本还没有证明声明。'
    else:
        for item in data['items']:
            lines.extend([
                '- [' + markdown_text(item['name']) + '](/_id/' + quote(item['id'], safe='') + ')'
            ])
        cursor_key = 'after'
        empty = 'No readable branches yet. / 暂无可读的分叉。'
    if not data['items']:
        lines.append(empty)
    if data.get(cursor_key):
        next_query = {key: value for key, value in query.items() if key != cursor_key}
        next_query[cursor_key] = data[cursor_key]
        lines.extend(['', '[More / 更多](' + path + '?' + urlencode(next_query) + ')'])
    return '\n'.join(lines)
