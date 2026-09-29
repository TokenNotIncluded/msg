"""SearchQuery v3 filters inspect only currently readable current revisions."""

from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, digest, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [3, 5])
async def test_v3_query_string_path_and_sealed_ref_share_filters(installed, version):
    app, _ = installed
    key, user, _ = await register(app, 'search-v3-http')
    root = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'v3needle root'},
        key=key,
        subject=user,
    )
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': root.resources[0].id}, 'body': 'v3needle reply'},
        key=key,
        subject=user,
    )
    assert root.status == reply.status == 'ok'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        query = await http.get('/_search?scope=%2Fmain&terms=v3needle&relation_type=reply_to')
        path = await http.get('/_s/q/3/s/%2Fmain/t/v3needle/rt/reply_to')
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
        assert [row['id'] for row in query.json()['items']] == [reply.resources[0].id]
    descriptor_args = {'scope': '/main', 'terms': 'v3needle', 'relation_type': 'reply_to'}
    if version == 5:
        descriptor_args['revision'] = reply.resources[0].revision
        descriptor_args['relation_to'] = root.resources[0].id
        descriptor_args['scope'] = {'resource_refs': [{'id': reply.resources[0].id}]}
    descriptor = canonical({'version': 1, 'kind': 'search', 'arguments': descriptor_args})
    opened = await call(
        app,
        'transfer.open',
        {
            'direction': 'upload',
            'size': len(descriptor),
            'digest': digest(descriptor),
            'media_type': 'application/vnd.msg.read-query+json',
        },
        key=key,
        subject=user,
    )
    tid = opened.data['transfer_id']
    await call(
        app,
        'transfer.part_put',
        {'transfer_id': tid, 'offset': 0, 'data': b64(descriptor), 'digest': digest(descriptor)},
        key=key,
        subject=user,
    )
    await call(
        app,
        'transfer.seal',
        {'transfer_id': tid, 'final_size': len(descriptor), 'final_digest': digest(descriptor)},
        key=key,
        subject=user,
    )
    sealed = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=user)
    assert sealed.status == 'ok', wire(sealed)
    token = sealed.data['query_ref']
    proof = request_for(
        'transfer.query_get',
        {'query_ref': token},
        app.settings.service_url,
        signer=key,
        subject=user,
        expires_at=NOW + timedelta(seconds=60),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        resolved = await http.get('/_r/q/' + token + '/p/' + b64(canonical(proof)))
        assert resolved.status_code == 200, resolved.text
        assert [row['id'] for row in resolved.json()['items']] == [reply.resources[0].id]


@pytest.mark.asyncio
async def test_source_and_relation_filters_are_v3_and_use_current_revision(installed):
    app, _ = installed
    key, user, _ = await register(app, 'search-relation-owner')
    root = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'relationfilterneedle root'},
        key=key,
        subject=user,
    )
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': root.resources[0].id}, 'body': 'relationfilterneedle reply'},
        key=key,
        subject=user,
    )
    quoted = await call(
        app,
        'discussion.quote',
        {
            'target': {'id': root.resources[0].id},
            'parent': '/main',
            'body': 'relationfilterneedle quote',
        },
        key=key,
        subject=user,
    )
    assert root.status == reply.status == quoted.status == 'ok'
    base = {'scope': '/main', 'terms': 'relationfilterneedle', 'field': 'body'}
    reply_only = await call(
        app, 'discovery.lexical_search', {**base, 'relation_type': 'reply_to'}, contract_version=3
    )
    assert reply_only.status == 'ok', wire(reply_only)
    assert [row['id'] for row in reply_only.data['items']] == [reply.resources[0].id]
    quote_only = await call(
        app, 'discovery.lexical_search', {**base, 'relation_type': 'quote'}, contract_version=3
    )
    assert [row['id'] for row in quote_only.data['items']] == [quoted.resources[0].id]
    no_release = await call(
        app, 'discovery.lexical_search', {**base, 'source_kind': 'release'}, contract_version=3
    )
    assert no_release.status == 'ok' and list(no_release.data['items']) == []
    released = await call(
        app,
        'discovery.lexical_search',
        {'scope': '/_rules', 'terms': 'identity', 'field': 'body', 'source_kind': 'release'},
        contract_version=3,
    )
    assert released.status == 'ok', wire(released)
    assert released.data['items']
    assert all(row['path'].startswith('/_rules/') for row in released.data['items'])
    old_contract = await call(
        app, 'discovery.lexical_search', {**base, 'relation_type': 'reply_to'}, contract_version=2
    )
    assert old_contract.status == 'error'


