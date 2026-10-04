"""默认只展示数字，正文按需读取；未知统计不伪装为零。"""

from base64 import b64encode
from hashlib import sha256
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from msg.core.codec import wire
from msg.transports.home_page import HOME_BROWSER_HEADERS, document_html, home_html
from msg.transports.thread_browser import (
    branch_items,
    fragment_html,
    post_discussion,
    reply_statuses,
    root_post_index,
)
from msg.transports.thread_panel import HASH, SCRIPT, discussion_panel_html, reply_label, reply_link


class Elements(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def matching(self, attribute, value=None):
        return [
            attrs
            for _, attrs in self.elements
            if attribute in attrs and (value is None or attrs[attribute] == value)
        ]


def post(number, parent=None, *, body='回复正文'):
    rid = 'r_' + f'{number:032x}'
    item = {
        'id': rid,
        'revision': 'v_' + f'{number:032x}',
        'name': f'post-{number}.md',
        'path': '/main/' + f'post-{number}.md',
        'type': 'post',
        'created_at': '2026-10-04T10:00:00Z',
        'content': body,
        'links': {'a': {'path': '/@reader'}, 't': {'path': '/main'}},
        'relations': [],
    }
    if parent:
        item['relations'].append({'type': 'reply_to', 'target': {'id': parent['id']}})
    return item


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        (None, '回复状态暂不可用'),
        ({'reply_count': 0, 'count_complete': True}, '0 条回复'),
        ({'reply_count': 3, 'count_complete': True}, '3 条回复'),
        ({'reply_count': 3, 'count_complete': False}, '至少 3 条回复'),
        ({'reply_count': 0, 'count_complete': False}, '回复尚未完整加载'),
        ({'reply_count': 0}, '回复尚未完整加载'),
        ({'reply_count': 0, 'count_complete': True, 'public_only': True}, '公开 0 条回复'),
        ({'reply_count': 3, 'count_complete': True, 'public_only': True}, '公开 3 条回复'),
        ({'reply_count': 3, 'count_complete': False, 'public_only': True}, '公开 至少 3 条回复'),
    ],
)
def test_reply_label_distinguishes_exact_count_lower_bound_and_unavailable(status, expected):
    assert reply_label(status) == expected


def test_home_shows_reply_counts_next_to_each_post_without_changing_input():
    item = {
        'name': 'demo.md',
        'title': '主页中的帖子',
        'path': '/main/demo.md',
        'created_at': '2026-10-04T10:00:00Z',
        'view_count': 21,
    }
    data = {
        'posts': 1,
        'posts_today': 1,
        'users': 1,
        'date': '2026-10-04',
        'timezone': 'UTC',
        'latest': [item],
    }
    page = home_html(
        data,
        reply_status={item['path']: {'reply_count': 2, 'count_complete': True}},
    ).decode()
    assert '21 浏览' in page and '2 条回复' in page
    assert Elements(page).matching('class', 'reply-link') == [
        {'class': 'reply-link', 'href': item['path'] + '#discussion'}
    ]
    assert 'reply_count' not in item and 'reply_status' not in data
    unknown = home_html(data).decode()
    assert '回复状态暂不可用' in unknown and '0 条回复' not in unknown


@pytest.mark.asyncio
async def test_initial_post_render_reads_only_counts_and_keeps_reply_body_unfetched():
    root = post(1, body='# 主帖标题\n\n主帖已有正文。')
    status = {
        'id': root['id'],
        'reply_count': 1,
        'count_complete': True,
        'view_count': 9,
        'recent': [{'ref': {'id': post(2)['id']}, 'summary': '不应自动读取或展示的评论正文'}],
    }
    requests = []

    async def execute(packet):
        requests.append(packet)
        assert packet.operation == 'discussion.reply_status'
        return SimpleNamespace(error=None, data={'items': [status]})

    panel, rendered_status = await post_discussion(execute, root, 'https://msg.example')
    assert len(requests) == 1 and wire(requests[0].arguments) == {'ids': [root['id']]}
    assert rendered_status == status
    page = document_html(
        '---\ntype: post\n---\n\n' + root['content'],
        resource=root,
        raw_path=root['path'],
        discussion_html=panel,
        reply_status=status,
        post_actions='<section id="post-actions">写回复</section>',
    ).decode()
    dom = Elements(page)
    assert '1 条回复' in page and '不应自动读取或展示的评论正文' not in page
    assert post(2)['id'] not in panel
    assert page.count('<p>主帖已有正文。</p>') == 1
    assert page.index('id="discussion"') < page.index('id="post-actions"')
    content = dom.matching('data-thread-content')
    assert len(content) == 1 and 'hidden' in content[0]
    assert not dom.matching('data-thread-fragment')
    assert dom.matching('data-thread-expand')[0]['aria-controls'] == 'discussion-content'
    assert dom.matching('data-thread-expand')[0]['aria-expanded'] == 'false'
    assert 'data-thread-content hidden></div>' in panel
    ids = [attrs['id'] for _, attrs in dom.elements if 'id' in attrs]
    assert len(ids) == len(set(ids))


