"""Small human views of already-authorized post branches and claims."""

import json
from html import escape
from urllib.parse import quote, urlencode

from msg.core.identifiers import hex_id
from msg.core.post_preview import plain_text, post_preview, shorten
from msg.transports.code_views import markdown_renderer
from msg.transports.home_page import display_time, markdown_text
from msg.transports.thread_tree import thread_tree


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


def _thread_content(item):
    content = item.get('content', '')
    if isinstance(content, dict) and 'template_id' in content and 'values' in content:
        from msg.core.template_dsl import render_values

        content = render_values(content['values'])
    return content if isinstance(content, str) else ''


def _thread_body(content):
    renderer = markdown_renderer()
    lines = content[:8192].splitlines()
    if lines and lines[0] == '---':
        for index, line in enumerate(lines[1:], 1):
            if line in {'---', '...'}:
                lines = lines[index + 1 :]
                break
        else:
            lines = []
    tokens = renderer.parse('\n'.join(lines))
    # 标题已经显示在帖头；只移除同一个正文 H1，代码块和后续章节保留。
    for index, token in enumerate(tokens[:-2]):
        if token.type == 'heading_open' and token.tag == 'h1':
            del tokens[index : index + 3]
            break
    return renderer.renderer.render(tokens, renderer.options, {})


def _thread_href(item):
    target = item.get('path', '')
    if not isinstance(target, str) or not target.startswith('/') or target.startswith('//'):
        target = '/*' + hex_id(item['id'])
    return escape(quote(target, safe='/@*'), quote=True)


def _thread_numbers(item, status):
    count = status.get('reply_count') if isinstance(status, dict) else None
    complete = status.get('count_complete') is True if isinstance(status, dict) else False
    valid_count = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    if valid_count and complete:
        replies = str(count) + ' 条回复'
    elif valid_count and count:
        replies = '至少 ' + str(count) + ' 条回复'
    else:
        replies = '回复数未知'
    if isinstance(status, dict) and status.get('public_only'):
        replies = '公开 ' + replies
    views = item.get('view_count')
    if views is None and isinstance(status, dict):
        views = status.get('view_count')
    valid_views = isinstance(views, int) and not isinstance(views, bool) and views >= 0
    label = str(views) + ' 浏览' if valid_views else '浏览量未知'
    return (
        '<span class="thread-reply-count">'
        + escape(replies)
        + '</span>'
        + '<span class="thread-view-count">'
        + escape(label)
        + '</span>',
        not valid_count or not complete or count > 0,
    )


