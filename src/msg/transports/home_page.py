"""Browser HTML views with escaped content and a pinned WebMCP adapter."""

import re
from datetime import UTC, datetime
from html import escape
from string import punctuation
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

from msg.transports.board_page import CSS as BOARD_CSS, header_html as board_header_html
from msg.transports.browser_style import (
    BRAND_LINK,
    PREFERENCES as PREFERENCES,
    SKIP_LINK,
    THEME_CSS as THEME_CSS,
)
from msg.transports.document_outline import (
    CSS as OUTLINE_CSS,
    HASH as OUTLINE_HASH,
    SCRIPT as OUTLINE_SCRIPT,
    outline_html,
)
from msg.transports.http_common import BASE_HEADERS
from msg.transports.post_actions import POST_ACTIONS_CSS, POST_ACTIONS_HASH, POST_ACTIONS_SCRIPT
from msg.transports.profile_page import (
    PROFILE_CSS,
    PROFILE_HASH,
    PROFILE_SCRIPT,
    profile_body as profile_art_body,
)
from msg.transports.public_board import (
    CSS as PUBLIC_BOARD_CSS,
    HASH as PUBLIC_BOARD_HASH,
    TAG as PUBLIC_BOARD_TAG,
    html as public_board_html,
)
from msg.transports.webmcp import WEBMCP_HASH, WEBMCP_TAG
from msg.transports.wiki_actions import WIKI_HASH, WIKI_SCRIPT

