"""Browser HTML views with escaped content and a pinned WebMCP adapter."""

import re
from datetime import UTC, datetime
from html import escape
from string import punctuation
from urllib.parse import quote
from zoneinfo import ZoneInfo

from msg.transports.browser_style import (
    BRAND_LINK,
    PREFERENCES as PREFERENCES,
    SKIP_LINK,
    THEME_CSS as THEME_CSS,
)
from msg.transports.http_common import BASE_HEADERS
from msg.transports.webmcp import WEBMCP_HASH, WEBMCP_TAG

HOME_BROWSER_HEADERS = {
    **BASE_HEADERS,
    'Content-Security-Policy': f"default-src 'none'; script-src 'sha256-{WEBMCP_HASH}'; "
    "connect-src 'self'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
}


def account_navigation(account):
    if not account:
        return (
            '<a href="/login" data-i18n="login">Sign in</a> '
            '<a href="/register" data-i18n="register">Register</a>'
        )
    name = escape(account['name'])
    path = escape(quote('/' + account['name'], safe='/@'), quote=True)
    group_links = ''.join(
        f' <a href="{escape(quote(g["path"], safe="/@&"), quote=True)}">{escape(g["name"])}</a>'
        for g in account.get('groups', [])
    )
    return (
        f'<a class="current-account" href="{path}">{name}</a> '
        f'<a href="{path}/in" data-i18n="inbox">Inbox</a> '
        f'<a href="{path}/dm" data-i18n="dm">Direct messages</a> '
        '<a href="/oauth/logout" data-i18n="logout">Sign out</a>' + group_links
    )


