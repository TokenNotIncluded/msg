"""SearchQuery v3 filters inspect only currently readable current revisions."""
from datetime import timedelta

import httpx
import pytest

from msg.core.codec import b64,canonical,digest,wire
from msg.core.requests import request_for
from msg.transports.http import create_app
from test_service import NOW,call,register


@pytest.mark.asyncio
@pytest.mark.parametrize('version', [3, 5])
async def test_v3_query_string_path_and_sealed_ref_share_filters(installed, version):
    app,_=installed
    key,user,_=await register(app,'search-v3-http')
    root=await call(app,'content.post_create',{'parent':'/main','body':'v3needle root'},
                    key=key,subject=user)
    reply=await call(app,'discussion.reply',{'target':{'id':root.resources[0].id},
                    'body':'v3needle reply'},key=key,subject=user)
    assert root.status==reply.status=='ok'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        query=await http.get('/_search?scope=%2Fmain&terms=v3needle&relation_type=reply_to')
        path=await http.get('/_s/q/3/s/%2Fmain/t/v3needle/rt/reply_to')
        assert query.status_code==path.status_code==200,(query.text,path.text)
        assert query.content==path.content
        assert [row['id'] for row in query.json()['items']]==[reply.resources[0].id]
    descriptor_args={'scope':'/main','terms':'v3needle','relation_type':'reply_to'}
    if version==5:
        descriptor_args['revision']=reply.resources[0].revision
    descriptor=canonical({'version':1,'kind':'search','arguments':descriptor_args})
    opened=await call(app,'transfer.open',{'direction':'upload','size':len(descriptor),
        'digest':digest(descriptor),'media_type':'application/vnd.msg.read-query+json'},
        key=key,subject=user)
    tid=opened.data['transfer_id']
    await call(app,'transfer.part_put',{'transfer_id':tid,'offset':0,
        'data':b64(descriptor),'digest':digest(descriptor)},key=key,subject=user)
    await call(app,'transfer.seal',{'transfer_id':tid,'final_size':len(descriptor),
        'final_digest':digest(descriptor)},key=key,subject=user)
    sealed=await call(app,'transfer.query_seal',{'transfer_id':tid},key=key,subject=user)
    assert sealed.status=='ok',wire(sealed)
    token=sealed.data['query_ref']
    proof=request_for('transfer.query_get',{'query_ref':token},app.settings.service_url,
                      signer=key,subject=user,expires_at=NOW+timedelta(seconds=60))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        resolved=await http.get('/_r/q/'+token+'/p/'+b64(canonical(proof)))
        assert resolved.status_code==200,resolved.text
        assert [row['id'] for row in resolved.json()['items']]==[reply.resources[0].id]


@pytest.mark.asyncio
async def test_source_and_relation_filters_are_v3_and_use_current_revision(installed):
    app, _ = installed
    key, user, _ = await register(app, 'search-relation-owner')
    root = await call(app, 'content.post_create',
                      {'parent': '/main', 'body': 'relationfilterneedle root'},
                      key=key, subject=user)
    reply = await call(app, 'discussion.reply',
                       {'target': {'id': root.resources[0].id},
                        'body': 'relationfilterneedle reply'}, key=key, subject=user)
    quoted = await call(app, 'discussion.quote',
                        {'target': {'id': root.resources[0].id}, 'parent': '/main',
                         'body': 'relationfilterneedle quote'}, key=key, subject=user)
    assert root.status == reply.status == quoted.status == 'ok'
    base = {'scope': '/main', 'terms': 'relationfilterneedle', 'field': 'body'}
    reply_only = await call(app, 'discovery.lexical_search',
                            {**base, 'relation_type': 'reply_to'}, contract_version=3)
    assert reply_only.status == 'ok', wire(reply_only)
    assert [row['id'] for row in reply_only.data['items']] == [reply.resources[0].id]
    quote_only = await call(app, 'discovery.lexical_search',
                            {**base, 'relation_type': 'quote'}, contract_version=3)
    assert [row['id'] for row in quote_only.data['items']] == [quoted.resources[0].id]
    no_release = await call(app, 'discovery.lexical_search',
                            {**base, 'source_kind': 'release'}, contract_version=3)
    assert no_release.status == 'ok' and list(no_release.data['items']) == []
    released = await call(app, 'discovery.lexical_search',
                           {'scope': '/_rules', 'terms': 'identity', 'field': 'body',
                            'source_kind': 'release'}, contract_version=3)
    assert released.status == 'ok', wire(released)
    assert released.data['items']
    assert all(row['path'].startswith('/_rules/') for row in released.data['items'])
    old_contract = await call(app, 'discovery.lexical_search',
                              {**base, 'relation_type': 'reply_to'}, contract_version=2)
    assert old_contract.status == 'error'