def test_empty_and_incomplete_discussions_have_truthful_visible_states():
    resource = post(1)
    empty = discussion_panel_html(resource, {'reply_count': 0, 'count_complete': True})
    assert '0 条回复' in empty and 'data-thread-content hidden></div>' in empty
    partial = discussion_panel_html(
        resource,
        {'reply_count': 2, 'count_complete': False, 'thread_count': 4, 'thread_complete': False},
    )
    assert '至少 2 条回复' in partial and '至少 4 条可读回复' not in partial
    assert 'data-thread-content hidden></div>' in partial
    unavailable = discussion_panel_html(resource, None)
    assert '回复状态暂不可用' in unavailable and '0 条回复' not in unavailable


def test_recent_summaries_are_never_embedded_and_paths_cannot_create_attribute_handlers():
    resource = post(1)
    status = {
        'reply_count': 1,
        'count_complete': True,
        'recent': [{'ref': {'id': post(2)['id']}, 'summary': '<img src=x onerror=steal()>'}],
    }
    panel = discussion_panel_html(resource, status)
    dom = Elements(panel)
    assert 'steal()' not in panel and post(2)['id'] not in panel
    assert not any(tag in {'img', 'script', 'iframe', 'object', 'embed'} for tag, _ in dom.elements)
    assert not any(name.startswith('on') for _, attrs in dom.elements for name in attrs)
    link = reply_link(status, '/main/" onmouseover="steal()')
    attrs = Elements(link).matching('class', 'reply-link')[0]
    assert set(attrs) == {'class', 'href'} and '%22' in attrs['href']


@pytest.mark.parametrize('query_id', ['r_' + f'{1:032x}', '/main/讨论 空白.md'])
def test_fragment_continuation_preserves_focus_and_is_an_inline_read_link(query_id):
    root, reply = post(1), post(2)
    reply['relations'] = [{'type': 'reply_to', 'target': {'id': root['id']}}]
    markup = fragment_html(
        {'root': root['id'], 'items': [root, reply], 'cursor': 'opaque-page+'},
        focus_id=root['id'],
        query={'id': query_id, 'limit': 8},
        status={'reply_count': 8, 'count_complete': True},
    )
    links = Elements(markup).matching('href')
    continuation = [attrs['href'] for attrs in links if '/_post/thread-fragment?' in attrs['href']]
    assert len(continuation) == 1
    parameters = parse_qs(urlsplit(continuation[0]).query)
    assert parameters == {'id': [query_id], 'limit': ['8'], 'cursor': ['opaque-page+']}
    assert Elements(markup).matching('data-thread-fragment')[0]['data-reply-label'] == '8 条回复'
    assert not markup.startswith('<!doctype') and '<script>' not in markup


