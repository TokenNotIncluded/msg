"""回复树不推测缺失父帖，也不会被坏关系绕成环。"""

import pytest

from msg.transports.post_read_pages import thread_fragment_html, thread_read_html
from msg.transports.thread_tree import thread_tree


def post(number, *parents, content='正文'):
    rid = 'r_' + f'{number:032x}'
    return {
        'id': rid,
        'name': f'post-{number}.md',
        'path': f'/main/post-{number}.md',
        'content': content,
        'relations': [
            {'type': 'reply_to', 'target': {'id': 'r_' + f'{parent:032x}'}} for parent in parents
        ],
    }


def key(number):
    return f'{number:032x}'


def test_single_parent_branches_keep_order_and_ignore_other_relations():
    root, branch, sibling, reply = post(1), post(2, 1), post(3, 1), post(4, 2)
    reply['relations'].extend([
        {'type': 'thread_root', 'target': {'id': root['id']}},
        {'type': 'fork_of', 'target': {'id': sibling['id']}},
    ])
    nodes, roots = thread_tree([root, branch, sibling, reply], root['id'])
    assert roots == (key(1),)
    assert nodes[key(1)].children == (key(2), key(3))
    assert nodes[key(2)].children == (key(4),)
    assert nodes[key(4)].parent == key(2)
    assert all(not node.invalid and not node.incomplete for node in nodes.values())


@pytest.mark.parametrize('parents', [(2,), (1, 3), ()])
def test_self_multiple_or_missing_parents_are_detached(parents):
    nodes, roots = thread_tree([post(1), post(2, *parents), post(3, 1)], key(1))
    assert nodes[key(2)].invalid and nodes[key(2)].parent is None
    assert roots == (key(1), key(2))
    assert nodes[key(1)].children == (key(3),)


def test_cycle_is_broken_without_discarding_posts_or_safe_descendants():
    nodes, roots = thread_tree([post(1), post(2, 3), post(3, 2), post(4, 2)], key(1))
    assert roots == (key(1), key(2), key(3))
    assert nodes[key(2)].invalid and nodes[key(3)].invalid
    assert nodes[key(2)].children == (key(4),)
    page = thread_read_html(
        [node.item for node in nodes.values()], {'root': key(1)}, path='/thread'
    ).decode()
    assert page.count('<article>') == 4 and '部分回复关系异常' in page


def test_partial_page_does_not_attach_missing_parents_to_the_root_or_link_them():
    nodes, roots = thread_tree([post(1), post(3, 2), post(4, 3)], key(1))
    assert roots == (key(1), key(3))
    assert nodes[key(3)].incomplete and nodes[key(3)].parent is None
    assert nodes[key(1)].children == () and nodes[key(3)].children == (key(4),)
    page = thread_read_html(
        [node.item for node in nodes.values()], {'root': key(1)}, path='/thread'
    ).decode()
    assert '父帖不在当前页' in page and f'href="#reply-{key(2)}"' not in page


def test_nested_html_contains_readable_body_and_native_branch_controls():
    items = [post(1), post(2, 1), post(3, 2, content='最后的验证结果。')]
    page = thread_read_html(items, {'root': key(1)}, path='/thread').decode()
    assert page.count('<article>') == 3
    assert '<p>最后的验证结果。</p>' in page
    assert page.count('class="thread-branch"') == 3
    assert 'class="thread-branch" open' not in page
    assert f'href="#reply-{key(2)}"' in page and 'data-depth="2"' in page


def test_unsafe_links_and_active_html_stay_inert_in_the_message_body():
    items = [post(1, content='<script>unsafe()</script>\n\n[x](javascript:alert(1))')]
    page = thread_read_html(items, {'root': key(1)}, path='/thread').decode()
    body = page.split('class="thread-body"', 1)[1].split('</details>', 1)[0]
    assert '<script>' not in body and 'href="javascript:' not in body
    assert '&lt;script&gt;' in body


def test_duplicate_page_items_and_deep_chain_are_rendered_once():
    items = [post(1)] + [post(n, n - 1) for n in range(2, 201)]
    page = thread_read_html([*items, items[0]], {'root': key(1)}, path='/thread').decode()
    assert page.count('<article>') == 200 and 'data-depth="199"' in page


def test_embedded_fragment_shows_replies_without_repeating_the_current_post():
    items = [post(1, content='根帖已经在页面正文里'), post(2, 1), post(3, 2)]
    fragment = thread_fragment_html(items, {'root': key(1)}, embedded=True)
    assert '根帖已经在页面正文里' not in fragment
    assert fragment.count('<article>') == 2
    assert '<!doctype' not in fragment and 'thread-raw-data' not in fragment
    assert '返回主帖' not in fragment
    assert '本页 2 条回复' in fragment and '1 个分支' in fragment
    assert 'data-depth="0"' in fragment and 'data-depth="1"' in fragment
    assert f'href="#reply-{key(1)}"' not in fragment
    assert '回复主帖' in fragment and f'href="#reply-{key(2)}"' in fragment


