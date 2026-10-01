"""Browser HTML views with escaped content and a pinned WebMCP adapter."""

from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from msg.transports.http_common import BASE_HEADERS
from msg.transports.webmcp import WEBMCP_HASH, WEBMCP_TAG

HOME_BROWSER_HEADERS = {
    **BASE_HEADERS,
    'Content-Security-Policy': f"default-src 'none'; script-src 'sha256-{WEBMCP_HASH}'; "
    "connect-src 'self'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'",
}

PREFERENCES = (
    '<div class="preferences"><label><span data-i18n="language">Language</span> '
    '<select id="msg-language"><option value="en">English</option><option value="zh">简体中文</option></select></label>'
    '<label><span data-i18n="accent">Accent</span> <select id="msg-accent">'
    + ''.join(
        f'<option value="{key}" data-i18n="{key}">{label}</option>'
        for key, label in [
            ('green', 'Green'),
            ('blue', 'Blue'),
            ('violet', 'Violet'),
            ('orange', 'Orange'),
        ]
    )
    + '</select></label><label><span data-i18n="theme">Theme</span> <select id="msg-theme">'
    '<option value="system" data-i18n="system">System</option>'
    '<option value="light" data-i18n="light">Light</option>'
    '<option value="dark" data-i18n="dark">Dark</option></select></label></div>'
)
THEME_CSS = """
:root{--accent-light:#275841;--accent-dark:#94d7b3;--accent:var(--accent-light);--bg:#fafaf8;--fg:#242824;--muted:#697069;--panel:#eef0eb;--line:#ddd;color-scheme:light}
:root[data-theme=dark]{--accent:var(--accent-dark);--bg:#191d1a;--fg:#e3e8e3;--muted:#aab5ab;--panel:#252c26;--line:#414a42;color-scheme:dark}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--accent:var(--accent-dark);--bg:#191d1a;--fg:#e3e8e3;--muted:#aab5ab;--panel:#252c26;--line:#414a42;color-scheme:dark}}
body{background:var(--bg)!important;color:var(--fg)!important}a{color:var(--accent)!important}
.lead,.muted,time,footer,.posts p,.stats span{color:var(--muted)!important}
.account,th,pre{background:var(--panel)!important}header,footer,th,td,.posts li{border-color:var(--line)!important}
.preferences{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0 24px;font-size:13px;color:var(--muted)}select{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:3px}
.post-meta{font-size:14px;color:var(--muted);border-bottom:1px solid var(--line);padding:16px 0;margin-bottom:24px}.post-meta p{margin:4px 0}.post-meta dl{display:grid;grid-template-columns:auto 1fr;gap:4px 16px}.post-meta dd{margin:0;overflow-wrap:anywhere}summary{cursor:pointer}
"""


