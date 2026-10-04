"""用真实 PostgreSQL 验证回复统计的授权、预算和只读边界。"""

import time
from dataclasses import replace

import pytest
from read_only_evidence import readonly_evidence
from test_service import call, register

from msg.core.codec import canonical, wire
from msg.core.models import Scope
from msg.plugins import discussion_reply_status as status_module
from msg.storage.post_view_migration import record_view
from msg.storage.postgres import PostgresSession


@pytest.fixture
def status_app(installed):
    app, _ = installed
    return app


def ok(result):
    assert result.status == 'ok', wire(result)
    return result


async def post(app, owner, *, target=None, body='正文不应进入统计'):
    return ok(
        await call(
            app,
            'discussion.reply' if target else 'content.post_create',
            {'target': wire(target), 'body': body} if target else {'parent': '/main', 'body': body},
            key=owner[0],
            subject=owner[1],
        )
    ).resources[0]


async def change(app, owner, operation, rid, *, contract_version=1, **arguments):
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(rid)).generation
    return ok(
        await call(
            app,
            operation,
            {'id': rid, **arguments},
            key=owner[0],
            subject=owner[1],
            expected=((rid, generation),),
            contract_version=contract_version,
        )
    )


async def stats(app, ids, owner=None, **options):
    return await call(
        app,
        'discussion.reply_status',
        {'ids': ids, **options},
        key=owner[0] if owner else None,
        subject=owner[1] if owner else None,
    )


@pytest.mark.asyncio
async def test_batch_current_revisions_archive_zero_and_readonly(status_app, monkeypatch):
    app = status_app
    owner = await register(app, 'reply-status-owner')
    root = await post(app, owner)
    empty = await post(app, owner)
    first = await post(app, owner, target=root)
    second = await post(app, owner, target=root)
    nested = await post(app, owner, target=first)
    hidden = await post(app, owner, target=root, body='不可泄漏的私密回复')
    await change(app, owner, 'content.chmod', hidden.id, mode='0600')
    edited = await change(
        app,
        owner,
        'content.post_edit',
        first.id,
        expected_revision=first.revision,
        body='编辑不应增加回复数',
        summary='安全摘要' * 50,
        contract_version=2,
    )
    queries = []
    execute = PostgresSession.execute

    def record_query(self, sql, parameters=(), **kwargs):
        if sql.startswith('WITH requested('):
            queries.append((sql, parameters))
        return execute(self, sql, parameters, **kwargs)

    monkeypatch.setattr(PostgresSession, 'execute', record_query)
    async with readonly_evidence(app, monkeypatch):
        result = ok(
            await stats(
                app, [root.id, empty.id, first.id, root.id], include_thread=True, recent_limit=3
            )
        )
    assert len(queries) == 1
    items = {item['id']: item for item in result.data['items']}
    assert len(items) == 3
    assert items[root.id]['reply_count'] == 2 and items[root.id]['thread_count'] == 3
    assert items[first.id]['reply_count'] == 1 and items[first.id]['thread_count'] == 3
    assert items[empty.id]['reply_count'] == items[empty.id]['thread_count'] == 0
    assert all(item['count_complete'] and item['thread_complete'] for item in items.values())
    assert wire(items[first.id]['thread_root']) == wire(root)
    assert {item['ref']['id'] for item in items[root.id]['recent']} == {first.id, second.id}
    preview = next(item for item in items[root.id]['recent'] if item['ref']['id'] == first.id)
    assert preview['ref']['revision'] == edited.resources[0].revision
    assert preview['summary'] == ('安全摘要' * 50)[:160]
    encoded = canonical(result.data).decode()
    assert (
        hidden.id not in encoded and '不可泄漏' not in encoded and '正文不应进入统计' not in encoded
    )
    await change(app, owner, 'content.archive', second.id)
    assert ok(await stats(app, [root.id])).data['items'][0]['reply_count'] == 1
    assert (
        ok(await stats(app, [first.id], include_thread=True)).data['items'][0]['thread_count'] == 2
    )
    denied = await stats(app, [root.id, hidden.id])
    assert denied.error.code == 'permission_denied' and denied.data is None
    assert (await stats(app, [root.id, 'r_missing'])).error.code == 'not_found'
    assert (await stats(app, ['/main'])).error.code == 'not_a_post'
    assert (await stats(app, [root.id] * 21)).error.code == 'schema_validation'
    assert nested.id in canonical(ok(await stats(app, [first.id], recent_limit=3)).data).decode()