def thread_fragment_html(items, data, *, focus_id=None, embedded=False, reply_statuses=None):
    """读取已授权片段，不补读父帖，也不把当前页数量当成全串数量。"""
    nodes, roots = thread_tree(items, data['root'])
    statuses = {hex_id(rid): value for rid, value in (reply_statuses or {}).items()}
    root_id = hex_id(data['root'])
    focus_id = hex_id(focus_id) if focus_id else root_id
    omitted = {focus_id} if embedded else set()
    shown = {rid: node for rid, node in nodes.items() if rid not in omitted}
    displayed_roots = tuple(
        rid for rid, node in shown.items() if node.parent is None or node.parent in omitted
    )
    if not embedded:
        displayed_roots = roots
    replies = sum(rid != root_id for rid in shown)
    branches = sum(rid != root_id and node.parent == root_id for rid, node in shown.items())
    item_order = {rid: index for index, rid in enumerate(shown)}
    latest = max(
        (rid for rid in shown if rid != root_id),
        key=lambda rid: (str(shown[rid].item.get('created_at', '')), item_order[rid]),
        default=None,
    )
    heading = 'h2' if embedded else 'h1'
    section_id = 'thread-discussion' if embedded else 'discussion'
    heading_id = section_id + '-heading'
    parts = [
        '<section class="thread-discussion" data-thread-fragment id="'
        + section_id
        + '" aria-labelledby="'
        + heading_id
        + '">',
        '<header class="thread-heading"><div><'
        + heading
        + (' class="sr-only"' if embedded else '')
        + ' id="'
        + heading_id
        + '">讨论</'
        + heading
        + '><p class="thread-range">本页 '
        + str(replies)
        + ' 条回复'
        + (' · ' + str(branches) + ' 个分支' if branches else '')
        + (' · 后面还有回复' if data.get('next') else '')
        + '</p></div><nav class="thread-actions" aria-label="讨论导航">',
    ]
    if latest is not None:
        parts.append(
            '<a class="thread-control" href="#reply-'
            + escape(latest, quote=True)
            + '">最新回复</a>'
        )
    if not embedded:
        parts.append(
            '<a class="thread-control" href="/*' + escape(root_id, quote=True) + '">返回主帖</a>'
        )
    parts.append('</nav></header>')
    if any(node.incomplete for node in nodes.values()):
        parts.append('<p class="thread-notice">本页为部分讨论；有些父帖不在当前页。</p>')
    if any(node.invalid for node in nodes.values()):
        parts.append('<p class="thread-notice">部分回复关系异常，已单独列出。</p>')
    if not shown:
        parts.append('<p class="thread-empty">还没有可读的回复。讨论会在这里接着展开。</p>')
    else:
        parts.append('<ol class="thread-tree">')
        stack = [(rid, 0, False) for rid in reversed(displayed_roots)]
        while stack:
            rid, depth, closing = stack.pop()
            node = shown[rid]
            children = tuple(child for child in node.children if child in shown)
            if closing:
                if children:
                    parts.append('</ol></details>')
                parts.append('</li>')
                continue
            item = node.item
            preview = thread_preview(item)
            href = _thread_href(item)
            title = preview['title']
            is_root = rid == root_id
            parts.append(
                '<li class="thread-node'
                + (' thread-root' if is_root else '')
                + (' thread-focus' if rid == focus_id else '')
                + '" id="reply-'
                + escape(rid, quote=True)
                + '" data-depth="'
                + str(depth)
                + '"><article>'
            )
            metadata = []
            author = item.get('links', {}).get('a', {}).get('path', '')
            if isinstance(author, str) and author.startswith('/@'):
                metadata.append(
                    '<a class="thread-author" href="'
                    + escape(quote(author, safe='/@'), quote=True)
                    + '">'
                    + escape(author.removeprefix('/'))
                    + '</a>'
                )
            else:
                metadata.append('<span class="thread-author">帖子</span>')
            if item.get('created_at'):
                metadata.append(
                    '<a class="thread-permalink" href="'
                    + href
                    + '"><time datetime="'
                    + escape(item['created_at'], quote=True)
                    + '">'
                    + escape(display_time(item['created_at']))
                    + '</time></a>'
                )
            if is_root:
                metadata.append('<span class="thread-node-kind">主帖</span>')
            elif node.parent in shown:
                metadata.append(
                    '<a class="thread-parent" href="#reply-'
                    + escape(node.parent, quote=True)
                    + '">回复上帖</a>'
                )
            elif node.parent in omitted:
                metadata.append(
                    '<span class="thread-node-kind">'
                    + ('回复主帖' if node.parent == root_id else '回复当前帖')
                    + '</span>'
                )
            elif node.incomplete:
                metadata.append('<span>父帖不在当前页</span>')
            revision = item.get('revision')
            if isinstance(revision, str) and revision:
                reply_name = (
                    author.removeprefix('/')
                    if isinstance(author, str) and author.startswith('/@')
                    else ''
                )
                metadata.append(
                    '<button type="button" class="thread-reply" data-reply-to="'
                    + escape(rid, quote=True)
                    + '" data-reply-revision="'
                    + escape(revision, quote=True)
                    + '" data-reply-name="'
                    + escape(reply_name, quote=True)
                    + '">回复</button>'
                )
            parts.append(
                '<header class="thread-node-heading"><p class="thread-meta">'
                + ''.join(metadata)
                + '</p>'
            )
            if title != 'Untitled post':
                parts.append(
                    '<p class="thread-title" role="heading" aria-level="3"><a href="'
                    + href
                    + '">'
                    + escape(title)
                    + '</a></p>'
                )
            parts.append('</header>')
            content = _thread_content(item)
            if not content and preview['excerpt']:
                parts.append('<p class="thread-body">' + escape(preview['excerpt']) + '</p>')
            elif content:
                body = _thread_body(content)
                if len(content) > 1600:
                    parts.append(
                        '<details class="thread-body thread-long-body"><summary>'
                        + '<span class="thread-excerpt">'
                        + escape(preview['excerpt'] or '长正文')
                        + '</span><span class="thread-expand-label">展开正文</span>'
                        + '<span class="thread-collapse-label">收起正文</span></summary>'
                        + '<div class="thread-content">'
                        + body
                        + '</div></details>'
                    )
                else:
                    parts.append('<div class="thread-body">' + body + '</div>')
                if len(content) > 8192 or item.get('_body_more'):
                    parts.append(
                        (
                            '<p class="thread-truncation">正文节选（前 8192 字符） · '
                            if len(content) > 8192
                            else '<p class="thread-truncation">正文节选 · '
                        )
                        + '<a class="thread-control" href="'
                        + href
                        + '">阅读完整帖子</a></p>'
                    )
            numbers, expandable = _thread_numbers(item, statuses.get(rid))
            if not children and not expandable:
                parts.append('<p class="thread-stats">' + numbers + '</p>')
            parts.append('</article>')
            stack.append((rid, depth, True))
            if children or expandable:
                parts.append(
                    '<details class="thread-branch" data-thread-branch data-post-id="'
                    + escape(item['id'], quote=True)
                    + '"><summary>'
                    + '<span class="thread-branch-count"><svg class="thread-branch-chevron" '
                    + 'width="12" height="12" viewBox="0 0 12 12" fill="none" '
                    + 'stroke="currentColor" stroke-width="1.5" aria-hidden="true">'
                    + '<path d="m4 2 4 4-4 4"/></svg>'
                    + numbers
                    + '</span></summary>'
                )
                if children:
                    parts.append('<ol class="thread-tree" data-thread-branch-content>')
                    stack.extend((child, depth + 1, False) for child in reversed(children))
                else:
                    parts.append(
                        '<div data-thread-branch-content><p class="thread-branch-pending">'
                        + '子回复尚未读取。</p></div></details>'
                    )
        parts.append('</ol>')
    if data.get('next'):
        next_path = data['next']
        if (
            isinstance(next_path, str)
            and next_path.startswith('/')
            and not next_path.startswith('//')
        ):
            href = escape(quote(next_path, safe='/@*?=&%+'), quote=True)
            parts.append(
                '<p class="thread-more"><a class="thread-control" href="'
                + href
                + '">继续阅读回复</a></p>'
            )
    parts.append('</section>')
    return ''.join(parts)


def thread_read_html(
    items, data, *, path, raw_query='', account=None, service_url=None, reply_statuses=None
):
    """Render only items the thread operation has already authorized."""
    from msg.transports.home_page import document_html

    body = thread_fragment_html(items, data, reply_statuses=reply_statuses)
    body += (
        '<details><summary>原始数据 / Raw data</summary><pre id="thread-raw-data"><code>'
        + escape(json.dumps(data, ensure_ascii=False, indent=2))
        + '</code></pre></details>'
    )
    return document_html(
        '',
        title='讨论 · msg',
        account=account,
        raw_path=path,
        raw_query=raw_query,
        body_html=body,
        thread_script=True,
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