@pytest.mark.asyncio
async def test_v3_old_cursor_rechecks_grants_before_items_facets_and_rank(installed):
    app, _ = installed
    key, user, _ = await register(app, 'search-revoke-owner')
    root = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'revoke-filter-root'},
        key=key,
        subject=user,
    )
    replies = []
    for _ in range(3):
        reply = await call(
            app,
            'discussion.reply',
            {'target': {'id': root.resources[0].id}, 'body': 'revokerelationneedle'},
            key=key,
            subject=user,
        )
        assert reply.status == 'ok', wire(reply)
        replies.append(reply)
    args = {
        'scope': '/main',
        'terms': 'revokerelationneedle',
        'field': 'body',
        'relation_type': 'reply_to',
        'limit': 1,
        'facets': ['type'],
    }
    first = await call(app, 'discovery.lexical_search', args, contract_version=3)
    assert first.status == 'ok', wire(first)
    assert wire(first)['data']['facets'] == {'type': [{'value': 'post', 'count': 3}]}
    assert first.data['cursor']
    downgraded = await call(
        app, 'discovery.lexical_search', {'cursor': first.data['cursor']}, contract_version=2
    )
    assert downgraded.status == 'error' and downgraded.error.code == 'cursor_query_mismatch'
    visible_first = first.data['items'][0]['id']
    for reply in replies:
        rid = reply.resources[0].id
        if rid != visible_first:
            locked = await call(
                app,
                'content.chmod',
                {'id': rid, 'mode': '0600'},
                key=key,
                subject=user,
                expected=((rid, reply.data['generation']),),
            )
            assert locked.status == 'ok', wire(locked)
    continued = await call(
        app, 'discovery.lexical_search', {'cursor': first.data['cursor']}, contract_version=3
    )
    assert continued.status == 'ok', wire(continued)
    assert list(continued.data['items']) == []
    assert wire(continued)['data']['facets'] == {'type': [{'value': 'post', 'count': 1}]}
    fresh = await call(app, 'discovery.lexical_search', args, contract_version=3)
    assert [row['id'] for row in fresh.data['items']] == [visible_first]


