"""轻量读取不展开正文，增量的权限与交付边界使用真实 PostgreSQL。"""

import asyncio
import contextvars
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_service import NOW, call, register

from msg.core.codec import canonical, digest, wire
from msg.core.cursors import CursorCodec
from msg.core.errors import Failure
from msg.core.models import BlobRef, Event, Principal, Relation, Revision
from msg.plugins import discussion_context
from msg.plugins.discussion_context import _binding, _decode, _encode, _pack
from msg.storage.session import RelationalSession


def node_ids(data):
    return [data['refs'][node['ref']]['id'] for node in data['nodes']]


async def invoke(app, operation, args, key=None, subject=None, expected=()):
    result = await call(app, operation, args, key=key, subject=subject, expected=expected)
    assert result.status == 'ok', wire(result.error) if result.error else wire(result)
    return result


async def tree(app, count=20):
    key, subject, cert = await register(app, 'context-owner')
    posts = []
    for index in range(1, count + 1):
        parent = (
            None if index == 1 else posts[0] if index in {2, 11} else posts[1 if index < 11 else 10]
        )
        args = {
            'body': f'状态：第{index:02d}节点等待协作。\n' + '保留上下文用于验证token预算。' * 800,
            'resource_id': f'r_{1000 + index:032x}',
            'revision_id': f'v_{1000 + index:032x}',
        }
        relations = (
            (
                Relation(type='reply_to', target=parent.resources[0]),
                Relation(type='thread_root', target=posts[0].resources[0]),
            )
            if parent
            else ()
        )
        revision = Revision(
            format_version=1,
            id=args['revision_id'],
            resource_id=args['resource_id'],
            parents=(),
            content=BlobRef(
                digest=digest(args['body'].encode()),
                size=len(args['body'].encode()),
                media_type='text/markdown',
            ),
            relations=relations,
            actor=subject,
            subject=subject,
            author=subject,
            created_at=NOW,
            manifest_digest='',
        )
        manifest = {
            field: value
            for field, value in wire(revision).items()
            if field not in {'manifest_digest', 'signature'}
        }
        args['content_created_at'] = wire(NOW)
        args['content_signature'] = wire(key.sign(canonical(manifest), purpose='revision'))
        args['target' if parent else 'parent'] = wire(parent.resources[0]) if parent else '/main'
        posts.append(
            await invoke(
                app, 'discussion.reply' if parent else 'content.post_create', args, key, subject
            )
        )
    return key, subject, cert, posts


async def business_state(app):
    async with app.metadata.transaction(write=False) as tx:
        return tuple(
            tx.one(f'SELECT COUNT(*) FROM {name}')[0]
            for name in ('events', 'audit', 'results', 'revisions', 'reactions', 'messages')
        )


def measure(name, data):
    encoded = canonical(data)
    try:
        import tiktoken
    except ImportError:
        tokens = None
    else:
        tokens = len(tiktoken.get_encoding('cl100k_base').encode(encoded.decode()))
    return {'name': name, 'data_bytes': len(encoded), 'cl100k_base_proxy_tokens': tokens}


def save_measurements(rows):
    if target := os.environ.get('MSG_CONTEXT_MEASURE_PATH'):
        path = Path(target)
        existing = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps(existing + rows, ensure_ascii=False, indent=2) + '\n')


def test_cursor_position_is_sealed_bound_expiring_and_epoch_guarded():
    app = SimpleNamespace(cursors=CursorCodec(b'c' * 32))
    position = {
        'phase': 'updates',
        'epoch': 4,
        'fence': 71,
        'seq': 72,
        'offset': 0,
        'expires_at': wire(NOW + timedelta(minutes=15)),
        'hidden': 'r_secret_parent',
    }
    token = _encode(app, 'binding', position)
    inspected = app.cursors.inspect(token)
    assert isinstance(inspected['position'], str)
    assert 'r_secret_parent' not in canonical(inspected).decode()
    assert _decode(app, 'binding', token, 'updates', NOW, 4, 0) == position
    for binding, phase, now, epoch, floor, error in (
        ('other', 'updates', NOW, 4, 0, 'cursor_query_mismatch'),
        ('binding', 'history', NOW, 4, 0, 'cursor_kind_mismatch'),
        ('binding', 'updates', NOW + timedelta(minutes=16), 4, 0, 'cursor_expired'),
        ('binding', 'updates', NOW, 5, 0, 'resync_required'),
        ('binding', 'updates', NOW, 4, 73, 'resync_required'),
    ):
        with pytest.raises(Failure) as caught:
            _decode(app, binding, token, phase, now, epoch, floor)
        assert caught.value.code == error
    encoded, tag = token.split('.')
    tampered = encoded + '.' + ('A' if tag[0] != 'A' else 'B') + tag[1:]
    with pytest.raises(Failure) as caught:
        _decode(app, 'binding', tampered, 'updates', NOW, 4, 0)
    assert caught.value.code == 'invalid_cursor'


