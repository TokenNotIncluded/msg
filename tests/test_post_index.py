"""主帖索引沿用真实 PostgreSQL 分页、当前授权和旧版本边界。"""

from dataclasses import replace

import pytest
from test_service import call, register

from msg.core.codec import wire
from msg.core.models import ResourceRef
from msg.core.read_query import read_query_version


async def write(app, key, subject, operation, arguments, **kwargs):
    result = await call(app, operation, arguments, key=key, subject=subject, **kwargs)
    assert result.status == 'ok', wire(result)
    return result


async def index(app, arguments, **kwargs):
    result = await call(app, 'discovery.read_query', arguments, contract_version=5, **kwargs)
    assert result.status == 'ok', wire(result)
    return result


async def state(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(
            tx.one(f'SELECT COUNT(*) FROM {table}')[0]
            for table in ('resources', 'revisions', 'events', 'jobs', 'results')
        )


@pytest.fixture
async def posts(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'post-index-owner')
    topic = await write(
        app, key, subject, 'content.topic_create', {'parent': '/main', 'name': 'post-index'}
    )
    parent = topic.resources[0].id
    root = await write(
        app,
        key,
        subject,
        'content.post_create',
        {'parent': parent, 'resource_id': 'r_index_root', 'name': 'root', 'body': 'root body'},
    )
    fork = await write(
        app,
        key,
        subject,
        'discussion.fork',
        {'target': wire(root.resources[0]), 'resource_id': 'r_index_fork', 'body': 'fork body'},
    )
    reply = await write(
        app,
        key,
        subject,
        'discussion.reply',
        {'target': wire(root.resources[0]), 'resource_id': 'r_index_reply', 'body': 'reply body'},
    )
    return app, key, subject, topic, root, fork, reply


@pytest.mark.parametrize(
    'arguments,version',
    [
        ({}, 1),
        ({'expand': ['replies']}, 2),
        ({'expand': {'replies': {}}}, 3),
        ({'query_version': 3}, 3),
        ({'query_version': 5}, 5),
        ({'query_version': 5, 'post_kind': 'roots'}, 5),
    ],
)
def test_post_index_version_is_explicit_and_old_selection_is_unchanged(arguments, version):
    assert read_query_version(arguments) == version


@pytest.mark.asyncio
async def test_roots_include_forks_replies_are_separate_and_queries_never_hydrate_body(
    posts, monkeypatch
):
    app, key, subject, topic, root, fork, reply = posts
    child_topic = await write(
        app,
        key,
        subject,
        'content.topic_create',
        {'parent': topic.resources[0].id, 'name': 'nested'},
    )
    before = await state(app)

    def no_body(*args, **kwargs):
        raise AssertionError('主帖索引不应读取正文')

    monkeypatch.setattr(app.contents, 'read', no_body)
    monkeypatch.setattr(app.contents, 'read_bytes', no_body)
    args = {'parent': topic.resources[0].id, 'fields': ['id', 'name', 'revision', 'view_count']}
    roots = await index(app, {**args, 'post_kind': 'roots'})
    replies = await index(app, {**args, 'post_kind': 'replies'})
    assert {item['id'] for item in roots.data['items']} == {
        root.resources[0].id,
        fork.resources[0].id,
    }
    assert [item['id'] for item in replies.data['items']] == [reply.resources[0].id]
    assert all(item['view_count'] == 0 and 'content' not in item for item in roots.data['items'])
    all_items = await index(app, {'parent': topic.resources[0].id, 'fields': ['id', 'type']})
    assert {item['id'] for item in all_items.data['items']} == {
        root.resources[0].id,
        fork.resources[0].id,
        reply.resources[0].id,
        child_topic.resources[0].id,
    }
    explicit_all = await index(
        app, {'parent': topic.resources[0].id, 'post_kind': 'all', 'fields': ['id', 'type']}
    )
    assert explicit_all.data == all_items.data
    assert await state(app) == before
    async with app.metadata.transaction(write=False) as tx:
        resources = [
            await tx.resource(ref.id)
            for ref in (root.resources[0], fork.resources[0], reply.resources[0])
        ]
        assert all(resource.parent == topic.resources[0].id for resource in resources)


@pytest.mark.asyncio
async def test_edit_retains_reply_classification_and_projects_only_current_revision(posts):
    app, key, subject, topic, root, fork, reply = posts
    async with app.metadata.transaction(write=False) as tx:
        old = await tx.revision(reply.resources[0])
    changed = await write(
        app,
        key,
        subject,
        'content.post_edit',
        {
            'id': reply.resources[0].id,
            'expected_revision': reply.resources[0].revision,
            'body': 'edited reply',
        },
        expected=((reply.resources[0].id, reply.data['generation']),),
    )
    page = await index(
        app,
        {'parent': topic.resources[0].id, 'post_kind': 'replies', 'fields': ['id', 'revision']},
    )
    assert wire(page.data['items']) == [wire(changed.resources[0])]
    roots = await index(
        app, {'parent': topic.resources[0].id, 'post_kind': 'roots', 'fields': ['id']}
    )
    assert {item['id'] for item in roots.data['items']} == {
        root.resources[0].id,
        fork.resources[0].id,
    }
    async with app.metadata.transaction(write=False) as tx:
        current = await tx.revision(ResourceRef(id=changed.resources[0].id))
        assert current.relations == old.relations


@pytest.mark.asyncio
async def test_filtering_precedes_sql_page_and_cursor_carries_version_and_kind(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'post-index-page')
    topic = await write(
        app, key, subject, 'content.topic_create', {'parent': '/main', 'name': 'index-pages'}
    )
    parent = topic.resources[0].id
    roots = []
    for name in ('a', 'b'):
        roots.append(
            await write(
                app,
                key,
                subject,
                'content.post_create',
                {'parent': parent, 'resource_id': 'r_zroot_' + name, 'body': name},
            )
        )
    for number in range(55):
        await write(
            app,
            key,
            subject,
            'discussion.reply',
            {
                'target': wire(roots[0].resources[0]),
                'resource_id': f'r_00_reply_{number:02d}',
                'body': str(number),
            },
        )
    old = await call(
        app,
        'discovery.read_query',
        {'parent': parent, 'type': 'post', 'fields': ['id'], 'limit': 50},
    )
    assert old.status == 'ok', wire(old)
    assert len(old.data['items']) == 50
    assert all(item['id'].startswith('r_00_reply_') for item in old.data['items'])
    first = await index(app, {'parent': parent, 'post_kind': 'roots', 'fields': ['id'], 'limit': 1})
    assert wire(first.data['items']) == [{'id': roots[0].resources[0].id}]
    saved, _ = app.cursors.inspect_page(first.data['cursor'], app.clock())
    assert saved['arguments']['query_version'] == 5
    assert saved['arguments']['post_kind'] == 'roots'
    second = await index(app, {'cursor': first.data['cursor']})
    assert wire(second.data['items']) == [{'id': roots[1].resources[0].id}]
    assert 'next' not in second.data
    wrong_version = await call(
        app, 'discovery.read_query', {'cursor': first.data['cursor']}, contract_version=1
    )
    assert wrong_version.error.code == 'cursor_query_mismatch'
    old_cursor = await call(
        app, 'discovery.read_query', {'cursor': old.data['cursor']}, contract_version=5
    )
    assert old_cursor.error.code == 'cursor_query_mismatch'
    changed_kind = await call(
        app,
        'discovery.read_query',
        {'cursor': first.data['cursor'], 'post_kind': 'replies'},
        contract_version=5,
    )
    assert changed_kind.error.code == 'cursor_query_mismatch'


@pytest.mark.asyncio
async def test_continuation_rechecks_hidden_rows_parent_and_principal(posts):
    app, key, subject, topic, root, fork, _ = posts
    args = {'parent': topic.resources[0].id, 'post_kind': 'roots', 'fields': ['id'], 'limit': 1}
    first = await index(app, args)
    assert wire(first.data['items']) == [{'id': fork.resources[0].id}]
    await write(
        app,
        key,
        subject,
        'content.chmod',
        {'id': root.resources[0].id, 'mode': '0600'},
        expected=((root.resources[0].id, root.data['generation']),),
    )
    second = await index(app, {'cursor': first.data['cursor']})
    assert second.data['items'] == () and 'next' not in second.data
    changed_principal = await call(
        app,
        'discovery.read_query',
        {'cursor': first.data['cursor']},
        key=key,
        subject=subject,
        contract_version=5,
    )
    assert changed_principal.error.code == 'cursor_principal_mismatch'
    await write(
        app,
        key,
        subject,
        'content.chmod',
        {'id': topic.resources[0].id, 'mode': '0000'},
        expected=((topic.resources[0].id, topic.data['generation']),),
    )
    denied = await call(
        app, 'discovery.read_query', {'cursor': first.data['cursor']}, contract_version=5
    )
    assert denied.error.code == 'permission_denied'


@pytest.mark.parametrize('version', [1, 2, 3, 4])
@pytest.mark.asyncio
async def test_published_versions_reject_the_new_filter(installed, version):
    app, _ = installed
    args = {'home_summary': True} if version == 4 else {'parent': '/main'}
    result = await call(
        app, 'discovery.read_query', {**args, 'post_kind': 'roots'}, contract_version=version
    )
    assert result.error.code == 'schema_validation'


@pytest.mark.parametrize('version', [1, 2, 3])
@pytest.mark.asyncio
async def test_old_listing_versions_still_return_both_roots_and_replies(posts, version):
    app, _, _, topic, root, fork, reply = posts
    result = await call(
        app,
        'discovery.read_query',
        {'parent': topic.resources[0].id, 'type': 'post', 'fields': ['id']},
        contract_version=version,
    )
    assert result.status == 'ok', wire(result)
    assert {item['id'] for item in result.data['items']} == {
        root.resources[0].id,
        fork.resources[0].id,
        reply.resources[0].id,
    }


@pytest.mark.parametrize(
    'arguments',
    [{'post_kind': 'unknown'}, {'query_version': 3}, {'expand': ['replies']}],
)
@pytest.mark.asyncio
async def test_post_index_rejects_other_versions_and_nested_expansion(installed, arguments):
    app, _ = installed
    result = await call(
        app, 'discovery.read_query', {'parent': '/main', **arguments}, contract_version=5
    )
    assert result.error.code == 'schema_validation'


@pytest.mark.asyncio
async def test_old_credential_ceiling_does_not_gain_the_new_operation(posts):
    app, key, subject, topic, _, _, _ = posts
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        owner = await tx.subject(subject)
        ceiling = tuple(
            replace(grant, operations=grant.operations - {'discovery.read_query@5'})
            for grant in credential.ceiling
        )
        await tx.save_credential(replace(credential, ceiling=ceiling), owner.auth_version)
    args = {'parent': topic.resources[0].id, 'fields': ['id']}
    old = await call(app, 'discovery.read_query', args, key=key, subject=subject)
    assert old.status == 'ok', wire(old)
    new = await call(
        app, 'discovery.read_query', args, key=key, subject=subject, contract_version=5
    )
    assert new.error.code == 'credential_ceiling'