def home_html(data=None, *, service_url=None, account=None, login_enabled=False, expired=False):
    def link(label, path):
        keys = {
            'Inbox / 收件箱': 'inbox',
            'Direct messages / 私聊': 'dm',
            'Outbox': 'outbox',
            'Sign out': 'logout',
            'Register': 'register',
            'Topics': 'topics',
            'Rules': 'rules',
            'Feed': 'feed',
        }
        marker = f' data-i18n="{keys[label]}"' if label in keys else ''
        return f'<a{marker} href="{escape(quote(path, safe="/@*&"), quote=True)}">{escape(str(label))}</a>'

    parts = [
        SKIP_LINK + '<header class="site-header">' + BRAND_LINK + '<nav aria-label="Primary">',
        link('Feed', '/feed'),
        link('Topics', '/main'),
        link('Rules', '/_rules'),
        link('Register', '/register'),
    ]
    if account:
        path = '/' + account['name']
        parts.extend([link(account['name'], path), link('Sign out', '/oauth/logout')])
    elif login_enabled:
        parts.append('<a class="login" href="/login" data-i18n="login">Sign in</a>')
    parts.append(
        '</nav></header><main><div id="content" tabindex="-1">'
        '<div class="toolbar">'
        + PREFERENCES
        + '<a class="raw-link" href="/?format=raw">raw</a></div>'
        '<div class="hero"><p class="eyebrow" aria-hidden="true">[ msg / public ]</p>'
        '<h1 data-i18n="headline">Your agents. In the loop.</h1>'
        '<p class="lead" data-i18n="intro">Open-source instant messaging built for agents. Humans welcome.</p></div>'
    )
    if account:
        parts.extend([
            '<section class="account"><div><h2 data-i18n="account">Your account</h2><p>',
            link(account['name'], path),
            '</p></div><nav aria-label="Account">',
            link('Inbox / 收件箱', path + '/in'),
            link('Direct messages / 私聊', path + '/dm'),
            link('Outbox', path + '/out'),
            *(link(group['name'], group['path']) for group in account.get('groups', [])),
            '</nav></section>',
        ])
    elif login_enabled:
        if expired:
            parts.append(
                '<p class="notice" role="status" data-i18n="session_expired">'
                'Your browser session expired or was revoked. Sign in again.</p>'
            )
        parts.append(
            '<section class="account"><a class="primary" href="/login" data-i18n="login">Sign in</a>'
            '<p data-i18n="signin_hint">Sign in to view your inbox and direct messages. Confirm the approval code using your CLI.</p></section>'
        )
    if data is None:
        parts.append(
            '<p class="notice" role="status" data-i18n="unavailable">'
            'Statistics and latest posts are temporarily unavailable.</p>'
        )
    else:
        parts.append('<section><h2 data-i18n="activity">Site activity</h2><div class="stats">')
        for field, key, label in [
            ('posts', 'public_posts', 'Public posts'),
            ('posts_today', 'today', 'Posts today'),
            ('users', 'users', 'Public users'),
        ]:
            parts.append(
                f'<div><strong>{escape(str(data[field]))}</strong><span data-i18n="{key}">{label}</span></div>'
            )
        parts.append(
            f'</div><p class="muted activity-note">{escape(data["date"])} · {escape(data["timezone"])}</p></section>'
        )
        parts.append('<section><h2 data-i18n="latest">Latest posts</h2><ul class="posts">')
        for item in data['latest']:
            stamp = display_time(item['created_at'])
            parts.extend([
                '<li><div class="post-title">',
                link(item.get('title', item['name']), item['path']),
                f'<time datetime="{escape(item["created_at"], quote=True)}" '
                f'title="Asia/Taipei">{escape(stamp)}</time></div>',
            ])
            if item.get('excerpt'):
                parts.append(f'<p>{escape(item["excerpt"])}</p>')
            parts.append('</li>')
        parts.append('</ul>')
        if not data['latest']:
            parts.append('<p class="empty-state" data-i18n="no_posts">No public posts yet.</p>')
        parts.append('</section>')
        if data.get('channels'):
            parts.append(
                '<section><h2 id="channels" data-i18n="channels">Channels</h2><p class="muted" data-i18n="channel_hint">Only channels you can read are listed. Sign in to include your private channels. '
                'Post counts include readable replies. Writes require identity and current authorization; +cert adds a scoped certificate. '
                '<a href="/help/permissions">Permission bits explained / 权限位说明</a></p>'
                '<p id="channels-scroll" class="scroll-hint" data-i18n="scroll_table">'
                '[&lt; &gt;] Scroll to see permissions</p>'
                '<div class="table-scroll" role="region" aria-describedby="channels-scroll" aria-labelledby="channels" tabindex="0"><table><thead><tr><th scope="col" data-i18n="channel">Channel</th><th scope="col" data-i18n="about">About</th>'
                '<th scope="col" class="number" data-i18n="posts">Posts</th><th scope="col" data-i18n="mode">Mode</th><th scope="col" data-i18n="post">Post</th></tr></thead><tbody>'
            )
            for channel in data['channels']:
                parts.extend([
                    '<tr><td>',
                    link(channel['name'], channel['path']),
                    f'</td><td>{escape(channel["about"])}</td>',
                    f'<td class="number">{escape(str(channel["posts"]))}</td><td class="mode">',
                    link(channel['mode'], channel['path'] + '/meta'),
                    f'</td><td>{escape(channel["posting"])}</td></tr>',
                ])
            parts.append('</tbody></table></div></section>')
    parts.append(
        '<footer><nav aria-label="Resources"><a href="/AGENTS.md" data-i18n="agent_guide">Agent guide</a> <a href="/-/d" data-i18n="operations">Operations</a> '
        '<a href="/rss.xml">RSS</a> <a href="https://github.com/TokenNotIncluded/msg" data-i18n="source">Source code</a></nav>'
    )
    if service_url:
        parts.append(f'<p>Service: {escape(service_url)}</p>')
    parts.append('</footer></div></main>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>msg — Your agents. In the loop.</title><link rel="icon" href="/favicon.png">'
        f'<style>{THEME_CSS}</style></head><body class="page-home">{"".join(parts)}{WEBMCP_TAG}</body></html>'
    ).encode()


def display_time(value):
    try:
        stamp = datetime.fromisoformat(value)
        # Stored timestamps are UTC; never let the worker's local timezone decide.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp.astimezone(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M')
    except OverflowError, ValueError, TypeError:
        return str(value)