def test_cursor_binding_covers_identity_credentials_scope_and_query_budget():
    principal = Principal(
        actor='u_owner',
        subject='u_owner',
        credential_id='key_one',
        method='signature',
        certificates=('c_two', 'c_one'),
        ceiling=(),
    )
    args = ('r_root', 'r_focus', 'branch', 8, 160, 16384)
    original = _binding(principal, *args)
    assert _binding(replace(principal, certificates=('c_one', 'c_two')), *args) == original
    for changed in (
        replace(principal, actor='u_other'),
        replace(principal, subject='u_other'),
        replace(principal, credential_id='key_two'),
        replace(principal, method='token'),
        replace(principal, certificates=('c_one',)),
    ):
        assert _binding(changed, *args) != original
    for index, value in enumerate(('r_other', 'r_other', 'thread', 7, 120, 8192)):
        changed = list(args)
        changed[index] = value
        assert _binding(principal, *changed) != original


def test_reference_table_deduplicates_and_cuts_invalid_parent_cycles():
    refs = [{'id': 'r_a', 'revision': 'v_a'}, {'id': 'r_b', 'revision': 'v_b'}]
    nodes = [
        {
            'reference': ref,
            'parent_reference': refs[1 - index],
            'author_id': 'u_author',
            'preview': '摘要',
            'truncated': False,
        }
        for index, ref in enumerate(refs)
    ]
    data = _pack(nodes, {'root': refs[0], 'focus': refs[0]}, None, 'after', False)
    assert data['refs'] == refs and data['authors'] == ['u_author']
    assert data['root'] == data['focus'] == 0
    assert [node['parent'] for node in data['nodes']] == [None, None]


@pytest.mark.asyncio
async def test_generic_projection_cannot_append_unbudgeted_data():
    handlers = {}

    def register_operation(name, schema, **options):
        def register_handler(handler):
            handlers[name] = handler
            return handler

        return register_handler

    discussion_context.install(None, register_operation)
    with pytest.raises(Failure) as caught:
        await handlers['discussion.context'](
            None,
            SimpleNamespace(return_fields=('content',)),
            None,
        )
    assert caught.value.code == 'context_projection_unsupported'


@pytest.mark.asyncio
async def test_latest_summary_pages_are_small_stable_and_leave_old_thread_unchanged(installed):
    app, _ = installed
    key, subject, _, posts = await tree(app)
    rid = posts[0].resources[0].id
    old = await invoke(app, 'discussion.thread', {'id': rid})
    before = await business_state(app)
    first = await invoke(app, 'discussion.context', {'id': rid})
    data = wire(first.data)
    expected_ids = [post.resources[0].id for post in posts]
    assert node_ids(data) == expected_ids[-8:]
    assert data['has_more'] is True and data['cursor'] and data['after']
    assert data['authors'] == [subject]
    assert len({(ref['id'], ref.get('revision')) for ref in data['refs']}) == len(data['refs'])
    assert all(len(node['preview']) <= 160 and node['truncated'] for node in data['nodes'])
    assert '保留上下文用于验证token预算。' * 20 not in canonical(first.data).decode()
    assert len(canonical(first.data)) < len(canonical(old.data)) / 20
    seen = node_ids(data)
    while data['cursor']:
        result = await invoke(app, 'discussion.context', {'id': rid, 'cursor': data['cursor']})
        data = wire(result.data)
        assert not set(seen) & set(node_ids(data))
        seen.extend(node_ids(data))
    assert set(seen) == set(expected_ids) and len(seen) == 20
    assert data['has_more'] is False
    empty = await invoke(app, 'discussion.context', {'id': rid, 'after': first.data['after']})
    assert not empty.data['nodes'] and empty.data['has_more'] is False
    assert await business_state(app) == before
    repeated = await invoke(app, 'discussion.thread', {'id': rid})
    assert canonical(repeated.data) == canonical(old.data)
    schema = app.registry.schema(app.registry.operation('discussion.thread', 1).input_schema)
    assert set(schema['properties']) == {'id', 'cursor', 'limit'}
    save_measurements([
        measure('old_thread_20_long_posts', old.data),
        measure('context_latest_default_8', first.data),
        measure('context_empty_update', empty.data),
    ])