def test_latest_reply_uses_visible_timestamps_and_only_current_page_counts():
    root, newer, older = post(1), post(2, 1), post(3, 1)
    newer['created_at'] = '2026-10-04T10:20:00Z'
    older['created_at'] = '2026-10-04T10:00:00Z'
    fragment = thread_fragment_html(
        [root, newer, older], {'root': key(1), 'next': '/thread?after=page2'}, embedded=True
    )
    assert f'href="#reply-{key(2)}">最新回复</a>' in fragment
    assert '本页 2 条回复 · 2 个分支 · 后面还有回复' in fragment
    assert 'href="/thread?after=page2">继续阅读回复</a>' in fragment


def test_long_body_uses_explicit_native_disclosure_and_marks_bounded_excerpt():
    content = '# 唯一的作者标题\n\n第一段结论。\n\n' + '长正文。' * 2200
    fragment = thread_fragment_html([post(1, content=content)], {'root': key(1)})
    assert fragment.count('唯一的作者标题') == 1
    assert '<details class="thread-body thread-long-body"><summary>' in fragment
    assert '展开正文' in fragment and '收起正文' in fragment
    assert '正文节选（前 8192 字符）' in fragment
    assert '阅读完整帖子' in fragment
    assert '<h1>唯一的作者标题</h1>' not in fragment


def test_empty_embedded_discussion_and_unsafe_continuation_have_clear_safe_states():
    fragment = thread_fragment_html([post(1)], {'root': key(1)}, embedded=True)
    assert '本页 0 条回复' in fragment and '还没有可读的回复' in fragment
    fragment = thread_fragment_html(
        [post(1)], {'root': key(1), 'next': '//outside.invalid/thread'}, embedded=True
    )
    assert 'href="//outside.invalid' not in fragment


def test_embedded_non_root_focus_keeps_other_posts_and_descendants_once():
    items = [post(1), post(2, 1, content='当前帖已经在页面正文里'), post(3, 2), post(4, 1)]
    fragment = thread_fragment_html(items, {'root': key(1)}, focus_id=key(2), embedded=True)
    assert '当前帖已经在页面正文里' not in fragment
    assert fragment.count('<article>') == 3
    assert fragment.count('id="reply-' + key(3) + '"') == 1
    assert '回复当前帖' in fragment
    assert f'href="#reply-{key(2)}"' not in fragment


def test_every_reply_displays_authorized_direct_reply_and_view_numbers_below_it():
    root, branch, reply = post(1), post(2, 1), post(3, 2)
    branch['view_count'], reply['view_count'] = 37, 0
    statuses = {
        branch['id']: {'reply_count': 1, 'count_complete': True},
        reply['id']: {'reply_count': 0, 'count_complete': True},
    }
    page = thread_fragment_html(
        [root, branch, reply], {'root': key(1)}, embedded=True, reply_statuses=statuses
    )
    assert 'class="thread-branch" open' not in page
    assert '<span class="thread-reply-count">1 条回复</span>' in page
    assert '<span class="thread-view-count">37 浏览</span>' in page
    assert '<p class="thread-stats"><span class="thread-reply-count">0 条回复</span>' in page
    assert '<span class="thread-view-count">0 浏览</span>' in page
    assert page.count('data-thread-branch data-post-id=') == 1


def test_unknown_or_incomplete_statistics_never_look_like_exact_zero():
    items = [post(1), post(2, 1), post(3, 1), post(4, 1)]
    statuses = {
        items[2]['id']: {'reply_count': 0, 'count_complete': False},
        items[3]['id']: {'reply_count': 5, 'count_complete': False, 'public_only': True},
    }
    page = thread_fragment_html(items, {'root': key(1)}, embedded=True, reply_statuses=statuses)
    assert page.count('>回复数未知</span>') == 2
    assert page.count('>浏览量未知</span>') == 3
    assert '>公开 至少 5 条回复</span>' in page
    assert '>0 条回复</span>' not in page and '>0 浏览</span>' not in page
    assert page.count('子回复尚未读取。') == 3


def test_sidechannel_does_not_change_items_or_legacy_thread_data():
    from msg.core.codec import canonical

    items = [post(1), post(2, 1)]
    data = {'root': key(1), 'items': items}
    statuses = {items[1]['id']: {'reply_count': 0, 'count_complete': True, 'view_count': 9}}
    before = canonical({'data': data, 'statuses': statuses})
    fragment = thread_fragment_html(items, data, embedded=True, reply_statuses=statuses)
    assert '>9 浏览</span>' in fragment
    assert canonical({'data': data, 'statuses': statuses}) == before


@pytest.mark.parametrize('count,complete', [(-1, True), (0, 'false')])
def test_incorrect_statistics_types_are_unknown_not_coerced_into_counts(count, complete):
    root, reply = post(1), post(2, 1)
    reply['view_count'] = True
    page = thread_fragment_html(
        [root, reply],
        {'root': key(1)},
        embedded=True,
        reply_statuses={reply['id']: {'reply_count': count, 'count_complete': complete}},
    )
    assert '>回复数未知</span>' in page and '>浏览量未知</span>' in page
    assert '>-1 条回复</span>' not in page and '>1 浏览</span>' not in page
