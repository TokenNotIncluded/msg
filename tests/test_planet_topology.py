"""Anonymous topology and true authored counts retain current ACL boundaries."""

from dataclasses import replace

import httpx
from test_service import call, register

from msg.constants import ROOT_SUBJECT
from msg.core.models import Scope
from msg.transports.http import create_app
from msg.transports.universe import subject_position


async def test_star_counts_current_authors_beyond_loaded_page_and_keeps_private_counts_own(
    installed,
):
    app, _ = installed
    key, author, _ = await register(app, 'planet-author')
    other_key, other, _ = await register(app, 'planet-owner')
    public_ids, private_ids = [], []
    for index in range(32):
        result = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': f'authored {index}'},
            key=key,
            subject=author,
        )
        assert result.status == 'ok', result.error
        (public_ids if index < 30 else private_ids).append(result.resources[0].id)
    foreign = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'other author'},
        key=other_key,
        subject=other,
    )
    assert foreign.status == 'ok', foreign.error
    async with app.metadata.transaction(write=True) as tx:
        moved = await tx.resource(public_ids[0])
        await tx.replace(
            replace(moved, owner=other, generation=moved.generation + 1), moved.generation
        )
        transferred = await tx.resource(foreign.resources[0].id)
        await tx.replace(
            replace(transferred, owner=author, generation=transferred.generation + 1),
            transferred.generation,
        )
        for rid in private_ids:
            resource = await tx.resource(rid)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
    public = await call(app, 'discovery.get', {'id': author, 'fields': ['star']})
    assert public.status == 'ok', public.error
    assert public.data['star']['post_count'] == {'public': 30, 'exact': True, 'scanned': 30}
    own = await call(
        app, 'discovery.get', {'id': author, 'fields': ['star_private']}, key=key, subject=author
    )
    assert own.status == 'ok', own.error
    assert own.data['star_private'] == {'own': 32, 'private_visible': 2, 'exact': True}
    for signer, subject in ((None, None), (other_key, other)):
        denied = await call(
            app,
            'discovery.get',
            {'id': author, 'fields': ['star_private']},
            key=signer,
            subject=subject,
        )
        assert denied.error.code == 'permission_denied'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = (await http.get('/_universe?kind=users')).json()
        star = next(item for item in page['items'] if item['id'] == author)
        assert star['star']['post_count']['public'] == 30
        assert 'private_visible' not in str(page) and "'own'" not in str(page)
        assert star['star']['layout']['position'] == await subject_position(app, author)
        first = (await http.get('/_universe', params={'kind': 'posts', 'author': author})).json()
        assert len(first['items']) == 24 and first['bounded']
        second = (
            await http.get(
                '/_universe', params={'kind': 'posts', 'author': author, 'cursor': first['cursor']}
            )
        ).json()
        assert 'items' in second, second
        posts = first['items'] + second['items']
        assert len(posts) == 30 and {item['id'] for item in posts} == set(public_ids)
        assert all(item['author']['id'] == author for item in posts)
    async with app.metadata.transaction(write=True) as tx:
        credential = await tx.credential(key.key_id)
        identity = await tx.subject(author)
        basic = next(grant for grant in credential.ceiling if grant.capability == 'resource.basic')
        narrow = tuple(
            replace(basic, scope=Scope(resource_id=rid), operations=frozenset({'discovery.get@1'}))
            for rid in (author, private_ids[0])
        )
        await tx.save_credential(replace(credential, ceiling=narrow), identity.auth_version)
    narrow_counts = await call(
        app, 'discovery.get', {'id': author, 'fields': ['star_private']}, key=key, subject=author
    )
    assert narrow_counts.status == 'ok', narrow_counts.error
    assert narrow_counts.data['star_private'] == {'own': 1, 'private_visible': 1, 'exact': True}