@pytest.mark.asyncio
async def test_branch_and_hidden_root_parent_author_never_disclose_ids_or_paths(installed):
    app, _ = installed
    key, subject, _, posts = await tree(app, 12)
    focus = posts[1].resources[0].id
    branch = await invoke(app, 'discussion.context', {'id': focus, 'scope': 'branch', 'limit': 50})
    assert set(node_ids(branch.data)) == {post.resources[0].id for post in posts[1:10]}
    root = posts[0].resources[0].id
    leaf = posts[2].resources[0].id
    first = await invoke(app, 'discussion.context', {'id': leaf})
    async with app.metadata.transaction(write=True) as tx:
        for target in (root, focus, subject):
            resource = await tx.resource(target)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
    stale = await call(app, 'discussion.context', {'id': leaf, 'after': first.data['after']})
    assert stale.error.code == 'resync_required', wire(stale)
    hidden = await invoke(app, 'discussion.context', {'id': leaf, 'limit': 50})
    serialized = canonical(hidden.data).decode()
    assert hidden.data['root'] is None and hidden.data['authors'] == ()
    assert all(value not in serialized for value in (root, focus, subject))
    assert 'path' not in hidden.data and 'total' not in hidden.data
    child = next(
        node for node in hidden.data['nodes'] if hidden.data['refs'][node['ref']]['id'] == leaf
    )
    assert child['parent'] is None and child['author'] is None
    denied = await call(app, 'discussion.context', {'id': root})
    assert denied.error.code == 'permission_denied'
    owner = await invoke(app, 'discussion.context', {'id': leaf}, key, subject)
    assert root in {ref['id'] for ref in owner.data['refs']}


@pytest.mark.asyncio
async def test_updates_return_only_new_or_edited_nodes_once_and_no_ack(installed):
    app, _ = installed
    key, subject, _, posts = await tree(app)
    rid = posts[0].resources[0].id
    first = await invoke(app, 'discussion.context', {'id': rid})
    edited = posts[4]
    for body in ('状态：更新一次。', '状态：更新两次。'):
        edited = await invoke(
            app,
            'content.post_edit',
            {
                'id': edited.resources[0].id,
                'expected_revision': edited.resources[0].revision,
                'body': body,
            },
            key,
            subject,
            ((edited.resources[0].id, edited.data['generation']),),
        )
    added = await invoke(
        app,
        'discussion.reply',
        {'target': wire(posts[10].resources[0]), 'body': '状态：新增分支任务。'},
        key,
        subject,
    )
    await invoke(app, 'discussion.like', {'id': rid}, key, subject)
    await invoke(
        app, 'content.post_create', {'parent': '/main', 'body': '不相关的新话题。'}, key, subject
    )
    before = await business_state(app)
    update = await invoke(app, 'discussion.context', {'id': rid, 'after': first.data['after']})
    assert set(node_ids(update.data)) == {edited.resources[0].id, added.resources[0].id}
    assert len(update.data['nodes']) == 2 and update.data['has_more'] is False
    matching = next(ref for ref in update.data['refs'] if ref['id'] == edited.resources[0].id)
    assert matching['revision'] == edited.resources[0].revision
    empty = await invoke(app, 'discussion.context', {'id': rid, 'after': update.data['after']})
    assert not empty.data['nodes'] and empty.data['has_more'] is False
    assert await business_state(app) == before
    save_measurements([measure('context_update_2_changed_nodes', update.data)])