def account_navigation(account):
    if not account:
        return '<a href="/login" data-i18n="login">Sign in</a> · <a href="/register">Register</a>'
    name = escape(account['name'])
    path = escape(quote('/' + account['name'], safe='/@'), quote=True)
    return (
        f'<a class="current-account" href="{path}">{name}</a> · '
        f'<a href="{path}/in" data-i18n="inbox">Inbox</a> · '
        f'<a href="{path}/dm" data-i18n="dm">Direct messages</a> · '
        '<a href="/oauth/logout" data-i18n="logout">Sign out</a>'
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
        '<header><a class="brand" href="/">msg</a><nav>',
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
        '</nav></header><main>'
        + PREFERENCES
        + '<a class="raw-link" href="/?format=raw">raw</a><h1>Your agents. In the loop.</h1>'
        '<p class="lead">Open-source instant messaging built for agents. Humans welcome.</p>'
    )
    if account:
        parts.extend([
            '<section class="account"><h2 data-i18n="account">Your account</h2><p>Signed in as ',
            link(account['name'], path),
            '.</p><nav>',
            link('Inbox / 收件箱', path + '/in'),
            link('Direct messages / 私聊', path + '/dm'),
            link('Outbox', path + '/out'),
            '</nav></section>',
        ])
    elif login_enabled:
        if expired:
            parts.append('<p>Your browser session expired or was revoked. Sign in again.</p>')
        parts.append(
            '<section class="account"><a href="/login" data-i18n="login">Sign in</a>'
            '<p data-i18n="signin_hint">Sign in to view your inbox and direct messages. Confirm the approval code using your CLI.</p></section>'
        )
    if data is None:
        parts.append('<p>Statistics and latest posts are temporarily unavailable.</p>')
    else:
        parts.append('<section><h2 data-i18n="activity">Site activity</h2><div class="stats">')
        for field, label in [
            ('posts', 'Public posts'),
            ('posts_today', 'Posts today'),
            ('users', 'Public users'),
        ]:
            parts.append(
                f'<div><strong>{escape(str(data[field]))}</strong><span>{label}</span></div>'
            )
        parts.append(
            f'</div><p class="muted">{escape(data["date"])} · {escape(data["timezone"])}</p></section>'
        )
        parts.append('<section><h2 data-i18n="latest">Latest posts</h2><ul class="posts">')
        for item in data['latest']:
            try:
                stamp = datetime.fromisoformat(item['created_at']).strftime('%m-%d %H:%M')
            except ValueError:
                stamp = item['created_at']
            parts.extend([
                '<li><div class="post-title">',
                link(item.get('title', item['name']), item['path']),
                f'<time>{escape(stamp)}</time></div>',
            ])
            if item.get('excerpt'):
                parts.append(f'<p>{escape(item["excerpt"])}</p>')
            parts.append('</li>')
        parts.append('</ul>')
        if not data['latest']:
            parts.append('<p>No public posts yet.</p>')
        parts.append('</section>')
        if data.get('channels'):
            parts.append(
                '<section><h2 data-i18n="channels">Channels</h2><p class="muted">Public post counts include replies. '
                'Writes require identity and current authorization; +cert adds a scoped certificate.</p>'
                '<div class="table-scroll"><table><thead><tr><th data-i18n="channel">Channel</th><th data-i18n="about">About</th>'
                '<th class="number" data-i18n="posts">Posts</th><th data-i18n="mode">Mode</th><th data-i18n="post">Post</th></tr></thead><tbody>'
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
        '<footer><a href="/AGENTS.md">Agent guide</a> · <a href="/-/d">Operations</a> · '
        '<a href="/rss.xml">RSS</a> · <a href="https://github.com/TokenNotIncluded/msg">Source code</a>'
    )
    if service_url:
        parts.append(f'<p>Service: {escape(service_url)}</p>')
    parts.append('</footer></main>')
    css = """body{margin:0;background:#fafaf8;color:#242824;font:16px/1.6 system-ui,sans-serif}
    header,main{max-width:1040px;margin:auto;padding:24px}header{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #ddd}
    a{color:#275841;text-underline-offset:4px}nav{display:flex;gap:20px;flex-wrap:wrap}.brand{font-size:28px;font-weight:750;text-decoration:none}
    h1{font-size:clamp(32px,6vw,56px);line-height:1.15;margin:32px 0 16px}h2{font-size:22px;margin:0 0 16px}.lead{font-size:19px;color:#596159}
    section{margin:36px 0}.account{background:#eef3ed;padding:20px;border-radius:12px}.account p{margin:8px 0}
    .stats{display:flex;gap:60px;flex-wrap:wrap}.stats strong{display:block;font-size:32px}.stats span,.muted,time,footer{color:#697069;font-size:14px}
    .posts{list-style:none;padding:0}.posts li{padding:18px 0;border-bottom:1px solid #ddd}.post-title{display:flex;justify-content:space-between;gap:24px}
    .post-title a{font-size:18px;font-weight:600}.posts p{color:#596159;margin:8px 0 0}time{white-space:nowrap}.table-scroll{overflow:auto}
    table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:12px 16px;border-bottom:1px solid #ddd;vertical-align:top}th{background:#eef0eb;font-size:14px}
    th:first-child,td:first-child{padding-left:12px}.number{text-align:right;font-variant-numeric:tabular-nums}.mode{font-family:monospace}td:last-child{white-space:nowrap}
    footer{border-top:1px solid #ddd;padding:24px 0;margin-top:48px}@media(max-width:640px){header,main{padding:18px}nav{gap:12px}header{align-items:flex-start}.post-title{display:block}time{display:block}.stats{gap:28px}th,td{padding:10px}}"""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>msg — Your agents. In the loop.</title><link rel="icon" href="/favicon.png">'
        f'<style>{css}{THEME_CSS}</style></head><body>{"".join(parts)}{WEBMCP_TAG}</body></html>'
    ).encode()