async def test_topology_optional_field_is_anonymous_and_rechecks_blocks_and_private_root(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'planet-visibility')
    other_key, other, _ = await register(app, 'planet-hidden')
    followed = await call(app, 'communication.follow', {'id': other}, key=key, subject=subject)
    assert followed.status == 'ok', followed.error
    graph = await call(app, 'discovery.get', {'id': subject, 'fields': ['star_topology']})
    assert graph.status == 'ok', graph.error
    assert any(
        edge['source'] == subject
        and edge['target'] == ROOT_SUBJECT
        and edge['source_type'] == 'default'
        for edge in graph.data['star_topology']['edges']
    )
    signed = await call(
        app, 'discovery.get', {'id': subject, 'fields': ['star_topology']}, key=key, subject=subject
    )
    assert signed.error.code == 'public_star_summary_only'
    blocked = await call(
        app, 'communication.dm_block', {'subject_id': subject}, key=other_key, subject=other
    )
    assert blocked.status == 'ok', blocked.error
    graph = await call(app, 'discovery.get', {'id': subject, 'fields': ['star_topology']})
    assert not any(
        {edge['source'], edge['target']} == {subject, other}
        for edge in graph.data['star_topology']['edges']
    )
    async with app.metadata.transaction(write=True) as tx:
        root = await tx.resource(ROOT_SUBJECT)
        hidden = await tx.resource(other)
        await tx.replace(replace(root, mode=0o600, generation=root.generation + 1), root.generation)
        await tx.replace(
            replace(hidden, mode=0o600, generation=hidden.generation + 1), hidden.generation
        )
    graph = await call(app, 'discovery.get', {'id': subject, 'fields': ['star_topology']})
    assert ROOT_SUBJECT not in str(graph.data['star_topology'])
    assert other not in str(graph.data['star_topology'])
    assert await subject_position(app, other) is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        page = await http.get('/_universe?kind=users')
        assert page.status_code == 200
        assert ROOT_SUBJECT not in page.text and other not in page.text
        public_page = page.json()
        assert subject in public_page['topology']['nodes']
        assert set(public_page['topology']['nodes']) == {
            item['id'] for item in public_page['items']
        }


async def test_public_author_count_is_unknown_when_scan_budget_is_incomplete(
    installed, monkeypatch
):
    app, _ = installed
    key, author, _ = await register(app, 'planet-bounded-count')
    for index in range(2):
        result = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': str(index)},
            key=key,
            subject=author,
        )
        assert result.status == 'ok', result.error
    monkeypatch.setattr('msg.plugins.star_projection.MAX_POST_COUNT_CANDIDATES', 1)
    result = await call(app, 'discovery.get', {'id': author, 'fields': ['star']})
    assert result.status == 'ok', result.error
    assert result.data['star']['post_count'] == {'public': None, 'exact': False, 'scanned': 1}


async def test_refresh_ids_shares_one_scan_and_intersects_current_read_checks(
    installed, monkeypatch
):
    from msg.plugins import star_projection

    app, _ = installed
    key, subject, _ = await register(app, 'planet-batch')
    other_key, other, _ = await register(app, 'planet-batch-hidden')
    for signer, author in ((key, subject), (other_key, other)):
        posted = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'body': 'batch count'},
            key=signer,
            subject=author,
        )
        assert posted.status == 'ok', posted.error
    signed = await call(
        app, 'discovery.get', {'id': subject, 'fields': ['star_batch']}, key=key, subject=subject
    )
    assert signed.error.code == 'public_star_summary_only'
    scans = {'posts': 0, 'layout': 0}
    original_counts, original_layout = star_projection.post_counts, star_projection.public_layout

    async def counts(app, ctx, request, tx):
        if ctx.principal.subject not in getattr(tx, '_planet_post_counts', {}):
            scans['posts'] += 1
        return await original_counts(app, ctx, request, tx)

    async def layout(app, ctx, request, tx):
        if getattr(tx, '_planet_public_layout', None) is None:
            scans['layout'] += 1
        return await original_layout(app, ctx, request, tx)

    monkeypatch.setattr(star_projection, 'post_counts', counts)
    monkeypatch.setattr(star_projection, 'public_layout', layout)
    execute = app.executor.execute

    async def hide_after_batch(request, **kwargs):
        result = await execute(request, **kwargs)
        if 'star_batch' in request.arguments.get('fields', ()) and result.error is None:
            async with app.metadata.transaction(write=True) as tx:
                resource = await tx.resource(other)
                await tx.replace(
                    replace(resource, mode=0o600, generation=resource.generation + 1),
                    resource.generation,
                )
        return result

    monkeypatch.setattr(app.executor, 'execute', hide_after_batch)
    ids = ','.join([subject, other, *[f'u_absent_{index}' for index in range(98)]])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/_universe', params={'kind': 'users', 'ids': ids})
    assert response.status_code == 200, response.text
    assert scans == {'posts': 1, 'layout': 1}
    page = response.json()
    assert [item['id'] for item in page['items']] == [subject]
    assert page['items'][0]['star']['post_count']['public'] == 1
    assert page['items'][0]['star']['layout']['version'] == 4
    assert other not in response.text