@pytest.mark.asyncio
async def test_hidden_root_and_dynamic_group_access_are_not_inferred(status_app):
    app = status_app
    owner = await register(app, 'reply-status-private')
    member = await register(app, 'reply-status-member')
    root = await post(app, owner, body='不可泄漏的根标题')
    child = await post(app, owner, target=root)
    await change(app, owner, 'content.chmod', root.id, mode='0600')
    orphan = ok(await stats(app, [child.id], include_thread=True)).data['items'][0]
    assert orphan['thread_root'] is None and orphan['thread_count'] is None
    assert not orphan['thread_complete'] and root.id not in canonical(orphan).decode()
    group = (
        ok(
            await call(
                app, 'group.create', {'name': 'reply-status-group'}, key=owner[0], subject=owner[1]
            )
        )
        .resources[0]
        .id
    )
    ok(
        await call(
            app,
            'group.invite',
            {'group': group, 'subject': member[1]},
            key=owner[0],
            subject=owner[1],
        )
    )
    ok(await call(app, 'group.join', {'group': group}, key=member[0], subject=member[1]))
    await change(app, owner, 'content.chgrp', root.id, group=group)
    await change(app, owner, 'content.chmod', root.id, mode='0640')
    assert (
        ok(await stats(app, [child.id], member, include_thread=True)).data['items'][0][
            'thread_root'
        ]['id']
        == root.id
    )
    async with app.metadata.transaction(write=True) as tx:
        organization = await tx.resource(group)
        await tx.replace(
            replace(organization, state='archived', generation=organization.generation + 1),
            organization.generation,
        )
    orphan = ok(await stats(app, [child.id], member, include_thread=True)).data['items'][0]
    assert orphan['thread_root'] is None and orphan['thread_count'] is None
    assert root.id not in canonical(orphan).decode()


@pytest.mark.asyncio
async def test_credential_ceiling_filters_candidates_and_rejects_inputs(status_app):
    app = status_app
    owner = await register(app, 'reply-status-ceiling')
    root = await post(app, owner)
    child = await post(app, owner, target=root)
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(owner[0].key_id)
        identity = await tx.subject(owner[1])
        basic = next(grant for grant in credential.ceiling if grant.capability == 'resource.basic')
        await tx.save_credential(
            replace(
                credential,
                ceiling=(
                    replace(
                        basic,
                        scope=Scope(resource_id=root.id),
                        operations=frozenset({'discussion.reply_status@1'}),
                    ),
                ),
            ),
            identity.auth_version,
        )
    restricted = ok(await stats(app, [root.id], owner, include_thread=True, recent_limit=3)).data[
        'items'
    ][0]
    assert restricted['reply_count'] == restricted['thread_count'] == 0
    assert restricted['count_complete'] and restricted['thread_complete']
    assert not restricted['recent'] and child.id not in canonical(restricted).decode()
    denied = await stats(app, [root.id, child.id], owner)
    assert denied.error.code == 'credential_ceiling' and denied.data is None


