"""回复树不推测缺失父帖，也不会被坏关系绕成环。"""

import pytest

from msg.transports.post_read_pages import thread_read_html
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
    assert page.count('class="thread-branch" open') == 2
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