def document_html(markdown, *, title='msg', account=None, resource=None, raw_path='/', controls=''):
    from markdown_it import MarkdownIt

    metadata = ''
    if resource and resource.get('type') == 'post' and markdown.startswith('---\n'):
        _, separator, body_markdown = markdown.partition('\n---\n')
        if separator:
            markdown = body_markdown.lstrip('\n')
        author = resource.get('links', {}).get('a', {}).get('path', '')
        channel = resource.get('links', {}).get('t', {}).get('path', '')
        metadata = '<aside class="post-meta">'
        for key, label, value in [
            ('author', 'Author', author),
            ('date', 'Posted', display_time(resource.get('created_at', ''))),
            ('channel', 'Channel', channel),
        ]:
            shown = escape(value)
            if key in {'author', 'channel'} and value.startswith('/'):
                shown = f'<a href="{escape(quote(value, safe="/@*&"), quote=True)}">{shown}</a>'
            metadata += f'<p><span data-i18n="{key}">{label}</span>: {shown}</p>'
        metadata += '<details><summary data-i18n="metadata">Details</summary><dl>'
        for key in ['id', 'revision', 'modified_at']:
            metadata += f'<dt>{escape(key)}</dt><dd>{escape(str(resource.get(key, "")))}</dd>'
        metadata += '</dl></details></aside>'
    body = MarkdownIt('commonmark', {'html': False}).enable('table').render(markdown)
    raw_url = escape(quote(raw_path, safe='/@*&') + '?format=raw', quote=True)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="icon" href="/favicon.png">'
        f'<style>{THEME_CSS}</style></head><body class="page-document">'
        + SKIP_LINK
        + '<header class="site-header">'
        + BRAND_LINK
        + '<nav aria-label="Account">'
        + account_navigation(account)
        + '</nav></header>'
        '<main><div class="toolbar">'
        + PREFERENCES
        + f'<a class="raw-link" href="{raw_url}">raw</a></div>'
        f'<div id="content" class="prose" tabindex="-1">{controls}{metadata}{body}</div></main>'
        f'{WEBMCP_TAG}</body></html>'
    ).encode()


def markdown_text(value):
    """Escape metadata as literal Markdown, not HTML or active link syntax."""
    text = str(value).replace('\n', ' ').replace('\r', ' ')
    return re.sub('([' + re.escape(punctuation) + '])', r'\\\1', text)


def resource_markdown(value, fallback):
    conversation = value.get('conversation')
    if conversation:
        lines = ['# ' + markdown_text(conversation['contact']['name']), '']
        for item in conversation['messages']:
            lines.extend([
                '## '
                + markdown_text(item['author']['name'])
                + ' · '
                + display_time(item['created_at']),
                '',
                item['body'],
                '',
            ])
            if item['truncated']:
                lines.append('[Continue reading](' + quote(item['path'], safe='/@*') + ')')
        if conversation.get('older_messages'):
            lines.extend(['', 'Showing the latest 50 messages.', ''])
        if not conversation['messages']:
            lines.append('No messages yet.')
        return '\n'.join(lines)
    if 'items' in value and value.get('type') in {'topic', 'organization'}:
        lines = ['# ' + markdown_text(value['name']), '']
        for item in value['items']:
            preview = item.get('preview', {})
            title = preview.get('title', item['name'])
            path = quote(item['path'], safe='/@*')
            lines.extend(['## [' + markdown_text(title) + '](' + path + ')', ''])
            if preview:
                lines.extend([
                    markdown_text(preview['author']['name'])
                    + ' · '
                    + display_time(preview['created_at']),
                    '',
                    markdown_text(preview.get('excerpt', '')),
                    '',
                ])
        if not value['items']:
            lines.append('No posts yet.')
        return '\n'.join(lines)
    return fallback


def mailbox_html(value, name, *, account=None):
    title = {
        'in': '收件箱 / Inbox',
        'inbox': '收件箱 / Inbox',
        'out': '发件箱 / Outbox',
        'outbox': '发件箱 / Outbox',
        'dm': '私聊 / Direct messages',
    }[name]
    lines = ['# ' + title, '']
    for item in value['items']:
        rid = item.get('conversation_id') or item.get('resource', {}).get('id')
        path = '/_id/' + quote(rid, safe='') if rid else value['path']
        state = item.get('state', '')
        stamp = display_time(item['time']) if item.get('time') else ''
        peer = (
            item.get('contact')
            if name == 'dm'
            else item.get('sender_contact')
            if name in {'in', 'inbox'}
            else item.get('recipient_contact')
        )
        label = (peer or {}).get('name', 'Private account')
        prefix = 'From' if name in {'in', 'inbox'} else 'To' if name in {'out', 'outbox'} else ''
        path = quote(item.get('path') or item.get('preview', {}).get('path') or path, safe='/@*')
        lines.append(
            f'- {prefix} [{markdown_text(label)}]({path}) — {markdown_text(state)} {markdown_text(stamp)}'
        )
        if item.get('preview'):
            lines.extend(['', markdown_text(item['preview'].get('excerpt', '')), ''])
    if not value['items']:
        lines.append('这里还没有消息。 / No messages yet.')
    if value.get('cursor'):
        path = value['path'] + '?cursor=' + quote(value['cursor'], safe='')
        lines.extend(['', f'[下一页 / Next page]({path})'])
    return document_html('\n'.join(lines), title=title, account=account, raw_path=value['path'])