@pytest.mark.asyncio
async def test_hidden_candidate_budget_and_query_timeout_keep_transaction_usable(
    status_app, monkeypatch
):
    app = status_app
    owner = await register(app, 'reply-status-budget')
    root = await post(app, owner)
    for _ in range(5):
        child = await post(app, owner, target=root)
        await change(app, owner, 'content.chmod', child.id, mode='0600')
    assert (
        len(ok(await stats(app, [root.id], owner, recent_limit=3)).data['items'][0]['recent']) == 3
    )
    monkeypatch.setattr(status_module, 'MAX_CANDIDATES', 8)
    item = ok(await stats(app, [root.id], include_thread=True, recent_limit=3)).data['items'][0]
    assert item['reply_count'] == item['thread_count'] == 0
    assert not item['count_complete'] and not item['thread_complete'] and not item['recent']
    monkeypatch.setattr(status_module, 'TIME_BUDGET_SECONDS', 0)
    item = ok(await stats(app, [root.id], include_thread=True)).data['items'][0]
    assert not item['count_complete'] and not item['thread_complete']
    execute = PostgresSession.execute
    configured = []

    def delay_query(self, sql, parameters=(), **kwargs):
        if sql == "SELECT set_config('statement_timeout',?,true)":
            configured.append(parameters[0])
        if sql.startswith('WITH requested('):
            return execute(self, 'SELECT pg_sleep(0.1)')
        return execute(self, sql, parameters, **kwargs)

    monkeypatch.setattr(PostgresSession, 'execute', delay_query)
    async with app.metadata.transaction(write=False) as tx:
        timeout = tx.one("SELECT current_setting('statement_timeout')")[0]
        buckets = [(root.id, 'reply_to')]
        rows, incomplete = status_module._candidates(tx, buckets, time.monotonic() + 0.01)
        assert not rows and incomplete == set(buckets)
        assert tx.one('SELECT 1')[0] == 1
        assert tx.one("SELECT current_setting('statement_timeout')")[0] == timeout
        tx.execute("SELECT set_config('statement_timeout',?,true)", ('5ms',))
        configured.clear()
        rows, incomplete = status_module._candidates(tx, buckets, time.monotonic() + 0.02)
        assert not rows and incomplete == set(buckets)
        assert configured[0] == '5ms'
        assert tx.one("SELECT current_setting('statement_timeout')")[0] == '5ms'


@pytest.mark.asyncio
async def test_default_numbers_skip_body_summary_and_thread_candidates(status_app, monkeypatch):
    app = status_app
    owner = await register(app, 'reply-status-numbers')
    root = await post(app, owner)
    child = await post(app, owner, target=root)
    async with app.metadata.transaction(write=True) as tx:
        record_view(tx, root.id, '2026-09-27', 'visitor-one')
        record_view(tx, root.id, '2026-09-27', 'visitor-two')
    queries = []
    execute = PostgresSession.execute

    def record_query(self, sql, parameters=(), **kwargs):
        if sql.startswith('WITH requested('):
            queries.append(parameters)
        return execute(self, sql, parameters, **kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError('数字读取不应读取帖子修订、摘要或正文')

    monkeypatch.setattr(PostgresSession, 'execute', record_query)
    async with readonly_evidence(app, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(PostgresSession, 'revision', forbidden)
            patch.setattr(app.contents, 'read', forbidden)
            patch.setattr(app.contents, 'read_bytes', forbidden)
            result = ok(await stats(app, [root.id, child.id]))
            explicit = ok(
                await stats(app, [root.id, child.id], include_thread=False, recent_limit=0)
            )
    assert wire(result.data) == wire(explicit.data)
    items = {item['id']: item for item in result.data['items']}
    assert all(
        set(item) == {'id', 'reply_count', 'count_complete', 'view_count'}
        for item in items.values()
    )
    assert items[root.id] == {
        'id': root.id,
        'reply_count': 1,
        'count_complete': True,
        'view_count': 2,
    }
    assert items[child.id]['reply_count'] == items[child.id]['view_count'] == 0
    assert len(queries) == 2 and all(
        parameters[1:-1:2] == ('reply_to', 'reply_to') for parameters in queries
    )
    preview = ok(await stats(app, [root.id], recent_limit=1)).data['items'][0]
    assert len(preview['recent']) == 1 and 'thread_root' not in preview
    thread = ok(await stats(app, [root.id], include_thread=True)).data['items'][0]
    assert thread['thread_count'] == 1 and 'recent' not in thread
    assert (await stats(app, [root.id], recent_limit=4)).error.code == 'schema_validation'