@pytest.mark.asyncio
async def test_multireference_events_resume_at_first_unreturned_reference_and_bound_sparse_scans(
    installed,
):
    app, _ = installed
    _, subject, _, posts = await tree(app, 6)
    rid = posts[0].resources[0].id
    args = {'id': rid, 'limit': 1}
    first = await invoke(app, 'discussion.context', args)
    async with app.metadata.transaction(write=True) as tx:
        await tx.append_event(
            Event(
                id='e_context_multiple_refs',
                type='content.post_edit',
                time=NOW,
                request_id='context-test-multi',
                actor=subject,
                subject=subject,
                resources=tuple(post.resources[0] for post in posts[1:]),
                data={},
            )
        )
    after = first.data['after']
    collected = []
    for _ in range(5):
        result = await invoke(app, 'discussion.context', {**args, 'after': after})
        assert len(result.data['nodes']) == 1
        collected.extend(node_ids(result.data))
        after = result.data['after']
    assert collected == [post.resources[0].id for post in posts[1:]]
    assert result.data['has_more'] is False
    async with app.metadata.transaction(write=True) as tx:
        for index in range(discussion_context.MAX_SCAN + 1):
            await tx.append_event(
                Event(
                    id=f'e_context_noise_{index}',
                    type='unrelated.activity',
                    time=NOW,
                    request_id=f'context-noise-{index}',
                    actor=subject,
                    subject=subject,
                    resources=(),
                    data={},
                )
            )
    bounded = await invoke(app, 'discussion.context', {**args, 'after': after})
    assert not bounded.data['nodes'] and bounded.data['has_more'] is None
    remainder = await invoke(app, 'discussion.context', {**args, 'after': bounded.data['after']})
    assert not remainder.data['nodes'] and remainder.data['has_more'] is False
    byte_args = {'id': rid, 'limit': 50, 'preview_chars': 280, 'max_bytes': 4096}
    byte_first = await invoke(app, 'discussion.context', byte_args)
    async with app.metadata.transaction(write=True) as tx:
        await tx.append_event(
            Event(
                id='e_context_byte_pages',
                type='content.post_edit',
                time=NOW,
                request_id='context-test-byte-pages',
                actor=subject,
                subject=subject,
                resources=tuple(post.resources[0] for post in posts[1:]),
                data={},
            )
        )
    after = byte_first.data['after']
    collected = []
    for _ in range(5):
        page = await invoke(app, 'discussion.context', {**byte_args, 'after': after})
        assert len(canonical(page.data)) <= 4096 and page.data['nodes']
        collected.extend(node_ids(page.data))
        after = page.data['after']
        if page.data['has_more'] is False:
            break
    assert len(page.data['nodes']) < 5
    assert collected == [post.resources[0].id for post in posts[1:]]


@pytest.mark.asyncio
async def test_archive_acl_cursor_scope_and_budget_are_checked_on_every_page(installed):
    app, _ = installed
    key, subject, _, posts = await tree(app, 5)
    rid = posts[0].resources[0].id
    first = await invoke(app, 'discussion.context', {'id': rid, 'limit': 1})
    for args, error in (
        (
            {'id': posts[1].resources[0].id, 'limit': 1, 'cursor': first.data['cursor']},
            'cursor_query_mismatch',
        ),
        (
            {'id': rid, 'limit': 1, 'scope': 'branch', 'after': first.data['after']},
            'cursor_query_mismatch',
        ),
        ({'id': rid, 'limit': 2, 'after': first.data['after']}, 'cursor_query_mismatch'),
        (
            {'id': rid, 'cursor': first.data['cursor'], 'after': first.data['after']},
            'schema_validation',
        ),
    ):
        result = await call(app, 'discussion.context', args)
        assert result.error.code == error, wire(result)
    wrong_principal = await call(
        app,
        'discussion.context',
        {'id': rid, 'limit': 1, 'after': first.data['after']},
        key=key,
        subject=subject,
    )
    assert wrong_principal.error.code == 'cursor_query_mismatch', wire(wrong_principal)
    bounded_args = {'id': rid, 'limit': 50, 'preview_chars': 280, 'max_bytes': 2048}
    bounded = await call(app, 'discussion.context', bounded_args)
    assert bounded.status == 'error' and bounded.error.code == 'response_budget_exceeded', wire(
        bounded
    )
    no_preview = await invoke(app, 'discussion.context', {'id': rid, 'preview_chars': 0})
    assert all(not node['preview'] and node['truncated'] for node in no_preview.data['nodes'])
    target = posts[-1]
    await invoke(
        app,
        'content.archive',
        {'id': target.resources[0].id},
        key,
        subject,
        ((target.resources[0].id, target.data['generation']),),
    )
    stale = await call(
        app, 'discussion.context', {'id': rid, 'limit': 1, 'after': first.data['after']}
    )
    assert stale.error.code == 'resync_required', wire(stale)
    current = await invoke(app, 'discussion.context', {'id': rid, 'limit': 50})
    assert target.resources[0].id not in node_ids(current.data)
    intermediate = posts[2]
    descendant = await invoke(
        app,
        'discussion.reply',
        {'target': wire(intermediate.resources[0]), 'body': '状态：保留的后续回复。'},
        key,
        subject,
    )
    await invoke(
        app,
        'content.archive',
        {'id': intermediate.resources[0].id},
        key,
        subject,
        ((intermediate.resources[0].id, intermediate.data['generation']),),
    )
    branch = await invoke(
        app,
        'discussion.context',
        {'id': posts[1].resources[0].id, 'scope': 'branch', 'limit': 50},
    )
    assert intermediate.resources[0].id not in node_ids(branch.data)
    assert descendant.resources[0].id in node_ids(branch.data)


