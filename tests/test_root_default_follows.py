"""Root defaults are read policy; explicit actions retain signed, persistent intent."""

import time
from dataclasses import replace
from datetime import timedelta

import pytest
from read_only_evidence import readonly_evidence
from test_service import NOW, call, register

from msg.bootstrap import seed_resource
from msg.constants import ROOT_SUBJECT
from msg.core.codec import wire
from msg.core.models import ExecutionContext
from msg.core.requests import request_for
from msg.plugins import agent_follows


async def invoke(app, operation, arguments, *, key=None, subject=None, **kwargs):
    result = await call(app, operation, arguments, key=key, subject=subject, **kwargs)
    assert result.status == 'ok', wire(result)
    return result


async def relation(app, follower, target=ROOT_SUBJECT):
    async with app.metadata.transaction(write=False) as tx:
        return await agent_follows.effective_follow(tx, follower, target)


async def targets(app, follower):
    async with app.metadata.transaction(write=False) as tx:
        return await agent_follows.effective_targets(tx, follower)


async def topology(app, *, key=None, subject=None, **limits):
    request = request_for(
        'communication.followers',
        {'subject_id': ROOT_SUBJECT},
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=120),
    )
    async with app.metadata.transaction(write=False) as tx:
        principal = await app.authenticator.authenticate(request, tx, entry='network')
        context = ExecutionContext(
            request_id=request.request_id,
            principal=principal,
            entry='network',
            now=NOW,
            deadline_monotonic=time.monotonic() + 30,
        )
        graph = await agent_follows.public_topology(app, context, request, tx, **limits)
        assert graph['version'] == 1
        assert graph['scanned'] == len(graph['nodes']) + len(graph['edges'])
        return graph


def edges(graph):
    assert all(
        edge['source'] in graph['nodes'] and edge['target'] in graph['nodes']
        for edge in graph['edges']
    )
    return {(edge['source'], edge['target']): edge for edge in graph['edges']}


async def change_resource(app, rid, **changes):
    async with app.metadata.transaction(write=True) as tx:
        resource = await tx.resource(rid)
        await tx.replace(
            replace(resource, **changes, generation=resource.generation + 1),
            resource.generation,
        )


