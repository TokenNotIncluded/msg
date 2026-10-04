"""内嵌讨论沿用真实 PostgreSQL、当前授权和既有浏览器写入边界。"""

from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import pytest
from test_oauth import browser_login, oauth as oauth
from test_service import call, register

from msg.core.codec import canonical, wire
from msg.core.identifiers import hex_id
from msg.core.models import OperationError
from msg.security.oauth import OAuthService
from msg.transports import thread_browser
from msg.transports.http import create_app
from msg.transports.oauth_http import csrf


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.items = []
        self.elements = []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        if tag == 'a':
            self.current = [dict(attrs), '']

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.items.append(self.current)
            self.current = None


async def counts(app):
    async with app.metadata.transaction(write=False) as tx:
        return {
            table: tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('resources', 'revisions', 'events', 'results', 'audit', 'jobs')
        }


def assert_fixed_body_reads(requests, segments, ref, body):
    body_reads = [packet for packet in requests if packet.operation == 'discovery.read_segment']
    assert 1 <= len(body_reads) == len(segments) <= 8
    assert dict(body_reads[0].arguments) == {
        'id': ref.id,
        'revision': ref.revision,
        'max_bytes': 8192,
    }
    offset = 0
    encoded = body.encode()
    for index, (packet, segment) in enumerate(zip(body_reads, segments, strict=True)):
        assert segment['id'] == ref.id and segment['revision'] == ref.revision
        assert segment['range'][0] == offset
        offset = segment['range'][1]
        assert 0 < len(segment['text'].encode()) <= 8192
        assert segment['text'].encode() == encoded[segment['range'][0] : offset]
        if index:
            assert dict(packet.arguments) == {
                'cursor': segments[index - 1]['next'].removeprefix('/_r/c/')
            }


async def restrict_browser_reads(app, cookie, subject):
    async with app.metadata.transaction(write=True) as tx:
        _, credential_id, _ = await OAuthService(app).browser_credentials(tx, cookie)
        credential = await tx.credential(credential_id)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(
                        grant,
                        operations=frozenset(
                            operation
                            for operation in grant.operations
                            if operation.partition('@')[0] != 'discussion.reply_status'
                            and operation != 'discovery.read_query@3'
                        ),
                    )
                    for grant in credential.ceiling
                ),
            ),
            (await tx.subject(subject)).auth_version,
        )


async def post_tree(app, *, handle='panel-author', replies=2):
    key, subject, cert = await register(app, handle)

    async def write(operation, arguments):
        result = await call(app, operation, arguments, key=key, subject=subject, certs=(cert,))
        assert result.status == 'ok', wire(result)
        return result.resources[0]

    root = await write(
        'content.post_create',
        {'parent': '/main', 'name': 'panel-root.md', 'body': '# 主帖标题\n\n主帖正文只显示一次。'},
    )
    children = [
        await write(
            'discussion.reply',
            {
                'target': {'id': root.id},
                'body': f'# 回复标题 {index}\n\n可直接阅读的回复正文 {index}。',
            },
        )
        for index in range(replies)
    ]
    return root, children, write


@pytest.fixture
async def panel_http(installed):
    app, _ = installed
    asgi = create_app(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url=app.settings.service_url
    ) as http:
        try:
            yield app, http
        finally:
            await asgi.state.home_cache.close()


