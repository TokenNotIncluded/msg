import time
from dataclasses import replace
from datetime import timedelta

import pytest

from msg.core.errors import Failure
from msg.core.models import Relation, ResourceRef


async def tree(h):
    await h.create('r_tree')
    for parent in ('a', 'b'):
        await h.create('r_' + parent, 'r_tree')
        for child in ('a', 'b'):
            await h.create('r_' + parent + child, 'r_' + parent)
            await h.create('r_' + parent + child + '0', 'r_' + parent + child)
            await h.create('r_' + parent + child + '1', 'r_' + parent + child)


def query():
    return {
        'parent': 'r_tree',
        'limit': 1,
        'fields': ['id'],
        'expand': {
            'children': {
                'limit': 1,
                'fields': ['id'],
                'expand': {'children': {'limit': 1, 'fields': ['id']}},
            }
        },
    }


def token(page):
    return page['next'].removeprefix('/_r/c/')


async def test_each_nested_collection_has_independent_continuation(harness):
    h = harness
    await tree(h)
    first = (await h.invoke('discovery.read_query', query(), 3)).data
    assert first['items'][0]['id'] == 'r_a'
    nested = first['items'][0]['collections']['children']
    assert nested['items'][0]['id'] == 'r_aa'
    deepest = nested['items'][0]['collections']['children']
    assert deepest['items'][0]['id'] == 'r_aa0'
    for page in (first, nested, deepest):
        assert page['pageInfo']['hasNextPage'] is True
        assert page['pageInfo']['endCursor'] == token(page)
    next_nested = (await h.invoke('discovery.read_query', {'cursor': token(nested)}, 3)).data
    assert next_nested['items'][0]['id'] == 'r_ab'
    assert next_nested['items'][0]['collections']['children']['items'][0]['id'] == 'r_ab0'
    assert next_nested['pageInfo']['hasNextPage'] is False
    assert next_nested['pageInfo']['endCursor'] and 'next' not in next_nested
    next_root = (await h.invoke('discovery.read_query', {'cursor': token(first)}, 3)).data
    assert next_root['items'][0]['id'] == 'r_b'
    assert next_root['items'][0]['collections']['children']['items'][0]['id'] == 'r_ba'


async def test_nested_replies_use_the_same_projection_shape(harness):
    h = harness
    await h.create('r_thread')
    root = await h.create('r_post_a', 'r_thread', type='post', body='root\n')
    reply = await h.create(
        'r_post_b',
        'r_thread',
        type='post',
        body='reply\n',
        relations=(Relation(type='reply_to', target=ResourceRef(id=root.id)),),
    )
    await h.create(
        'r_post_c',
        'r_thread',
        type='post',
        body='nested\n',
        relations=(Relation(type='reply_to', target=ResourceRef(id=reply.id)),),
    )
    result = (
        await h.invoke(
            'discovery.read_query',
            {
                'parent': 'r_thread',
                'limit': 1,
                'fields': ['id'],
                'expand': {
                    'replies': {
                        'limit': 1,
                        'fields': ['id'],
                        'expand': {'replies': {'limit': 1, 'fields': ['id']}},
                    }
                },
            },
            3,
        )
    ).data
    one = result['items'][0]['collections']['replies']['items'][0]
    assert one['id'] == reply.id
    assert one['collections']['replies']['items'][0]['id'] == 'r_post_c'
    assert set(one) == {'id', 'collections'}


async def test_cursor_reauthorizes_parent_and_known_children(harness):
    h = harness
    await tree(h)
    result = (await h.invoke('discovery.read_query', query(), 3)).data
    nested = result['items'][0]['collections']['children']
    async with h.app.metadata.transaction(write=True) as tx:
        parent = await tx.resource('r_a')
        await tx.replace(
            replace(parent, owner='u_other', mode=0, generation=parent.generation + 1),
            parent.generation,
        )
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', {'cursor': token(nested)}, 3)
    assert exc.value.code == 'permission_denied'


async def test_revoked_child_is_absent_from_page_and_counts(harness):
    h = harness
    await tree(h)
    result = (await h.invoke('discovery.read_query', query(), 3)).data
    nested = result['items'][0]['collections']['children']
    async with h.app.metadata.transaction(write=True) as tx:
        resource = await tx.resource('r_ab')
        await tx.replace(
            replace(resource, owner='u_other', mode=0, generation=resource.generation + 1),
            resource.generation,
        )
    page = (await h.invoke('discovery.read_query', {'cursor': token(nested)}, 3)).data
    assert page['items'] == ()
    assert page['pageInfo']['hasNextPage'] is False
    assert not any(key in page for key in ('count', 'total', 'next'))