@pytest.mark.asyncio
async def test_root_defaults_are_readonly_across_lists_profile_feed_and_post_state(
    installed, monkeypatch
):
    app, _ = installed
    key, alice, _ = await register(app, 'root-default-reader')
    _, bob, _ = await register(app, 'root-default-other')
    # Root publishes release content through bootstrap, rather than network signing.
    async with app.metadata.transaction(write=True) as tx:
        post = await seed_resource(
            tx,
            app.contents,
            {
                'id': 'p_00000000000000000000000000000001',
                'type': 'post',
                'name': 'Root welcome.md',
                'parent': 't_main',
                'owner': ROOT_SUBJECT,
                'group': 'g_public',
                'mode': '0644',
            },
            NOW,
            body='# Root welcome\n\nA public announcement.',
        )
        # The author remains Root when a current owner changes.
        await tx.replace(replace(post, owner=bob, generation=post.generation + 1), post.generation)
        expected_followers = {
            row[0]
            for row in tx.rows(
                "SELECT id FROM resources WHERE type='user' AND state='active' AND id<>?",
                (ROOT_SUBJECT,),
            )
        }
        assert tx.one('SELECT COUNT(*) FROM agent_follows')[0] == 0
    async with readonly_evidence(app, monkeypatch):
        assert await relation(app, alice) == 'default'
        assert await relation(app, ROOT_SUBJECT) is None
        assert await relation(app, alice, bob) is None
        assert await targets(app, alice) == [ROOT_SUBJECT]
        assert await targets(app, ROOT_SUBJECT) == []
        outgoing = await invoke(app, 'communication.agent_following', {'subject_id': alice})
        assert [item['id'] for item in outgoing.data['items']] == [ROOT_SUBJECT]
        assert outgoing.data['items'][0]['mutual'] is False
        incoming = await invoke(
            app, 'communication.followers', {'subject_id': ROOT_SUBJECT, 'limit': 100}
        )
        assert {item['id'] for item in incoming.data['items']} == expected_followers
        assert not incoming.data['has_more']
        profile = await invoke(app, 'discovery.get', {'id': alice})
        assert profile.data['profile']['following_count'] == 1
        root_profile = await invoke(app, 'discovery.get', {'id': ROOT_SUBJECT})
        assert root_profile.data['profile']['follower_count'] == len(expected_followers)
        assert root_profile.data['profile']['following_count'] == 0
        state = await invoke(app, 'discussion.state', {'id': post.id}, key=key, subject=alice)
        assert state.data['following'] is True
        anonymous_state = await invoke(app, 'discussion.state', {'id': post.id})
        assert anonymous_state.data['following'] is False
        feed = await invoke(app, 'discovery.recommendations', {}, key=key, subject=alice)
        item = next(item for item in feed.data['items'] if item['id'] == post.id)
        assert 'followed_author' in item['reasons']
        anonymous_feed = await invoke(app, 'discovery.recommendations', {})
        item = next(item for item in anonymous_feed.data['items'] if item['id'] == post.id)
        assert 'followed_author' not in item['reasons']
        graph = await topology(app)
        assert edges(graph)[alice, ROOT_SUBJECT]['source_type'] == 'default'
        assert not graph['bounded']
    await invoke(app, 'communication.unfollow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    async with readonly_evidence(app, monkeypatch):
        profile = await invoke(app, 'discovery.get', {'id': alice})
        assert profile.data['profile']['following_count'] == 0
        root_profile = await invoke(app, 'discovery.get', {'id': ROOT_SUBJECT})
        assert root_profile.data['profile']['follower_count'] == len(expected_followers) - 1
        state = await invoke(app, 'discussion.state', {'id': post.id}, key=key, subject=alice)
        assert state.data['following'] is False
        feed = await invoke(app, 'discovery.recommendations', {}, key=key, subject=alice)
        item = next(item for item in feed.data['items'] if item['id'] == post.id)
        assert 'followed_author' not in item['reasons']
        assert (alice, ROOT_SUBJECT) not in edges(await topology(app))


@pytest.mark.asyncio
async def test_root_unfollow_without_row_persists_and_historical_result_survives_refollow(
    installed, monkeypatch
):
    app, _ = installed
    key, alice, _ = await register(app, 'root-optout-reader')
    request_id = 'root-unfollow-once'
    unfollowed = await invoke(
        app,
        'communication.unfollow',
        {'id': '/@root'},
        key=key,
        subject=alice,
        rid=request_id,
    )
    assert unfollowed.data['following'] is False
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('root_follow_optout:' + alice) is True
        assert not tx.one(
            'SELECT 1 FROM agent_follows WHERE follower=? AND target=?', (alice, ROOT_SUBJECT)
        )
        historical = tx.one(
            'SELECT body FROM results WHERE subject=? AND request_id=?', (alice, request_id)
        )[0]
    async with readonly_evidence(app, monkeypatch):
        assert await relation(app, alice) is None
        assert await targets(app, alice) == []
        outgoing = await invoke(app, 'communication.agent_following', {'subject_id': alice})
        assert not outgoing.data['items']
    # Remove only the new marker to exercise an actual older successful receipt.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DELETE FROM settings WHERE key=?', ('root_follow_optout:' + alice,), write=True)
    assert await relation(app, alice) is None
    await invoke(app, 'communication.follow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    assert await relation(app, alice) == 'explicit'
    async with app.metadata.transaction(write=True) as tx:
        # Existing explicit intent wins even a restored stale opt-out marker.
        tx.set_setting('root_follow_optout:' + alice, True)
    assert await relation(app, alice) == 'explicit'
    await invoke(app, 'communication.follow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    async with readonly_evidence(app, monkeypatch):
        replay = await invoke(
            app,
            'communication.unfollow',
            {'id': '/@root'},
            key=key,
            subject=alice,
            rid=request_id,
        )
        assert replay.replayed
        assert await relation(app, alice) == 'explicit'
        async with app.metadata.transaction(write=False) as tx:
            assert tx.setting('root_follow_optout:' + alice) is None
            assert (
                tx.one(
                    'SELECT body FROM results WHERE subject=? AND request_id=?', (alice, request_id)
                )[0]
                == historical
            )
    await invoke(app, 'communication.unfollow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    assert await relation(app, alice) is None


@pytest.mark.asyncio
async def test_root_default_history_limit_fails_closed_but_explicit_relation_wins(
    installed, monkeypatch
):
    app, _ = installed
    key, alice, _ = await register(app, 'root-history-reader')
    _, bob, _ = await register(app, 'root-history-other')
    assert await relation(app, alice) == 'default'
    await invoke(app, 'communication.unfollow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    async with app.metadata.transaction(write=True) as tx:
        tx.execute('DELETE FROM settings WHERE key=?', ('root_follow_optout:' + alice,), write=True)
    # Only genuine Root-unfollow candidates count against the historical cap.
    monkeypatch.setattr(agent_follows, 'MAX_ROOT_HISTORY_RESULTS', 0)
    assert await relation(app, alice) is None
    assert await relation(app, bob) == 'default'
    graph = await topology(app)
    assert graph['bounded']
    assert (alice, ROOT_SUBJECT) not in edges(graph)
    monkeypatch.setattr(agent_follows, 'MAX_ROOT_HISTORY_RESULTS', 256)
    monkeypatch.setattr(agent_follows, 'MAX_ROOT_HISTORY_BYTES', 1)
    assert await relation(app, alice) is None
    assert (alice, ROOT_SUBJECT) not in edges(await topology(app))
    assert (await topology(app))['bounded']
    await invoke(app, 'communication.follow', {'id': ROOT_SUBJECT}, key=key, subject=alice)
    monkeypatch.setattr(agent_follows, 'MAX_ROOT_HISTORY_RESULTS', 0)
    assert await relation(app, alice) == 'explicit'
    assert await relation(app, bob) == 'default'
    assert (alice, ROOT_SUBJECT) in edges(await topology(app))


@pytest.mark.asyncio
async def test_unrelated_signed_results_do_not_disable_default_root_follow(installed, monkeypatch):
    from msg.storage.postgres import PostgresSession

    app, _ = installed
    key, alice, _ = await register(app, 'root-busy-reader')
    _, bob, _ = await register(app, 'root-busy-other')
    for index in range(256):
        await invoke(
            app,
            'communication.follow',
            {'id': bob},
            key=key,
            subject=alice,
            rid='unrelated-follow-' + str(index),
        )
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT COUNT(*) FROM results WHERE subject=?', (alice,))[0] == 257
        assert tx.one('SELECT COUNT(*) FROM agent_follows WHERE follower=?', (alice,))[0] == 1
    original_rows = PostgresSession.rows
    historical_rows = []

    def inspect_rows(session, sql, parameters=()):
        rows = original_rows(session, sql, parameters)
        if 'FROM results' in sql:
            historical_rows.extend(rows)
        return rows

    monkeypatch.setattr(PostgresSession, 'rows', inspect_rows)
    async with readonly_evidence(app, monkeypatch):
        assert await relation(app, alice) == 'default'
        graph = await topology(app)
        assert not graph['bounded']
        assert edges(graph)[alice, ROOT_SUBJECT]['source_type'] == 'default'
        assert edges(graph)[alice, bob]['source_type'] == 'explicit'
        incoming = await invoke(app, 'communication.followers', {'subject_id': ROOT_SUBJECT})
        assert alice in {item['id'] for item in incoming.data['items']}
        profile = await invoke(app, 'discovery.get', {'id': alice})
        assert profile.data['profile']['following_count'] == 2
        assert historical_rows == []


@pytest.mark.asyncio
async def test_topology_exhausted_scan_budget_does_not_fetch_more_history(installed, monkeypatch):
    from msg.storage.postgres import PostgresSession

    app, _ = installed
    _, alice, _ = await register(app, 'root-budget-alice')
    _, bob, _ = await register(app, 'root-budget-bob')
    # Reserving Root plus these two users consumes every permitted scan. The
    # additional built-in online CA proves the node scan is incomplete.
    monkeypatch.setattr(agent_follows, 'MAX_READ_SCANNED', 3)
    original_rows = PostgresSession.rows
    history_queries = []

    def inspect_rows(session, sql, parameters=()):
        if 'FROM results' in sql:
            history_queries.append(sql)
        return original_rows(session, sql, parameters)

    monkeypatch.setattr(PostgresSession, 'rows', inspect_rows)
    async with readonly_evidence(app, monkeypatch):
        graph = await topology(app)
        assert set(graph['nodes']) == {ROOT_SUBJECT, alice, bob}
        assert graph['bounded'] and not graph['edges']
        assert history_queries == []


@pytest.mark.asyncio
async def test_default_root_follow_does_not_expand_signed_operation_ceiling(installed):
    app, _ = installed
    key, alice, _ = await register(app, 'root-ceiling-reader')
    assert await relation(app, alice) == 'default'
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        await tx.save_credential(
            replace(
                credential,
                ceiling=tuple(
                    replace(
                        grant,
                        operations=grant.operations
                        - {'communication.follow@1', 'communication.unfollow@1'},
                    )
                    for grant in credential.ceiling
                ),
            ),
            (await tx.subject(alice)).auth_version,
        )
    for operation in ('communication.follow', 'communication.unfollow'):
        denied = await call(app, operation, {'id': ROOT_SUBJECT}, key=key, subject=alice)
        assert denied.status == 'error' and denied.error.code == 'credential_ceiling'
    assert await relation(app, alice) == 'default'
    async with app.metadata.transaction(write=False) as tx:
        assert tx.setting('root_follow_optout:' + alice) is None
        assert tx.one('SELECT COUNT(*) FROM agent_follows WHERE follower=?', (alice,))[0] == 0


@pytest.mark.asyncio
async def test_root_default_pagination_counts_and_mutual_use_effective_relations(installed):
    app, _ = installed
    registered = [await register(app, 'root-page-' + str(index)) for index in range(3)]
    alice = registered[0][1]
    async with app.metadata.transaction(write=True) as tx:
        # Root is local-only; this fixture represents an existing explicit row.
        tx.execute(
            'INSERT INTO agent_follows VALUES (?,?,?)', (ROOT_SUBJECT, alice, wire(NOW)), write=True
        )
        expected = {
            row[0]
            for row in tx.rows(
                "SELECT id FROM resources WHERE type='user' AND state='active' AND id<>?",
                (ROOT_SUBJECT,),
            )
        }
    observed = []
    after = None
    while True:
        arguments = {'subject_id': ROOT_SUBJECT, 'limit': 1}
        if after:
            arguments['after'] = after
        page = await invoke(app, 'communication.followers', arguments)
        observed.extend(page.data['items'])
        if not page.data['has_more']:
            break
        assert page.data['after'] == page.data['items'][-1]['id']
        assert after is None or page.data['after'] > after
        after = page.data['after']
    assert [item['id'] for item in observed] == sorted(expected)
    assert next(item for item in observed if item['id'] == alice)['mutual'] is True
    outgoing = await invoke(app, 'communication.agent_following', {'subject_id': alice})
    assert outgoing.data['items'][0]['id'] == ROOT_SUBJECT
    assert outgoing.data['items'][0]['mutual'] is True
    root_outgoing = await invoke(app, 'communication.agent_following', {'subject_id': ROOT_SUBJECT})
    assert [(item['id'], item['mutual']) for item in root_outgoing.data['items']] == [(alice, True)]
    graph_edges = edges(await topology(app))
    assert graph_edges[alice, ROOT_SUBJECT]['mutual'] is True
    assert graph_edges[ROOT_SUBJECT, alice]['source_type'] == 'explicit'


@pytest.mark.asyncio
async def test_active_service_user_without_subject_has_default_and_preserves_explicit_source(
    installed, monkeypatch
):
    app, _ = installed
    key, alice, _ = await register(app, 'root-service-reader')
    async with app.metadata.transaction(write=True) as tx:
        service = await seed_resource(
            tx,
            app.contents,
            {
                'id': 'u_service_default_fixture',
                'type': 'user',
                'name': '@service-default-fixture',
                'parent': 'r_root',
                'owner': ROOT_SUBJECT,
                'group': 'g_public',
                'mode': '0755',
            },
            NOW,
        )
        assert not tx.one('SELECT 1 FROM identities WHERE id=?', (service.id,))
        expected_count = tx.one(
            "SELECT COUNT(*) FROM resources WHERE type='user' AND state='active' AND id<>?",
            (ROOT_SUBJECT,),
        )[0]
    async with readonly_evidence(app, monkeypatch):
        assert await relation(app, service.id) == 'default'
        assert await targets(app, service.id) == [ROOT_SUBJECT]
        assert await relation(app, service.id, service.id) is None
        graph = await topology(app)
        assert service.id in graph['nodes']
        assert edges(graph)[service.id, ROOT_SUBJECT]['source_type'] == 'default'
        assert edges(graph)[service.id, ROOT_SUBJECT]['created_at'] is None
        incoming = await invoke(app, 'communication.followers', {'subject_id': ROOT_SUBJECT})
        assert service.id in {item['id'] for item in incoming.data['items']}
        root_profile = await invoke(app, 'discovery.get', {'id': ROOT_SUBJECT})
        assert root_profile.data['profile']['follower_count'] == expected_count
    # A read-derived default never supplies the identity required for writes.
    denied = await call(app, 'communication.follow', {'id': service.id}, key=key, subject=alice)
    assert denied.status == 'error' and denied.error.code == 'subject_not_found'
    created_at = wire(NOW - timedelta(days=1))
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO agent_follows VALUES (?,?,?)',
            (service.id, ROOT_SUBJECT, created_at),
            write=True,
        )
        assert not tx.one('SELECT 1 FROM identities WHERE id=?', (service.id,))
    async with readonly_evidence(app, monkeypatch):
        assert await relation(app, service.id) == 'explicit'
        graph = await topology(app)
        edge = edges(graph)[service.id, ROOT_SUBJECT]
        assert edge['source_type'] == 'explicit' and edge['created_at'] == created_at
        root_profile = await invoke(app, 'discovery.get', {'id': ROOT_SUBJECT})
        assert root_profile.data['profile']['follower_count'] == expected_count


@pytest.mark.asyncio
async def test_root_default_public_topology_reserves_root_and_limits_targets(
    installed, monkeypatch
):
    app, _ = installed
    key, alice, _ = await register(app, 'root-cap-reader')
    _, bob, _ = await register(app, 'root-cap-bob')
    _, carol, _ = await register(app, 'root-cap-carol')
    monkeypatch.setattr(agent_follows, 'MAX_FOLLOWS', 2)
    for target in (bob, carol):
        await invoke(app, 'communication.follow', {'id': target}, key=key, subject=alice)
    assert await targets(app, alice) == sorted([bob, carol, ROOT_SUBJECT])
    other_key, other, _ = await register(app, 'root-cap-explicit')
    for target in (bob, ROOT_SUBJECT):
        await invoke(app, 'communication.follow', {'id': target}, key=other_key, subject=other)
    assert await targets(app, other) == sorted([bob, ROOT_SUBJECT])
    graph = await topology(app, max_users=1, max_edges=1)
    assert graph['nodes'] == [ROOT_SUBJECT]
    assert graph['edges'] == [] and graph['bounded']
    graph = await topology(app, max_users=3, max_edges=1)
    assert ROOT_SUBJECT in graph['nodes'] and len(graph['nodes']) == 3
    assert len(graph['edges']) == 1 and graph['bounded']
    edges(graph)


@pytest.mark.asyncio
async def test_private_and_inactive_accounts_never_enter_public_topology_even_for_owner(installed):
    app, _ = installed
    private_key, private, _ = await register(app, 'root-private-user')
    _, inactive, _ = await register(app, 'root-inactive-user')
    await change_resource(app, private, mode=0o700)
    await change_resource(app, inactive, state='archived')
    assert await relation(app, private) == 'default'
    assert await relation(app, inactive) is None
    for graph in (await topology(app), await topology(app, key=private_key, subject=private)):
        assert private not in graph['nodes'] and inactive not in graph['nodes']
        assert private not in str(graph['edges']) and inactive not in str(graph['edges'])
        edges(graph)
    incoming = await invoke(app, 'communication.followers', {'subject_id': ROOT_SUBJECT})
    assert not {private, inactive} & {item['id'] for item in incoming.data['items']}
    await change_resource(app, ROOT_SUBJECT, mode=0o700)
    graph = await topology(app, max_users=1, max_edges=1)
    assert ROOT_SUBJECT not in graph['nodes']
    assert ROOT_SUBJECT not in str(graph['edges'])
    outgoing = await invoke(
        app,
        'communication.agent_following',
        {'subject_id': private},
        key=private_key,
        subject=private,
    )
    assert not outgoing.data['items']


@pytest.mark.asyncio
async def test_inactive_ancestor_hides_root_and_all_public_nodes(installed):
    app, _ = installed
    _, alice, _ = await register(app, 'root-ancestor-reader')
    await change_resource(app, 'r_root', state='archived')
    # Policy is not permission: ancestor state is enforced by the projection.
    assert await relation(app, alice) == 'default'
    graph = await topology(app, max_users=1, max_edges=1)
    assert graph['nodes'] == [] and graph['edges'] == []
    denied = await call(app, 'communication.agent_following', {'subject_id': alice})
    assert denied.status == 'error' and denied.error.code == 'ancestor_inactive'


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_public_topology_blocks_nodes_and_edges_in_both_directions(installed, reverse):
    app, _ = installed
    vk, viewer, _ = await register(app, 'root-block-viewer')
    ak, alice, _ = await register(app, 'root-block-alice')
    bk, bob, _ = await register(app, 'root-block-bob')
    for key, subject, target in ((ak, alice, bob), (bk, bob, alice)):
        await invoke(app, 'communication.follow', {'id': target}, key=key, subject=subject)
    assert edges(await topology(app))[alice, bob]['mutual'] is True
    key, subject, target = (bk, bob, alice) if reverse else (ak, alice, bob)
    await invoke(app, 'communication.dm_block', {'subject_id': target}, key=key, subject=subject)
    public_graph = await topology(app)
    assert {alice, bob} <= set(public_graph['nodes'])
    assert (alice, bob) not in edges(public_graph) and (bob, alice) not in edges(public_graph)
    key, subject, target = (bk, bob, viewer) if reverse else (vk, viewer, bob)
    await invoke(app, 'communication.dm_block', {'subject_id': target}, key=key, subject=subject)
    graph = await topology(app, key=vk, subject=viewer)
    assert bob not in graph['nodes'] and bob not in str(graph['edges'])
    assert await relation(app, bob) == 'default'
    # Represent either direction of an existing local-only Root block.
    async with app.metadata.transaction(write=True) as tx:
        tx.execute(
            'INSERT INTO dm_blocks VALUES (?,?)',
            (ROOT_SUBJECT, viewer) if reverse else (viewer, ROOT_SUBJECT),
            write=True,
        )
    graph = await topology(app, key=vk, subject=viewer, max_users=1, max_edges=1)
    assert ROOT_SUBJECT not in graph['nodes'] and ROOT_SUBJECT not in str(graph['edges'])
    outgoing = await invoke(
        app, 'communication.agent_following', {'subject_id': viewer}, key=vk, subject=viewer
    )
    assert not outgoing.data['items']
    assert ROOT_SUBJECT in (await topology(app))['nodes']