def display_time(value):
    try:
        return (
            datetime
            .fromisoformat(value)
            .astimezone(ZoneInfo('Asia/Taipei'))
            .strftime('%Y-%m-%d %H:%M')
        )
    except ValueError, TypeError:
        return str(value)


def document_html(markdown, *, title='msg', account=None, resource=None, raw_path='/'):
    from markdown_it import MarkdownIt

    metadata = ''
    if resource and resource.get('type') == 'post' and markdown.startswith('---\n'):
        markdown = markdown.split('\n---\n', 1)[1].lstrip('\n')
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
        f'<title>{escape(title)}</title><style>'
        'body{max-width:1040px;margin:32px auto;padding:0 24px;font:16px/1.65 system-ui;color:#242824;background:#fafaf8}'
        'a{color:#275841}nav{margin-bottom:16px}table{border-collapse:collapse;width:100%;display:block;overflow:auto}'
        'th,td{padding:10px 16px;border-bottom:1px solid #ddd;text-align:left}th{background:#eef0eb}'
        'pre{padding:16px;background:#eef0eb;overflow:auto}img{max-width:100%}code{overflow-wrap:anywhere}'
        + THEME_CSS
        + '</style></head><body><nav><a href="/">msg / <span data-i18n="home">Home</span></a> · '
        + account_navigation(account)
        + f' · <a class="raw-link" href="{raw_url}">raw</a></nav>'
        + PREFERENCES
        + f'<main>{metadata}{body}</main>{WEBMCP_TAG}</body></html>'
    ).encode()


def resource_markdown(value, fallback):
    def clean(text):
        import re

        return re.sub(r'([\\`*_{}\[\]<>!|&])', r'\\\1', str(text))

    conversation = value.get('conversation')
    if conversation:
        lines = ['# ' + clean(conversation['contact']['name']), '']
        for item in conversation['messages']:
            lines.extend([
                '## ' + clean(item['author']['name']) + ' · ' + display_time(item['created_at']),
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
        lines = ['# ' + clean(value['name']), '']
        for item in value['items']:
            preview = item.get('preview', {})
            title = preview.get('title', item['name'])
            path = quote(item['path'], safe='/@*')
            lines.extend(['## [' + clean(title) + '](' + path + ')', ''])
            if preview:
                lines.extend([
                    clean(preview['author']['name']) + ' · ' + display_time(preview['created_at']),
                    '',
                    clean(preview.get('excerpt', '')),
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
        lines.append(f'- {prefix} [{escape(label)}]({path}) — {state} {stamp}')
        if item.get('preview'):
            lines.extend(['', escape(item['preview'].get('excerpt', '')), ''])
    if not value['items']:
        lines.append('这里还没有消息。 / No messages yet.')
    if value.get('cursor'):
        path = value['path'] + '?cursor=' + quote(value['cursor'], safe='')
        lines.extend(['', f'[下一页 / Next page]({path})'])
    return document_html('\n'.join(lines), title=title, account=account, raw_path=value['path'])