def test_explicit_fragment_shows_only_loaded_layer_and_keeps_next_layer_collapsed():
    root = post(1, body='主帖已在页面正文显示')
    reply = post(2, root, body='点击展开后才读取的回复正文')
    child = post(3, reply, body='属于下一层的回复正文')
    statuses = {
        reply['id']: {'reply_count': 41, 'count_complete': True, 'view_count': 7319},
        child['id']: {'reply_count': 0, 'count_complete': True, 'view_count': 5},
    }
    fragment = fragment_html(
        {'root': root['id'], 'items': [{'id': root['id']}, reply]},
        focus_id=root['id'],
        query={'id': root['id'], 'limit': 8},
        status={'reply_count': 1, 'count_complete': True},
        statuses=statuses,
    )
    dom = Elements(fragment)
    assert '主帖已在页面正文显示' not in fragment
    assert fragment.count('<article>') == 1
    assert '点击展开后才读取的回复正文' in fragment
    assert '属于下一层的回复正文' not in fragment
    assert child['id'] not in fragment
    assert dom.matching('data-thread-fragment')[0]['id'] != 'discussion'
    branches = dom.matching('class', 'thread-branch')
    assert len(branches) == 1 and 'open' not in branches[0]
    assert '7319' in fragment and '41 条回复' in fragment
    assert '子回复尚未读取' in fragment
    ids = [attrs['id'] for _, attrs in dom.elements if 'id' in attrs]
    assert len(ids) == len(set(ids))
    next_layer = fragment_html(
        {'root': reply['id'], 'items': [{'id': reply['id']}, child]},
        focus_id=reply['id'],
        query={'id': reply['id'], 'limit': 8},
        status=statuses[reply['id']],
        statuses=statuses,
    )
    assert '点击展开后才读取的回复正文' not in next_layer
    assert '属于下一层的回复正文' in next_layer and '0 条回复' in next_layer


@pytest.mark.asyncio
async def test_loaded_layer_reads_fixed_metadata_and_bounded_body_with_excerpt_notice():
    root, reply = post(1), post(2)
    reply['relations'] = [{'type': 'reply_to', 'target': {'id': root['id']}}]
    ref = {'id': reply['id'], 'revision': reply['revision']}
    text = '界' * 2730 + 'ab'
    assert len(text.encode()) == 8192
    metadata = {key: value for key, value in reply.items() if key != 'content'} | {
        'state': 'active'
    }
    calls = []

    async def execute(packet):
        calls.append(packet)
        args = wire(packet.arguments)
        assert {key: args[key] for key in ref} == ref
        if packet.operation == 'discovery.get':
            assert 'content' not in args['fields']
            return SimpleNamespace(error=None, data=metadata)
        assert packet.operation == 'discovery.read_segment'
        assert args == {**ref, 'max_bytes': 8192}
        return SimpleNamespace(
            error=None,
            data={**ref, 'range': [0, 8192], 'text': text, 'next': '/_r/c/body-continuation'},
        )

    values = await branch_items(execute, {'items': [ref]}, 'https://msg.example')
    assert [packet.operation for packet in calls] == ['discovery.get', 'discovery.read_segment']
    assert len(values) == 1 and values[0]['content'] == text and values[0]['_body_more'] is True
    assert 'content' not in metadata and '_body_more' not in metadata
    fragment = fragment_html(
        {'root': root['id'], 'items': [{'id': root['id']}, *values]},
        focus_id=root['id'],
        query={'id': root['id'], 'limit': 8},
        statuses={reply['id']: {'reply_count': 0, 'count_complete': True}},
    )
    assert '正文节选' in fragment and '阅读完整帖子' in fragment
    assert '前 8192 字符' not in fragment


@pytest.mark.asyncio
async def test_loaded_layer_does_not_read_body_after_current_metadata_authorization_fails():
    calls = []

    async def execute(packet):
        calls.append(packet.operation)
        return SimpleNamespace(error=SimpleNamespace(code='permission_denied'), data=None)

    ref = {'id': post(2)['id'], 'revision': post(2)['revision']}
    assert await branch_items(execute, {'items': [ref]}, 'https://msg.example') == []
    assert calls == ['discovery.get']