HOME_BROWSER_HEADERS = {
    **BASE_HEADERS,
    'Content-Security-Policy': f"default-src 'none'; script-src 'sha256-{OUTLINE_HASH}' 'sha256-{PROFILE_HASH}' 'sha256-{WEBMCP_HASH}' 'sha256-{PUBLIC_BOARD_HASH}' 'sha256-{POST_ACTIONS_HASH}' 'sha256-{WIKI_HASH}'; "
    "connect-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
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
    return (
        f'<a class="current-account" href="{path}">{name}</a> '
        f'<a href="{path}/in" data-i18n="inbox">Inbox</a> '
        f'<a href="{path}/dm" data-i18n="dm">Direct messages</a> '
        f'<a href="{path}/follows" data-i18n="follows">Following</a> '
        f'<a href="{path}/followers" data-i18n="followers">Followers</a> '
        f'<a href="{path}/bal" data-i18n="wallet">Wallet</a> '
        '<a href="/bookmarks">Saved / 收藏</a> '
        '<a href="/oauth/logout" data-i18n="logout">Sign out</a>'
    )


def home_html(
    data=None,
    *,
    service_url=None,
    account=None,
    login_enabled=False,
    expired=False,
    public_board=None,
    csrf_token='',
):
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
            'Search': 'search_submit',
            'Following': 'follows',
            'Followers': 'followers',
            'Wallet': 'wallet',
        }
        marker = f' data-i18n="{keys[label]}"' if label in keys else ''
        return f'<a{marker} href="{escape(quote(path, safe="/@*&"), quote=True)}">{escape(str(label))}</a>'

    parts = [
        SKIP_LINK + '<header class="site-header">' + BRAND_LINK + '<nav aria-label="Primary">',
        link('Search', '/search'),
        link('Now', '/now'),
        link('Terminal', '/terminal'),
        link('Feed', '/feed'),
        link('Topics', '/topics'),
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
        + public_board_html(public_board, account, csrf_token)
    )
    if account:
        parts.extend([
            '<section class="account"><div><h2 data-i18n="account">Your account</h2><p>',
            link(account['name'], path),
            '</p>',
            *(
                [
                    '<details class="account-groups"><summary>User groups / 用户分类</summary><ul>',
                    *(
                        f'<li>{link(group["name"], group["path"])}</li>'
                        for group in account['groups']
                    ),
                    '</ul></details>',
                ]
                if account.get('groups')
                else []
            ),
            '</div><nav aria-label="Account">',
            link('Inbox / 收件箱', path + '/in'),
            link('Direct messages / 私聊', path + '/dm'),
            link('Outbox', path + '/out'),
            link('Following', path + '/follows'),
            link('Followers', path + '/followers'),
            link('Wallet', path + '/bal'),
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
        f'<style>{THEME_CSS}{PUBLIC_BOARD_CSS}</style></head><body class="page-home">{"".join(parts)}{WEBMCP_TAG}{PUBLIC_BOARD_TAG}</body></html>'
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


def document_html(
    markdown,
    *,
    title='msg',
    account=None,
    resource=None,
    raw_path='/',
    raw_query='',
    controls='',
    body_html=None,
    service_url=None,
    post_actions='',
    wiki_actions='',
):
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
    body = (
        MarkdownIt('commonmark', {'html': False}).enable('table').render(markdown)
        if body_html is None
        else body_html
    )
    is_profile = bool(resource and resource.get('type') == 'user' and 'profile' in resource)
    if is_profile and body_html is None:
        body = profile_art_body(resource)
    is_board = bool(resource and resource.get('type') == 'topic' and 'presentation' in resource)
    if is_board and body_html is None:
        board_value = {key: value for key, value in resource.items() if key != 'presentation'}
        board_markdown = resource_markdown(board_value, markdown)
        body = MarkdownIt('commonmark', {'html': False}).enable('table').render(board_markdown)
        body = re.sub(r'<h1(?:\s[^>]*)?>.*?</h1>\s*', '', body, count=1, flags=re.DOTALL)
        body = board_header_html(resource) + body
    body, outline = outline_html(body) if not is_profile and not is_board else (body, '')
    raw_url = escape(
        quote(raw_path, safe='/@*&') + '?' + (raw_query + '&' if raw_query else '') + 'format=raw',
        quote=True,
    )
    share_url = urljoin(service_url or '', quote(raw_path, safe='/@*&'))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="icon" href="/favicon.png">'
        '<link rel="search" type="application/opensearchdescription+xml" title="MSG" href="/opensearch.xml">'
        f'<style>{THEME_CSS}{BOARD_CSS if is_board else ""}{OUTLINE_CSS if outline else ""}{PROFILE_CSS if is_profile else ""}{POST_ACTIONS_CSS if post_actions or wiki_actions else ""}</style></head><body class="page-document{" has-outline" if outline else ""}">'
        + SKIP_LINK
        + '<header class="site-header">'
        + BRAND_LINK
        + '<nav aria-label="Account">'
        + account_navigation(account)
        + '</nav><div class="toolbar document-toolbar">'
        + PREFERENCES
        + '<div class="document-actions">'
        '<button type="button" id="msg-copy-document" title="Copy text / 复制正文"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/></svg><span class="sr-only" data-i18n="copy_document">Copy text</span></button>'
        f'<button type="button" id="msg-share-document" data-share-path="{escape(share_url, quote=True)}" title="Share / 分享"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3m-4 4 4-4 4 4M5 12v8h14v-8"/></svg><span class="sr-only" data-i18n="share_document">Share</span></button>'
        + f'<a class="raw-link" href="{raw_url}">raw</a></div>'
        '</div></header><main><p id="msg-document-status" class="document-status" role="status" aria-live="polite"></p>'
        f'<textarea id="msg-document-source" aria-label="Markdown source" readonly hidden>{escape(markdown)}</textarea>'
        f'<div id="content" class="prose" tabindex="-1">{controls}{metadata}{body}</div>{wiki_actions}{post_actions}</main>'
        + outline
        + (f'<script>{OUTLINE_SCRIPT}</script>' if outline else '')
        + (f'<script>{PROFILE_SCRIPT}</script>' if is_profile or is_board else '')
        + (f'<script>{POST_ACTIONS_SCRIPT}</script>' if post_actions else '')
        + (f'<script>{WIKI_SCRIPT}</script>' if wiki_actions else '')
        + f'{WEBMCP_TAG}</body></html>'
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
    if value.get('type') == 'organization':
        description = {
            'g_public': '公开用户分类，用于归类用户和授予组权限。 / The public user group, used for membership and group permissions.',
            'g_admins': '管理员用户分类，用于归类管理员和授予组权限。 / The administrators group, used for membership and group permissions.',
        }.get(
            value.get('id'),
            '用于归类用户和授予组权限。 / Used for user membership and group permissions.',
        )
        return '\n'.join([
            '# ' + markdown_text(value['name']),
            '',
            'User group / 用户分类',
            '',
            description,
            '',
        ])
    if 'items' in value and value.get('type') == 'topic':
        lines = ['# ' + markdown_text(value['name']), '']
        presentation = value.get('presentation')
        if presentation:
            lines.extend([
                markdown_text(presentation['description']),
                '',
                'Administrators: '
                + ', '.join(
                    markdown_text(name)
                    for name in presentation.get(
                        'administrator_names', presentation['administrators']
                    )
                ),
                '',
            ])
        rules = value.get('board_rules')
        if rules:
            lines.extend([
                '## Board rules / 本板规则',
                '',
                markdown_text(rules['text']) if rules['text'] else 'No additional board rules.',
                '',
                'Posting: '
                + rules['posting_policy']
                + ' · Membership: '
                + rules['membership_policy'],
                '',
                'Rule generation: '
                + str(rules['generation'])
                + ' · [Platform rules](/_rules/topics)',
                '',
            ])
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


def mailbox_html(value, name, *, account=None, service_url=None):
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
    return document_html(
        '\n'.join(lines),
        title=title,
        account=account,
        raw_path=value['path'],
        service_url=service_url,
    )