@pytest.mark.asyncio
async def test_v3_old_cursor_rechecks_grants_before_items_facets_and_rank(installed):
    app, _ = installed
    key, user, _ = await register(app, 'search-revoke-owner')
    root = await call(app, 'content.post_create',
                      {'parent': '/main', 'body': 'revoke-filter-root'},
                      key=key, subject=user)
    replies = []
    for _ in range(3):
        reply = await call(app, 'discussion.reply',
                           {'target': {'id': root.resources[0].id},
                            'body': 'revokerelationneedle'}, key=key, subject=user)
        assert reply.status == 'ok', wire(reply)
        replies.append(reply)
    args = {'scope': '/main', 'terms': 'revokerelationneedle', 'field': 'body',
            'relation_type': 'reply_to', 'limit': 1, 'facets': ['type']}
    first = await call(app, 'discovery.lexical_search', args, contract_version=3)
    assert first.status == 'ok', wire(first)
    assert wire(first)['data']['facets'] == {'type': [{'value': 'post', 'count': 3}]}
    assert first.data['cursor']
    downgraded = await call(app, 'discovery.lexical_search',
                            {'cursor': first.data['cursor']}, contract_version=2)
    assert downgraded.status == 'error' and downgraded.error.code == 'cursor_query_mismatch'
    visible_first = first.data['items'][0]['id']
    for reply in replies:
        rid = reply.resources[0].id
        if rid != visible_first:
            locked = await call(app, 'content.chmod', {'id': rid, 'mode': '0600'},
                                key=key, subject=user,
                                expected=((rid, reply.data['generation']),))
            assert locked.status == 'ok', wire(locked)
    continued = await call(app, 'discovery.lexical_search',
                           {'cursor': first.data['cursor']}, contract_version=3)
    assert continued.status == 'ok', wire(continued)
    assert list(continued.data['items']) == []
    assert wire(continued)['data']['facets'] == {'type': [{'value': 'post', 'count': 1}]}
    fresh = await call(app, 'discovery.lexical_search', args, contract_version=3)
    assert [row['id'] for row in fresh.data['items']] == [visible_first]


@pytest.mark.asyncio
async def test_v5_revision_and_source_version_filters_use_current_readable_revision(installed):
    app, _ = installed
    key, subject, _ = await register(app, 'search-v5-owner')
    created = await call(app, 'content.post_create', {'parent': '/main', 'body': 'v5revisionneedle'},
                         key=key, subject=subject)
    assert created.status == 'ok', wire(created)
    rid, revision = created.resources[0].id, created.resources[0].revision
    args = {'scope': '/main', 'terms': 'v5revisionneedle', 'revision': revision}
    found = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert found.status == 'ok', wire(found)
    assert [item['id'] for item in found.data['items']] == [rid]
    old = await call(app, 'discovery.lexical_search', args, contract_version=4)
    assert old.status == 'error'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
        query = await http.get('/_search', params=args)
        path = await http.get('/_s/q/5/s/%2Fmain/t/v5revisionneedle/rv/' + revision)
        assert query.status_code == path.status_code == 200, (query.text, path.text)
        assert query.content == path.content
    edited = await call(app, 'content.post_edit', {'id': rid, 'body': 'v5revisionneedle updated',
        'expected_revision': revision}, key=key, subject=subject,
        expected=((rid, created.data['generation']),))
    assert edited.status == 'ok', wire(edited)
    gone = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert gone.status == 'ok' and not gone.data['items']
    release = await call(app, 'discovery.lexical_search',
                         {'scope': '/_rules', 'terms': 'identity', 'source_kind': 'release'},
                         contract_version=3)
    ref = release.data['items'][0]['id']
    from msg.core.models import ResourceRef
    async with app.metadata.transaction(write=False) as tx:
        source = await tx.revision(ResourceRef(id=ref))
    filtered = await call(app, 'discovery.lexical_search', {
        'scope': '/_rules', 'terms': 'identity', 'revision': source.id,
        'source_kind': 'release', 'source_version': source.source_version}, contract_version=5)
    assert filtered.status == 'ok', wire(filtered)
    assert [item['id'] for item in filtered.data['items']] == [ref]
    missed = await call(app, 'discovery.lexical_search', {
        'scope': '/_rules', 'terms': 'identity', 'revision': source.id,
        'source_version': source.source_version + 1}, contract_version=5)
    assert missed.status == 'ok' and not missed.data['items']


@pytest.mark.asyncio
async def test_v5_source_version_cursor_preserves_contract_and_rejects_downgrade(installed):
    app, _ = installed
    first = await call(app, 'discovery.lexical_search', {
        'scope': '/_rules', 'terms': 'identity', 'source_kind': 'release',
        'source_version': 1, 'limit': 1}, contract_version=5)
    assert first.status == 'ok' and first.data['cursor'], wire(first)
    downgraded = await call(app, 'discovery.lexical_search',
        {'cursor': first.data['cursor']}, contract_version=4)
    assert downgraded.status == 'error' and downgraded.error.code == 'cursor_query_mismatch'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)),
                                 base_url=app.settings.service_url) as http:
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
    created = await call(app, 'content.post_create',
        {'parent': '/main', 'body': 'privatev5needle'}, key=key, subject=subject)
    rid = created.resources[0].id
    changed = await call(app, 'content.chmod', {'id': rid, 'mode': '0600'},
        key=key, subject=subject, expected=((rid, created.data['generation']),))
    assert changed.status == 'ok', wire(changed)
    args = {'scope': '/main', 'terms': 'privatev5needle',
            'revision': created.resources[0].revision, 'facets': ['type'], 'snippet': True}
    denied = await call(app, 'discovery.lexical_search', args, contract_version=5)
    assert denied.status == 'ok' and not denied.data['items']
    assert wire(denied.data['facets']) == {'type': []}
    allowed = await call(app, 'discovery.lexical_search', args, key=key, subject=subject, contract_version=5)
    assert allowed.status == 'ok' and [item['id'] for item in allowed.data['items']] == [rid]