@pytest.mark.asyncio
@pytest.mark.parametrize('overflow', [False, True])
async def test_markdown_title_segment_is_followed_without_exceeding_utf8_body_budget(overflow):
    reply = post(2)
    ref = {'id': reply['id'], 'revision': reply['revision']}
    metadata = {key: value for key, value in reply.items() if key != 'content'} | {
        'state': 'active'
    }
    prefix = '# 标题\n\n'
    tail = '界' * 2730 + 'ab' if overflow else '续读后才出现的完整回复正文。'
    prefix_bytes = len(prefix.encode())
    requests = []

    async def execute(packet):
        requests.append(packet)
        args = wire(packet.arguments)
        if packet.operation == 'discovery.get':
            assert 'content' not in args['fields']
            return SimpleNamespace(error=None, data=metadata)
        assert packet.operation == 'discovery.read_segment'
        if args == {**ref, 'max_bytes': 8192}:
            return SimpleNamespace(
                error=None,
                data={
                    **ref,
                    'range': [0, prefix_bytes],
                    'text': prefix,
                    'next': '/_r/c/reply-body',
                },
            )
        assert args == {'cursor': 'reply-body'}
        return SimpleNamespace(
            error=None,
            data={
                **ref,
                'range': [prefix_bytes, prefix_bytes + len(tail.encode())],
                'text': tail,
            },
        )

    values = await branch_items(execute, {'items': [ref]}, 'https://msg.example')
    assert len(requests) == 3 and len(values) == 1
    content = values[0]['content']
    expected = (prefix + tail).encode()[:8192].decode('utf-8', errors='ignore')
    assert content == expected and len(content.encode()) <= 8192
    assert values[0]['_body_more'] is overflow
    assert '\ufffd' not in content
    if not overflow:
        assert '续读后才出现的完整回复正文。' in content


@pytest.mark.asyncio
async def test_body_continuation_revocation_discards_the_whole_node():
    reply = post(2)
    ref = {'id': reply['id'], 'revision': reply['revision']}
    metadata = {key: value for key, value in reply.items() if key != 'content'} | {
        'state': 'active'
    }
    calls = []

    async def execute(packet):
        calls.append(packet)
        if packet.operation == 'discovery.get':
            return SimpleNamespace(error=None, data=metadata)
        args = wire(packet.arguments)
        if 'cursor' not in args:
            return SimpleNamespace(
                error=None,
                data={**ref, 'text': '# 原本可读的标题\n\n', 'next': '/_r/c/revoked'},
            )
        assert args == {'cursor': 'revoked'}
        return SimpleNamespace(error=SimpleNamespace(code='permission_denied'), data=None)

    values = await branch_items(execute, {'items': [ref]}, 'https://msg.example')
    assert values == []
    assert [packet.operation for packet in calls] == [
        'discovery.get',
        'discovery.read_segment',
        'discovery.read_segment',
    ]


@pytest.mark.asyncio
async def test_body_continuation_stops_at_eight_segments_and_marks_remaining_content():
    reply = post(2)
    ref = {'id': reply['id'], 'revision': reply['revision']}
    metadata = {key: value for key, value in reply.items() if key != 'content'} | {
        'state': 'active'
    }
    segments = []
    text = '这一段。\n\n'

    async def execute(packet):
        if packet.operation == 'discovery.get':
            return SimpleNamespace(error=None, data=metadata)
        args = wire(packet.arguments)
        index = len(segments)
        assert args == ({**ref, 'max_bytes': 8192} if not index else {'cursor': f'part-{index}'})
        segments.append(packet)
        return SimpleNamespace(
            error=None,
            data={**ref, 'text': text, 'next': f'/_r/c/part-{index + 1}'},
        )

    values = await branch_items(execute, {'items': [ref]}, 'https://msg.example')
    assert len(segments) == 8 and len(values) == 1
    assert values[0]['content'] == text * 8 and values[0]['_body_more'] is True
    assert len(values[0]['content'].encode()) <= 8192