@pytest.mark.asyncio
async def test_home_and_post_show_only_reply_counts_without_loading_thread_bodies(
    panel_http, monkeypatch
):
    app, http = panel_http
    root, replies, _ = await post_tree(app)
    path = '/*' + hex_id(root.id)
    post_before = await http.get(path + '/json')
    home_before = await call(
        app, 'discovery.read_query', {'home_summary': True}, contract_version=4
    )
    assert home_before.status == 'ok', wire(home_before)
    before = await counts(app)
    operations = []
    execute = app.executor.execute

    async def observed(packet, **kwargs):
        operations.append(packet.operation)
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', observed)

    page = await http.get(path, headers={'Accept': 'text/html'})
    assert page.status_code == 200, page.text
    assert page.text.count('<p>主帖正文只显示一次。</p>') == 1
    assert 'id="discussion"' in page.text and 'data-thread-panel' in page.text
    assert '<span class="discussion-count">2 条回复</span>' in page.text
    assert all(f'可直接阅读的回复正文 {index}。' not in page.text for index in range(2))
    assert all('id="reply-' + hex_id(reply.id) + '"' not in page.text for reply in replies)
    assert 'id="discussion-content" data-thread-content hidden></div>' in page.text
    assert 'private, no-store' == page.headers['cache-control']
    assert page.headers['vary'] == 'Accept, Cookie'
    assert "default-src 'none'" in page.headers['content-security-policy']

    home = await http.get('/', headers={'Accept': 'text/html'})
    assert home.status_code == 200, home.text
    links = Links(home.text).items
    assert any(
        attrs.get('href') == path + '#discussion' and text == '2 条回复' for attrs, text in links
    )
    assert 'discussion.reply_status' in operations and 'discussion.thread' not in operations
    assert (await http.get(path + '/json')).json() == post_before.json()
    home_after = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    assert canonical(home_after.data) == canonical(home_before.data)
    assert all(
        set(item) == {'name', 'title', 'excerpt', 'view_count', 'path', 'created_at'}
        for item in home_after.data['latest']
    )
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_topic_browser_pages_only_root_posts_while_raw_json_and_home_v4_stay_complete(
    panel_http, monkeypatch
):
    app, http = panel_http
    _, _, write = await post_tree(app, replies=0)
    home_baseline = await call(
        app, 'discovery.read_query', {'home_summary': True}, contract_version=4
    )
    assert home_baseline.status == 'ok', wire(home_baseline)
    topic = await write('content.topic_create', {'parent': '/main', 'name': 'panel-roots'})
    roots = [
        await write(
            'content.post_create',
            {
                'parent': topic.id,
                'name': f'root-{index}.md',
                'body': f'# 独立主帖 {index}\n\n主帖正文 {index}。',
            },
        )
        for index in range(21)
    ]
    replies = [
        await write(
            'discussion.reply',
            {'target': {'id': roots[0].id}, 'body': f'# 不占列表的评论 {index}\n\n评论正文。'},
        )
        for index in range(2)
    ]
    path = '/*' + hex_id(topic.id)
    raw_before = await http.get(path + '?format=raw')
    json_before = await http.get(path + '/json')
    assert raw_before.status_code == json_before.status_code == 200
    assert (await http.get(path + '/raw')).status_code == 404
    assert {hex_id(reply.id) for reply in replies} <= {
        hex_id(item['id']) for item in json_before.json()['items']
    }
    assert all(f'不占列表的评论 {index}' in raw_before.text for index in range(2))
    home_before = await call(
        app, 'discovery.read_query', {'home_summary': True}, contract_version=4
    )
    assert home_before.status == 'ok', wire(home_before)
    assert home_before.data['posts'] == home_baseline.data['posts'] + len(roots) + len(replies)
    before = await counts(app)
    requests = []
    execute = app.executor.execute

    async def observed(packet, **kwargs):
        requests.append(packet)
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', observed)
    expected = {'/*' + hex_id(root.id) for root in roots}
    seen = set()
    pages = []
    next_path = path
    while next_path:
        assert len(pages) < 3
        response = await http.get(next_path, headers={'Accept': 'text/html'})
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'private, no-store'
        assert response.headers['vary'] == 'Accept, Cookie'
        links = Links(response.text).items
        current = {attrs.get('href') for attrs, _ in links} & expected
        assert current and not (current & seen)
        seen |= current
        pages.append(current)
        assert all(f'不占列表的评论 {index}' not in response.text for index in range(2))
        assert all('/*' + hex_id(reply.id) not in response.text for reply in replies)
        continuations = [attrs['href'] for attrs, text in links if text == '继续查看主帖']
        assert len(continuations) <= 1
        next_path = continuations[0] if continuations else None
        if next_path:
            parsed = urlsplit(next_path)
            assert parsed.path == path
            assert set(parse_qs(parsed.query)) == {'post_cursor'}
    assert [len(page) for page in pages] == [20, 1]
    assert seen == expected
    indexes = [
        packet
        for packet in requests
        if packet.operation == 'discovery.read_query' and packet.contract_version == 5
    ]
    assert len(indexes) == 2
    assert indexes[0].arguments['parent'] == topic.id
    assert indexes[0].arguments['post_kind'] == 'roots'
    assert set(indexes[1].arguments) == {'cursor'}
    assert not any(packet.operation == 'discussion.thread' for packet in requests)
    assert (await http.get(path + '?format=raw')).content == raw_before.content
    assert (await http.get(path + '/raw')).status_code == 404
    assert (await http.get(path + '/json')).json() == json_before.json()
    home_after = await call(app, 'discovery.read_query', {'home_summary': True}, contract_version=4)
    assert canonical(home_after.data) == canonical(home_before.data)
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_topic_html_rejects_valid_same_topic_cursor_with_different_projection(panel_http):
    app, http = panel_http
    _, _, write = await post_tree(app, replies=0)
    topic = await write('content.topic_create', {'parent': '/main', 'name': 'panel-cursor'})
    for index in range(21):
        await write(
            'content.post_create',
            {'parent': topic.id, 'name': f'root-{index}.md', 'body': f'游标主帖 {index}。'},
        )
    page = await call(
        app,
        'discovery.read_query',
        {
            'parent': topic.id,
            'type': 'post',
            'post_kind': 'roots',
            'sort': 'time',
            'direction': 'desc',
            'limit': 20,
            'fields': ['id'],
        },
        contract_version=5,
    )
    assert page.status == 'ok' and page.data.get('next'), wire(page)
    cursor = page.data['cursor']
    continuation = await call(app, 'discovery.read_query', {'cursor': cursor}, contract_version=5)
    assert continuation.status == 'ok', wire(continuation)
    assert len(continuation.data['items']) == 1
    assert set(continuation.data['items'][0]) == {'id'}
    before = await counts(app)
    response = await http.get(
        '/*' + hex_id(topic.id) + '?' + urlencode({'post_cursor': cursor}),
        headers={'Accept': 'text/html'},
    )
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'cursor_query_mismatch'
    assert response.headers['cache-control'] == 'no-store'
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_fragment_rejects_valid_same_focus_cursor_with_different_projection(panel_http):
    app, http = panel_http
    root, _, _ = await post_tree(app, replies=9)
    page = await call(
        app,
        'discovery.read_query',
        {'parent': root.id, 'collection': 'replies', 'limit': 8, 'fields': ['id']},
        contract_version=3,
    )
    assert page.status == 'ok' and page.data['pageInfo']['hasNextPage'], wire(page)
    cursor = page.data['pageInfo']['endCursor']
    continuation = await call(app, 'discovery.read_query', {'cursor': cursor}, contract_version=3)
    assert continuation.status == 'ok', wire(continuation)
    assert len(continuation.data['items']) == 1
    assert set(continuation.data['items'][0]) == {'id'}
    before = await counts(app)
    response = await http.get(
        '/_post/thread-fragment?' + urlencode({'id': root.id, 'cursor': cursor})
    )
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'cursor_query_mismatch'
    assert response.headers['cache-control'] == 'no-store'
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_home_root_index_failure_is_no_store_and_never_reuses_old_etag(
    panel_http, monkeypatch
):
    app, http = panel_http
    await post_tree(app, replies=0)
    first = await http.get('/', headers={'Accept': 'text/html'})
    assert first.status_code == 200 and 'etag' in first.headers
    before = await counts(app)
    failures = []
    execute = app.executor.execute

    async def unavailable(packet, **kwargs):
        result = await execute(packet, **kwargs)
        if packet.operation != 'discovery.read_query' or packet.contract_version != 5:
            return result
        assert result.status == 'ok', wire(result)
        failures.append(packet)
        return replace(
            result,
            status='error',
            data=None,
            receipt=None,
            error=OperationError(code='server_busy', retryable=True),
        )

    monkeypatch.setattr(app.executor, 'execute', unavailable)
    headers = {'Accept': 'text/html', 'If-None-Match': first.headers['etag']}
    failed = await http.get('/', headers=headers)
    assert failed.status_code == 200
    assert failed.headers['cache-control'] == 'private, no-store'
    assert failed.headers['vary'] == 'Accept, Cookie'
    assert 'etag' not in failed.headers
    assert '主帖列表暂时无法读取，请稍后刷新。' in failed.text
    head = await http.head('/', headers=headers)
    assert head.status_code == 200 and not head.content
    assert head.headers['cache-control'] == 'private, no-store'
    assert 'etag' not in head.headers
    assert head.headers['content-length'] == str(len(failed.content))
    assert len(failures) == 2
    assert await counts(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('reference_style', ['id', 'path'])
async def test_fragment_head_and_continuation_stay_inline_bounded_and_read_only(
    panel_http, reference_style
):
    app, http = panel_http
    root, replies, _ = await post_tree(app, replies=3)
    before = await counts(app)
    reference = root.id if reference_style == 'id' else '/*' + hex_id(root.id)
    next_path = '/_post/thread-fragment?' + urlencode({'id': reference, 'limit': 2})
    seen_paths = set()
    pages = []
    rejected_wrong_focus = False
    while next_path:
        assert next_path not in seen_paths and len(seen_paths) < 10
        seen_paths.add(next_path)
        page = await http.get(next_path, headers={'Accept': 'text/html'})
        assert page.status_code == 200, page.text
        assert page.headers['content-type'].startswith('text/html')
        assert page.headers['cache-control'] == 'private, no-store'
        assert page.headers['vary'] == 'Accept, Cookie'
        assert 'data-thread-fragment' in page.text
        assert '<html' not in page.text and '<script' not in page.text
        assert '主帖正文只显示一次。' not in page.text
        assert page.text.count('<article>') <= 2
        head = await http.head(next_path, headers={'Accept': 'text/html'})
        assert head.status_code == 200 and not head.content
        assert head.headers['content-length'] == str(len(page.content))
        assert head.headers['cache-control'] == 'private, no-store'
        pages.append(page.text)
        continuations = [
            attrs['href'] for attrs, text in Links(page.text).items if text == '继续阅读回复'
        ]
        assert len(continuations) <= 1
        next_path = continuations[0] if continuations else None
        if next_path:
            parsed = urlsplit(unescape(next_path))
            query = parse_qs(parsed.query)
            assert not parsed.netloc and parsed.path == '/_post/thread-fragment'
            assert query['id'] == [reference] and query['limit'] == ['2'] and query.get('cursor')
            if not rejected_wrong_focus:
                cursor = query['cursor'][0]
                for wrong_query in (
                    {'id': replies[0].id, 'limit': 2, 'cursor': cursor},
                    {'id': reference, 'limit': 1, 'cursor': cursor},
                ):
                    denied = await http.get('/_post/thread-fragment?' + urlencode(wrong_query))
                    assert denied.status_code == 400, denied.text
                    assert denied.json()['error']['code'] == 'cursor_query_mismatch'
                rejected_wrong_focus = True
    rendered = ''.join(pages)
    for index, reply in enumerate(replies):
        assert rendered.count('id="reply-' + hex_id(reply.id) + '"') == 1
        assert rendered.count(f'可直接阅读的回复正文 {index}。') == 1
    assert rejected_wrong_focus
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_reply_status_returns_only_authorized_numbers_with_get_head_and_no_business_write(
    panel_http, monkeypatch
):
    app, http = panel_http
    root, replies, _ = await post_tree(app)
    before = await counts(app)
    operations = []
    execute = app.executor.execute

    async def observed(packet, **kwargs):
        operations.append(packet.operation)
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', observed)
    url = '/_post/reply-status?' + urlencode({'id': '/*' + hex_id(root.id)})
    response = await http.get(url)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.headers['vary'] == 'Accept, Cookie'
    data = response.json()['data']
    assert data['id'] == root.id and data['reply_label'] == '2 条回复'
    status = data['status']
    assert status['reply_count'] == 2 and status['count_complete'] is True
    assert not {'thread_root', 'thread_count', 'thread_complete'} & set(status)
    assert isinstance(status['view_count'], int) and status['view_count'] >= 0
    assert not {'recent', 'summary', 'content', 'body', 'title', 'path'} & set(status)
    assert all(hex_id(reply.id) not in response.text for reply in replies)
    assert '主帖正文只显示一次。' not in response.text and '回复标题' not in response.text
    head = await http.head(url)
    assert head.status_code == 200 and not head.content
    assert head.headers['content-length'] == str(len(response.content))
    assert head.headers['cache-control'] == 'private, no-store'
    assert 'discussion.reply_status' in operations and 'discussion.thread' not in operations
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_fragment_reads_only_direct_layer_and_next_layer_requires_its_own_request(
    panel_http, monkeypatch
):
    app, http = panel_http
    root, replies, write = await post_tree(app, replies=1)
    child = replies[0]
    grandchild = await write(
        'discussion.reply',
        {'target': {'id': child.id}, 'body': '# 第三层标题\n\n第三层正文只在展开父回复后读取。'},
    )
    before = await counts(app)
    requests = []
    segments = []
    execute = app.executor.execute

    async def observed(packet, **kwargs):
        requests.append(packet)
        result = await execute(packet, **kwargs)
        if packet.operation == 'discovery.read_segment':
            assert result.status == 'ok', wire(result)
            segments.append(wire(result.data))
        return result

    monkeypatch.setattr(app.executor, 'execute', observed)
    first = await http.get('/_post/thread-fragment?id=' + root.id)
    assert first.status_code == 200, first.text
    assert '可直接阅读的回复正文 0。' in first.text
    assert '第三层正文只在展开父回复后读取。' not in first.text
    assert hex_id(grandchild.id) not in first.text and '主帖正文只显示一次。' not in first.text
    assert not any(packet.operation == 'discussion.thread' for packet in requests)
    page_reads = [packet for packet in requests if packet.operation == 'discovery.read_query']
    assert len(page_reads) == 1
    assert page_reads[0].contract_version == 3
    assert dict(page_reads[0].arguments) == {
        'parent': root.id,
        'collection': 'replies',
        'fields': ('id', 'revision'),
        'limit': 8,
    }
    metadata_reads = [packet for packet in requests if packet.operation == 'discovery.get']
    assert all('content' not in packet.arguments['fields'] for packet in metadata_reads)
    assert_fixed_body_reads(requests, segments, child, '# 回复标题 0\n\n可直接阅读的回复正文 0。')
    branches = [
        attributes
        for tag, attributes in Links(first.text).elements
        if tag == 'details' and 'data-thread-branch' in attributes
    ]
    assert len(branches) == 1 and branches[0]['data-post-id'] == child.id
    assert 'open' not in branches[0]

    requests.clear()
    segments.clear()
    second = await http.get('/_post/thread-fragment?id=' + child.id)
    assert second.status_code == 200, second.text
    assert '第三层正文只在展开父回复后读取。' in second.text
    assert '可直接阅读的回复正文 0。' not in second.text
    assert '主帖正文只显示一次。' not in second.text
    page_reads = [packet for packet in requests if packet.operation == 'discovery.read_query']
    assert len(page_reads) == 1 and page_reads[0].arguments['parent'] == child.id
    assert_fixed_body_reads(
        requests, segments, grandchild, '# 第三层标题\n\n第三层正文只在展开父回复后读取。'
    )
    assert not any(packet.operation == 'discussion.thread' for packet in requests)
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_long_reply_body_uses_fixed_revision_segment_with_byte_budget_and_excerpt_marker(
    panel_http, monkeypatch
):
    app, http = panel_http
    root, _, write = await post_tree(app, replies=0)
    body = '汉字🙂' * 4000 + '\n\n末尾不能在第一段被读取。'
    child = await write('discussion.reply', {'target': {'id': root.id}, 'body': body})
    requests = []
    segments = []
    displayed = []
    execute = app.executor.execute
    render = thread_browser.fragment_html

    async def observed(packet, **kwargs):
        requests.append(packet)
        result = await execute(packet, **kwargs)
        if packet.operation == 'discovery.read_segment':
            assert result.status == 'ok', wire(result)
            segments.append(wire(result.data))
        return result

    monkeypatch.setattr(app.executor, 'execute', observed)

    def observed_render(data, **kwargs):
        displayed.extend(item for item in data['items'] if item['id'] == child.id)
        return render(data, **kwargs)

    monkeypatch.setattr(thread_browser, 'fragment_html', observed_render)
    before = await counts(app)
    response = await http.get('/_post/thread-fragment?id=' + root.id)
    assert response.status_code == 200, response.text
    assert_fixed_body_reads(requests, segments, child, body)
    segment = segments[0]
    assert segment['id'] == child.id and segment['revision'] == child.revision
    assert 0 < len(segment['text'].encode()) <= 8192
    assert segment['range'][0] == 0 and segment['range'][1] <= 8192
    assert segment['text'].encode() == body.encode()[: segment['range'][1]]
    assert segment.get('next') and segment['range'][1] < len(body.encode())
    assert len(displayed) == 1
    shown = displayed[0]['content'].encode()
    assert 0 < len(shown) <= 8192 and shown == body.encode()[: len(shown)]
    assert displayed[0]['_body_more'] is True
    assert '正文节选' in response.text and '阅读完整帖子' in response.text
    assert '末尾不能在第一段被读取。' not in response.text
    assert not any(packet.operation == 'discussion.thread' for packet in requests)
    metadata_reads = [packet for packet in requests if packet.operation == 'discovery.get']
    assert all('content' not in packet.arguments['fields'] for packet in metadata_reads)
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_fragment_filters_hidden_parent_and_rechecks_root_access(panel_http):
    app, http = panel_http
    root, replies, write = await post_tree(app)
    hidden = replies[1]
    child = await write(
        'discussion.reply',
        {'target': {'id': hidden.id}, 'body': '# 可读子帖\n\n可读子帖不暴露隐藏父帖。'},
    )
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden.id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    before = await counts(app)
    url = '/_post/thread-fragment?id=' + root.id
    fragment = await http.get(url)
    assert fragment.status_code == 200, fragment.text
    assert 'data-reply-label="1 条回复"' in fragment.text
    assert '可读子帖不暴露隐藏父帖。' not in fragment.text
    assert 'id="reply-' + hex_id(child.id) + '"' not in fragment.text
    assert '回复标题 1' not in fragment.text and hex_id(hidden.id) not in fragment.text
    hidden_focus = await http.get('/_post/thread-fragment?id=' + hidden.id)
    assert hidden_focus.status_code == 403
    page = await http.get('/*' + hex_id(root.id), headers={'Accept': 'text/html'})
    assert page.status_code == 200
    assert '<span class="discussion-count">1 条回复</span>' in page.text
    assert hex_id(hidden.id) not in page.text and '回复标题 1' not in page.text
    assert await counts(app) == before

    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(root.id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    revoked = await counts(app)
    for method in (http.get, http.head):
        denied = await method(url)
        assert denied.status_code == 403 and 'etag' not in denied.headers
        denied_count = await method('/_post/reply-status?id=' + root.id)
        assert denied_count.status_code == 403 and 'etag' not in denied_count.headers
    denied_post = await http.get(
        '/*' + hex_id(root.id),
        headers={'Accept': 'text/html', 'If-None-Match': page.headers['etag']},
    )
    assert denied_post.status_code == 403 and 'etag' not in denied_post.headers
    assert await counts(app) == revoked


@pytest.mark.asyncio
async def test_fragment_rejects_ambiguous_queries_and_explicit_execution_headers(panel_http):
    app, http = panel_http
    root, _, _ = await post_tree(app, replies=0)
    before = await counts(app)
    base = '/_post/thread-fragment?id=' + root.id
    rejected = [
        ('/_post/thread-fragment', 'unknown_query_parameter'),
        (base + '&id=' + root.id, 'duplicate_query_parameter'),
        (base + '&unknown=1', 'unknown_query_parameter'),
        (base + '&limit=0', 'invalid_limit'),
        (base + '&limit=11', 'invalid_limit'),
        (base + '&limit=21', 'invalid_limit'),
        (base + '&limit=-1', 'invalid_limit'),
        (base + '&limit=1.5', 'invalid_limit'),
        (base + '&limit=0001', 'invalid_limit'),
        (base + '&limit=%EF%BC%91', 'invalid_limit'),
    ]
    for url, code in rejected:
        response = await http.get(url)
        assert response.status_code == 400 and response.json()['error']['code'] == code
        assert 'no-store' in response.headers['cache-control']
    bad_cursor = await http.get(base + '&cursor=not-a-cursor')
    assert bad_cursor.status_code == 400, bad_cursor.text
    for header in ({'Authorization': 'Bearer rejected'}, {'X-Msg-Request': '{}'}):
        response = await http.get(base, headers=header)
        assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_request'
    response = await http.post(
        base, json={'operation': 'discussion.reply', 'body': 'No implicit write'}
    )
    assert response.status_code == 405
    status_base = '/_post/reply-status?id=' + root.id
    for url, code in [
        ('/_post/reply-status', 'unknown_query_parameter'),
        (status_base + '&id=' + root.id, 'duplicate_query_parameter'),
        (status_base + '&limit=1', 'unknown_query_parameter'),
        (status_base + '&cursor=invalid', 'unknown_query_parameter'),
        (status_base + '&operation=discussion.reply', 'unknown_query_parameter'),
    ]:
        response = await http.get(url)
        assert response.status_code == 400 and response.json()['error']['code'] == code
        assert 'no-store' in response.headers['cache-control']
    for header in ({'Authorization': 'Bearer rejected'}, {'X-Msg-Request': '{}'}):
        response = await http.get(status_base, headers=header)
        assert response.status_code == 400 and response.json()['error']['code'] == 'invalid_request'
    response = await http.post(status_base, json={'body': 'No implicit write'})
    assert response.status_code == 405
    assert await counts(app) == before


@pytest.mark.asyncio
async def test_browser_reply_remains_explicit_csrf_write_and_read_ceiling_is_not_widened(oauth):
    app, key, subject, http = oauth
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# 浏览器主帖\n\n正文。'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok', wire(created)
    root = created.resources[0]
    cookie = await browser_login(oauth)
    url = '/_post/thread-fragment?id=' + root.id
    before = await counts(app)
    fragment = await http.get(url)
    assert fragment.status_code == 200, fragment.text
    assert await counts(app) == before
    payload = {
        'csrf': 'invalid',
        'id': root.id,
        'revision': root.revision,
        'operation': 'discussion.reply',
        'request_id': uuid4().hex,
        'body': '只有显式发送后才出现的回复。',
    }
    denied = await http.post(
        '/oauth/post-action', json=payload, headers={'Origin': app.settings.service_url}
    )
    assert denied.status_code == 400, denied.text
    assert await counts(app) == before
    accepted = await http.post(
        '/oauth/post-action',
        json={**payload, 'csrf': csrf(cookie)},
        headers={'Origin': app.settings.service_url},
    )
    assert accepted.status_code == 200, accepted.text
    committed = await counts(app)
    refreshed = await http.get(url)
    assert refreshed.status_code == 200 and payload['body'] in refreshed.text
    assert await counts(app) == committed

    await restrict_browser_reads(app, cookie, subject)
    fenced = await counts(app)
    denied = await http.get(url)
    assert denied.status_code == 403 and denied.json()['error']['code'] == 'credential_ceiling'
    assert payload['body'] not in denied.text
    assert await counts(app) == fenced


@pytest.mark.asyncio
async def test_limited_browser_cookie_uses_only_current_public_counts_and_never_private_data(
    oauth, monkeypatch
):
    app, key, subject, http = oauth

    async def write(operation, arguments):
        result = await call(app, operation, arguments, key=key, subject=subject)
        assert result.status == 'ok', wire(result)
        return result.resources[0]

    root = await write(
        'content.post_create', {'parent': '/main', 'body': '# 公开主帖\n\n公开正文。'}
    )
    await write('discussion.reply', {'target': {'id': root.id}, 'body': '公开回复正文。'})
    hidden = await write('discussion.reply', {'target': {'id': root.id}, 'body': '私有回复正文。'})
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(hidden.id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    cookie = await browser_login(oauth)
    await restrict_browser_reads(app, cookie, subject)
    before = await counts(app)
    requests = []
    execute = app.executor.execute

    async def observed(packet, **kwargs):
        requests.append((packet.operation, packet.subject))
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', observed)
    url = '/_post/reply-status?id=' + root.id
    response = await http.get(url)
    assert response.status_code == 200, response.text
    data = response.json()['data']
    assert data['status']['public_only'] is True
    assert data['status']['reply_count'] == 1
    assert not {'thread_root', 'thread_count', 'thread_complete'} & set(data['status'])
    assert data['reply_label'] == '公开 1 条回复'
    assert hex_id(hidden.id) not in response.text and '私有回复正文。' not in response.text
    assert [
        identity for operation, identity in requests if operation == 'discussion.reply_status'
    ] == [
        subject,
        None,
    ]
    assert await counts(app) == before

    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(root.id)
        await tx.replace(
            replace(resource, mode=0o600, generation=resource.generation + 1), resource.generation
        )
    fenced = await counts(app)
    requests.clear()
    private = await http.get(url)
    assert private.status_code == 503, private.text
    assert private.json()['data']['status'] is None
    assert '私有回复正文。' not in private.text and hex_id(hidden.id) not in private.text
    assert 'reply_count' not in private.text and 'thread_count' not in private.text
    assert [
        identity for operation, identity in requests if operation == 'discussion.reply_status'
    ] == [
        subject,
        None,
    ]
    assert await counts(app) == fenced


@pytest.mark.asyncio
@pytest.mark.parametrize('error_code', ['permission_denied', 'server_busy'])
async def test_non_ceiling_count_failures_do_not_fall_back_to_anonymous(
    oauth, monkeypatch, error_code
):
    app, key, subject, http = oauth
    result = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': '# 不降级主帖\n\n正文。'},
        key=key,
        subject=subject,
    )
    assert result.status == 'ok', wire(result)
    root = result.resources[0]
    await browser_login(oauth)
    requests = []
    execute = app.executor.execute

    async def unavailable(packet, **kwargs):
        result = await execute(packet, **kwargs)
        if packet.operation != 'discussion.reply_status':
            return result
        assert result.status == 'ok', wire(result)
        requests.append(packet.subject)
        # 在真实认证与只读事务之后注入上游失败，检验降级条件，不伪造主体或权限。
        return replace(
            result,
            status='error',
            data=None,
            receipt=None,
            error=OperationError(code=error_code, retryable=error_code == 'server_busy'),
        )

    monkeypatch.setattr(app.executor, 'execute', unavailable)
    before = await counts(app)
    response = await http.get('/_post/reply-status?id=' + root.id)
    assert response.status_code == 503 and response.json()['data']['status'] is None
    assert requests == [subject]
    assert '正文。' not in response.text and 'reply_count' not in response.text
    assert await counts(app) == before