@pytest.mark.asyncio
async def test_v5_revision_and_source_version_filters_use_current_readable_revision(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'search-v5-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'v5revisionneedle'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok', wire(created)
    rid, revision = created.resources[0].id, created.resources[0].revision
    args = {'scope': '/main', 'terms': 'v5revisionneedle', 'revision': revision}
    found = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert found.status == 'ok', wire(found)
    assert [item['id'] for item in found.data['items']] == [rid]
    old = await call(app, 'discovery.lexical_search', args, contract_version=4)
    assert old.status == 'error'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        query = await http.get('/_search', params=args)
        path = await http.get('/_s/q/5/s/%2Fmain/t/v5revisionneedle/rv/' + revision)
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
    edited = await call(
        app,
        'content.post_edit',
        {'id': rid, 'body': 'v5revisionneedle updated', 'expected_revision': revision},
        key=key,
        subject=subject,
        expected=((rid, created.data['generation']),),
    )
    assert edited.status == 'ok', wire(edited)
    gone = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert gone.status == 'ok' and not gone.data['items']
    release = await call(
        app,
        'discovery.lexical_search',
        {'scope': '/_rules', 'terms': 'identity', 'source_kind': 'release'},
        contract_version=3,
    )
    ref = release.data['items'][0]['id']
    from msg.core.models import ResourceRef

    async with app.metadata.transaction(write=False) as tx:
        source = await tx.revision(ResourceRef(id=ref))
    filtered = await call(
        app,
        'discovery.lexical_search',
        {
            'scope': '/_rules',
            'terms': 'identity',
            'revision': source.id,
            'source_kind': 'release',
            'source_version': source.source_version,
        },
        contract_version=5,
    )
    assert filtered.status == 'ok', wire(filtered)
    assert [item['id'] for item in filtered.data['items']] == [ref]
    missed = await call(
        app,
        'discovery.lexical_search',
        {
            'scope': '/_rules',
            'terms': 'identity',
            'revision': source.id,
            'source_version': source.source_version + 1,
        },
        contract_version=5,
    )
    assert missed.status == 'ok' and not missed.data['items']


@pytest.mark.asyncio
async def test_v5_source_version_cursor_preserves_contract_and_rejects_downgrade(installed):
    app, _ = installed
    first = await call(
        app,
        'discovery.lexical_search',
        {
            'scope': '/_rules',
            'terms': 'identity',
            'source_kind': 'release',
            'source_version': 1,
            'limit': 1,
        },
        contract_version=5,
    )
    assert first.status == 'ok' and first.data['cursor'], wire(first)
    downgraded = await call(
        app, 'discovery.lexical_search', {'cursor': first.data['cursor']}, contract_version=4
    )
    assert downgraded.status == 'error' and downgraded.error.code == 'cursor_query_mismatch'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        next_page = await http.get(first.data['next'])
        assert next_page.status_code == 200, next_page.text
        query = await http.get('/_search?scope=%2F_rules&terms=identity&source_version=1')
        path = await http.get('/_s/q/5/s/%2F_rules/t/identity/sv/1')
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content


@pytest.mark.asyncio
async def test_v5_revision_filter_does_not_reveal_private_resource(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'private-revision-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'privatev5needle'},
        key=key,
        subject=subject,
    )
    rid = created.resources[0].id
    changed = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((rid, created.data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    args = {
        'scope': '/main',
        'terms': 'privatev5needle',
        'revision': created.resources[0].revision,
        'facets': ['type'],
        'snippet': True,
    }
    denied = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert denied.status == 'ok' and not denied.data['items']
    assert wire(denied.data['facets']) == {'type': []}
    allowed = await call(
        app, 'discovery.lexical_search', args, key=key, subject=subject, contract_version=5
    )
    assert allowed.status == 'ok' and [item['id'] for item in allowed.data['items']] == [rid]


@pytest.mark.asyncio
async def test_v5_directional_relations_and_presence_recheck_both_endpoints(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'directional-search-owner')
    root = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'directionneedle root'},
        key=key,
        subject=subject,
    )
    reply = await call(
        app,
        'discussion.reply',
        {'target': {'id': root.resources[0].id}, 'body': 'directionneedle reply'},
        key=key,
        subject=subject,
    )
    assert root.status == reply.status == 'ok'
    root_id, reply_id = root.resources[0].id, reply.resources[0].id
    base = {'scope': '/main', 'terms': 'directionneedle', 'facets': ['type']}

    async def find(extra, *, signed=False):
        result = await call(
            app,
            'discovery.lexical_search',
            {**base, **extra},
            contract_version=5,
            key=key if signed else None,
            subject=subject if signed else None,
        )
        assert result.status == 'ok', wire(result)
        return [item['id'] for item in result.data['items']]

    assert await find({'relation_to': root_id, 'relation_type': 'reply_to'}) == [reply_id]
    assert await find({'relation_from': reply_id, 'relation_type': 'reply_to'}) == [root_id]
    assert await find({'has_replies': True}) == [root_id]
    assert await find({'has_references': True}) == [root_id]
    assert await find({'has_replies': False}) == [reply_id]
    private = await call(
        app,
        'content.chmod',
        {'id': reply_id, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((reply_id, reply.data['generation']),),
    )
    assert private.status == 'ok', wire(private)
    assert await find({'relation_from': reply_id}) == []
    assert await find({'has_replies': True}) == []
    assert await find({'has_references': True}) == []
    assert await find({'has_replies': True}, signed=True) == [root_id]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        query = await http.get('/_search', params={**base, 'facets': 'type', 'has_replies': '1'})
        path = await http.get('/_s/q/5/s/%2Fmain/t/directionneedle/fc/type/hr/1')
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
    # Readable source cannot reveal its now-private target through a predicate.
    hidden = await call(
        app,
        'content.chmod',
        {'id': root_id, 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((root_id, root.data['generation']),),
    )
    assert hidden.status == 'ok', wire(hidden)
    public_reply = await call(
        app,
        'content.chmod',
        {'id': reply_id, 'mode': '0644'},
        key=key,
        subject=subject,
        expected=((reply_id, private.data['generation']),),
    )
    assert public_reply.status == 'ok', wire(public_reply)
    assert await find({'relation_to': root_id}) == []
    assert await find({'relation_to': root_id}, signed=True) == [reply_id]


@pytest.mark.asyncio
async def test_v5_title_is_explicit_resource_display_name_alias(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'title-search-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'NeedleTitle', 'body': 'unrelated content'},
        key=key,
        subject=subject,
    )
    assert created.status == 'ok', wire(created)
    args = {
        'scope': '/main',
        'terms': 'needletitle',
        'field': 'title',
        'fields': ['id', 'title', 'snippet'],
        'snippet': True,
    }
    found = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert found.status == 'ok', wire(found)
    assert wire(found.data['items']) == [
        {
            'id': created.resources[0].id,
            'title': 'NeedleTitle.md',
            'snippet': {'field': 'title', 'text': 'NeedleTitle.md', 'range': [0, 11]},
        }
    ]
    old = await call(app, 'discovery.lexical_search', args, contract_version=4)
    assert old.status == 'error'
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        query = await http.get(
            '/_search', params={**args, 'fields': 'id,title,snippet', 'snippet': '1'}
        )
        path = await http.get('/_s/q/5/s/%2Fmain/t/needletitle/f/t/fi/id,title,snippet/x/1')
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
        old_path = await http.get('/_s/q/4/s/%2Fmain/t/needletitle/f/t')
        assert old_path.status_code >= 400


@pytest.mark.asyncio
async def test_v5_typed_scopes_union_resources_and_subject_with_current_access(installed):
    from urllib.parse import quote

    app, _ = installed
    key, subject, _ = await register(app, 'scope-search-owner')
    posts = []
    for name in ('scope-one', 'scope-two', 'scope-outside'):
        post = await call(
            app,
            'content.post_create',
            {'parent': '/main', 'name': name, 'body': 'scopev5needle'},
            key=key,
            subject=subject,
        )
        assert post.status == 'ok', wire(post)
        posts.append(post)
    ids = [post.resources[0].id for post in posts]
    scope = {'resource_refs': [{'id': rid} for rid in ids[:2]]}
    args = {'scope': scope, 'terms': 'scopev5needle', 'limit': 1, 'order': 'name'}
    result = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert result.status == 'ok', wire(result)
    assert [item['id'] for item in result.data['items']] == [ids[0]]
    continued = await call(
        app, 'discovery.lexical_search', {'cursor': result.data['cursor']}, contract_version=5
    )
    assert continued.status == 'ok' and [item['id'] for item in continued.data['items']] == [ids[1]]
    owned = await call(
        app,
        'discovery.lexical_search',
        {**args, 'scope': {'subject': subject}, 'limit': 10},
        key=key,
        subject=subject,
        contract_version=5,
    )
    assert owned.status == 'ok' and {item['id'] for item in owned.data['items']} == set(ids), wire(
        owned
    )
    bad_type = await call(
        app, 'discovery.lexical_search', {**args, 'scope': {'org': subject}}, contract_version=5
    )
    assert bad_type.status == 'error' and bad_type.error.code == 'invalid_search_scope'
    group = await call(
        app, 'group.create', {'name': 'typed-search-group'}, key=key, subject=subject
    )
    assert group.status == 'ok', wire(group)
    group_id = group.resources[0].id
    grouped = await call(
        app,
        'content.chgrp',
        {'id': ids[0], 'group': group_id},
        key=key,
        subject=subject,
        expected=((ids[0], posts[0].data['generation']),),
    )
    assert grouped.status == 'ok', wire(grouped)
    group_search = await call(
        app,
        'discovery.lexical_search',
        {**args, 'scope': {'org': group_id}, 'limit': 10},
        key=key,
        subject=subject,
        contract_version=5,
    )
    assert group_search.status == 'ok' and [item['id'] for item in group_search.data['items']] == [
        ids[0]
    ], wire(group_search)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        query = await http.get(
            '/_search', params={'scope': canonical(scope).decode(), 'terms': 'scopev5needle'}
        )
        path = await http.get(
            '/_s/q/5/s/' + quote(canonical(scope).decode(), safe=',') + '/t/scopev5needle'
        )
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
    descriptor = canonical({'version': 1, 'kind': 'search', 'arguments': args})
    opened = await call(
        app,
        'transfer.open',
        {
            'direction': 'upload',
            'size': len(descriptor),
            'digest': digest(descriptor),
            'media_type': 'application/vnd.msg.read-query+json',
        },
        key=key,
        subject=subject,
    )
    tid = opened.data['transfer_id']
    await call(
        app,
        'transfer.part_put',
        {'transfer_id': tid, 'offset': 0, 'data': b64(descriptor), 'digest': digest(descriptor)},
        key=key,
        subject=subject,
    )
    await call(
        app,
        'transfer.seal',
        {'transfer_id': tid, 'final_size': len(descriptor), 'final_digest': digest(descriptor)},
        key=key,
        subject=subject,
    )
    sealed = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=subject)
    assert sealed.status == 'ok', wire(sealed)
    first = await call(
        app, 'transfer.query_get', {'query_ref': sealed.data['query_ref']}, key=key, subject=subject
    )
    assert first.status == 'ok' and first.data['cursor'], wire(first)
    second = await call(
        app,
        'transfer.query_get',
        {'query_ref': sealed.data['query_ref'], 'cursor': first.data['cursor']},
        key=key,
        subject=subject,
    )
    assert second.status == 'ok' and [item['id'] for item in second.data['items']] == [ids[1]], (
        wire(second)
    )
    changed = await call(
        app,
        'content.chmod',
        {'id': ids[1], 'mode': '0600'},
        key=key,
        subject=subject,
        expected=((ids[1], posts[1].data['generation']),),
    )
    assert changed.status == 'ok', wire(changed)
    revoked = await call(
        app, 'discovery.lexical_search', {'cursor': result.data['cursor']}, contract_version=5
    )
    assert revoked.status == 'error'
    old = await call(app, 'discovery.lexical_search', args, contract_version=4)
    assert old.status == 'error'