@pytest.mark.parametrize(
    'change,code', [('principal', 'cursor_principal_mismatch'), ('expired', 'cursor_expired')]
)
async def test_cursor_principal_and_expiry(harness, change, code):
    h = harness
    await tree(h)
    page = (await h.invoke('discovery.read_query', query(), 3)).data
    ctx = (
        replace(h.ctx, principal=replace(h.ctx.principal, actor='u_bob', subject='u_bob'))
        if change == 'principal'
        else replace(h.ctx, now=h.ctx.now + timedelta(minutes=16))
    )
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', {'cursor': token(page)}, 3, context=ctx)
    assert exc.value.code == code


@pytest.mark.parametrize('version', [1, 2])
async def test_cursor_cannot_downgrade_to_an_older_contract(harness, version):
    h = harness
    await tree(h)
    page = (await h.invoke('discovery.read_query', query(), 3)).data
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', {'cursor': token(page)}, version)
    assert exc.value.code == 'cursor_query_mismatch'


async def test_invalid_projection_rejected_for_empty_scope(harness):
    h = harness
    await h.create('r_empty')
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', {'parent': 'r_empty', 'fields': ['secret_field']}, 3)
    assert exc.value.code == 'schema_validation'


async def test_depth_and_response_budget(harness):
    h = harness
    await tree(h)
    nested = {}
    for _ in range(5):
        nested = {'children': {'limit': 1, 'expand': nested}}
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', {'parent': 'r_tree', 'expand': nested}, 3)
    assert exc.value.code == 'schema_validation'
    h.app.settings.server.limits.max_response_bytes = 100
    with pytest.raises(Failure) as exc:
        await h.invoke('discovery.read_query', query(), 3)
    assert exc.value.code == 'response_too_large'


async def test_read_deadline(harness):
    h = harness
    await tree(h)
    with pytest.raises(Failure) as exc:
        await h.invoke(
            'discovery.read_query',
            query(),
            3,
            context=replace(h.ctx, deadline_monotonic=time.monotonic() - 1),
        )
    assert exc.value.code == 'query_cost_exceeded'


async def test_success_and_failure_do_not_write_business_state(harness):
    h = harness
    await tree(h)

    async def snapshot():
        async with h.app.metadata.transaction(write=False) as tx:
            tables = tx.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return {
                name: sorted(tx.rows('SELECT * FROM "' + name + '"'), key=repr)
                for (name,) in tables
            }

    before = await snapshot()
    await h.invoke('discovery.read_query', query(), 3)
    with pytest.raises(Failure):
        await h.invoke('discovery.read_query', {'parent': 'not_found'}, 3)
    assert await snapshot() == before


async def test_global_node_budget_covers_all_branches(harness):
    h = harness
    await h.create('r_tree')
    base = await h.create('r_parent', 'r_tree')
    async with h.app.metadata.transaction(write=True) as tx:
        for i in range(10):
            mid = replace(base, id=f'r_mid_{i}', name=f'mid{i}', parent=base.id)
            await tx.insert(mid)
            for j in range(10):
                await tx.insert(replace(mid, id=f'r_leaf_{i}_{j}', name=f'leaf{j}', parent=mid.id))
    with pytest.raises(Failure) as exc:
        await h.invoke(
            'discovery.read_query',
            {
                'parent': 'r_tree',
                'limit': 1,
                'fields': ['id'],
                'expand': {
                    'children': {
                        'limit': 10,
                        'fields': ['id'],
                        'expand': {'children': {'limit': 10, 'fields': ['id']}},
                    }
                },
            },
            3,
        )
    assert exc.value.code == 'query_cost_exceeded'


async def test_flat_v2_and_legacy_cursor_compatibility(harness):
    h = harness
    await tree(h)
    args = {'parent': 'r_tree', 'limit': 1, 'fields': ['id']}
    first = (await h.invoke('discovery.read_query', args, 2)).data
    saved, _ = h.app.cursors.inspect_page(first['cursor'], h.ctx.now)
    assert saved['arguments']['expand'] == []
    second = (await h.invoke('discovery.read_query', {'cursor': first['cursor']}, 2)).data
    assert second['items'][0]['id'] == 'r_b'
    # Older flat v2 pages did not include an expand marker. Their exact
    # read semantics match v1 and must not be invalidated by adding v3.
    old = (await h.invoke('discovery.read_query', args, 1)).data
    resumed = (await h.invoke('discovery.read_query', {'cursor': old['cursor']}, 2)).data
    assert resumed['items'][0]['id'] == 'r_b'