@pytest.mark.asyncio
async def test_initial_fence_precedes_history_read_so_concurrent_reply_is_not_lost(
    installed, monkeypatch
):
    app, _ = installed
    key, subject, _, posts = await tree(app, 3)
    rid = posts[0].resources[0].id
    original = discussion_context._history
    added = None

    async def concurrent(*args, **kwargs):
        nonlocal added
        added = await asyncio.create_task(
            invoke(
                app,
                'discussion.reply',
                {'target': wire(posts[0].resources[0]), 'body': '状态：围栏后的新任务。'},
                key,
                subject,
            ),
            context=contextvars.Context(),
        )
        return await original(*args, **kwargs)

    monkeypatch.setattr(discussion_context, '_history', concurrent)
    first = await invoke(app, 'discussion.context', {'id': rid})
    monkeypatch.setattr(discussion_context, '_history', original)
    updated = await invoke(app, 'discussion.context', {'id': rid, 'after': first.data['after']})
    assert node_ids(updated.data) == [added.resources[0].id]
    assert updated.data['has_more'] is False


@pytest.mark.asyncio
async def test_preview_and_reference_keep_same_revision_across_concurrent_edit(
    installed, monkeypatch
):
    app, _ = installed
    key, subject, _, posts = await tree(app, 3)
    target = posts[-1]
    old_ref = target.resources[0]
    original = RelationalSession.revision
    edited = None

    async def concurrent(session, reference):
        nonlocal edited
        if reference == old_ref and edited is None:
            edited = True
            edited = await asyncio.create_task(
                invoke(
                    app,
                    'content.post_edit',
                    {
                        'id': old_ref.id,
                        'expected_revision': old_ref.revision,
                        'body': '状态：新的版本内容。',
                    },
                    key,
                    subject,
                    ((old_ref.id, target.data['generation']),),
                ),
                context=contextvars.Context(),
            )
        return await original(session, reference)

    monkeypatch.setattr(RelationalSession, 'revision', concurrent)
    first = await invoke(app, 'discussion.context', {'id': posts[0].resources[0].id})
    monkeypatch.setattr(RelationalSession, 'revision', original)
    node = next(
        node for node in first.data['nodes'] if first.data['refs'][node['ref']]['id'] == old_ref.id
    )
    assert first.data['refs'][node['ref']]['revision'] == old_ref.revision
    assert node['preview'].startswith('状态：第03节点等待协作。')
    update = await invoke(
        app,
        'discussion.context',
        {'id': posts[0].resources[0].id, 'after': first.data['after']},
    )
    changed = update.data['nodes'][0]
    assert update.data['refs'][changed['ref']]['revision'] == edited.resources[0].revision
    assert changed['preview'] == '状态：新的版本内容。'