@pytest.mark.asyncio
async def test_root_index_discards_a_post_when_title_read_authorization_has_changed():
    hidden, visible = post(1), post(2)
    hidden['name'] = '撤权后不应显示的名称.md'
    items = [
        {key: value for key, value in item.items() if key != 'content'}
        for item in (hidden, visible)
    ]
    title_reads = []

    async def execute(packet):
        if packet.operation == 'discovery.read_query':
            assert packet.contract_version == 5 and packet.arguments['post_kind'] == 'roots'
            return SimpleNamespace(error=None, data={'items': items})
        assert packet.operation == 'discovery.read_segment'
        args = wire(packet.arguments)
        title_reads.append(args['id'])
        assert args['max_bytes'] == 1024
        if args['id'] == hidden['id']:
            return SimpleNamespace(error=SimpleNamespace(code='permission_denied'), data=None)
        assert args['revision'] == visible['revision']
        return SimpleNamespace(error=None, data={'text': '# 仍可读的主帖\n\n摘要'})

    result = await root_post_index(execute, 'https://msg.example')
    assert set(title_reads) == {hidden['id'], visible['id']}
    assert len(result['items']) == 1
    assert result['items'][0]['id'] == visible['id']
    assert result['items'][0]['title'] == '仍可读的主帖'
    assert hidden['id'] not in str(result) and hidden['name'] not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error_code', ['permission_denied', 'certificate_gate', 'credential_revoked']
)
async def test_non_ceiling_failures_never_fallback_to_anonymous_status(error_code):
    calls = []

    async def current(packet):
        calls.append(('current', packet.operation))
        return SimpleNamespace(error=SimpleNamespace(code=error_code), data=None)

    async def public(packet):
        pytest.fail('非凭据上限错误不能改用匿名身份重试')

    result = await reply_statuses(
        current, [post(1)['id']], 'https://msg.example', public_execute=public
    )
    assert result == {} and calls == [('current', 'discussion.reply_status')]


@pytest.mark.asyncio
async def test_ceiling_fallback_reexecutes_the_read_and_marks_public_numbers_explicitly():
    rid = post(1)['id']
    calls = []

    async def current(packet):
        calls.append(('current', packet))
        return SimpleNamespace(error=SimpleNamespace(code='credential_ceiling'), data=None)

    async def public(packet):
        calls.append(('public', packet))
        return SimpleNamespace(
            error=None,
            data={
                'items': [{'id': rid, 'reply_count': 2, 'count_complete': True, 'view_count': 1}]
            },
        )

    result = await reply_statuses(current, [rid], 'https://msg.example', public_execute=public)
    assert [label for label, _ in calls] == ['current', 'public']
    assert all(packet.operation == 'discussion.reply_status' for _, packet in calls)
    assert wire(calls[0][1].arguments) == wire(calls[1][1].arguments) == {'ids': [rid]}
    assert calls[1][1].subject is None and calls[1][1].proof is None
    assert result[rid]['public_only'] is True and reply_label(result[rid]) == '公开 2 条回复'


def test_discussion_script_is_exactly_pinned_and_only_added_for_a_panel():
    digest = b64encode(sha256(SCRIPT.encode()).digest()).decode()
    assert HASH == digest
    assert "'sha256-" + digest + "'" in HOME_BROWSER_HEADERS['Content-Security-Policy']
    script_policy = next(
        directive
        for directive in HOME_BROWSER_HEADERS['Content-Security-Policy'].split(';')
        if directive.strip().startswith('script-src ')
    )
    assert 'unsafe-inline' not in script_policy
    ordinary = document_html('# 一般文档').decode()
    assert '<script>' + SCRIPT + '</script>' not in ordinary
    panel = discussion_panel_html(post(1), None)
    page = document_html('# 有讨论的帖子', discussion_html=panel).decode()
    assert page.count('<script>' + SCRIPT + '</script>') == 1


def test_read_controls_have_distinct_routes_and_accessible_status():
    resource = post(1)
    panel = discussion_panel_html(resource, None)
    dom = Elements(panel)
    assert dom.matching('data-thread-panel')[0]['data-post-id'] == resource['id']
    attrs = dom.matching('data-thread-panel')[0]
    body_url, number_url = urlsplit(attrs['data-thread-url']), urlsplit(attrs['data-status-url'])
    assert body_url.path == '/_post/thread-fragment' and number_url.path == '/_post/reply-status'
    assert parse_qs(body_url.query) == {'id': [resource['id']], 'limit': ['8']}
    assert parse_qs(number_url.query) == {'id': [resource['id']]}
    assert len(dom.matching('data-thread-refresh')) == 1
    assert dom.matching('data-thread-status')[0]['role'] == 'status'
    assert dom.matching('data-thread-status')[0]['aria-live'] == 'polite'
    assert dom.matching('data-thread-expand')[0]['type'] == 'button'
    assert dom.matching('data-thread-refresh')[0]['type'] == 'button'
