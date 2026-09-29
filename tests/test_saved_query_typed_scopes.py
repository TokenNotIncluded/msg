"""Saved search descriptors preserve the typed scope and its live authority."""
import pytest
from test_saved_queries import make_query_ref
from test_service import call, register

from msg.core.codec import digest, wire


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['subject', 'org', 'resource_refs'])
async def test_signed_typed_search_save_read_and_current_revocation(installed, kind):
    app, _ = installed
    key, owner, _ = await register(app, 'typed-query-owner')
    reader_key, reader, _ = await register(app, 'typed-query-reader')
    if kind == 'subject':
        target = owner
        supplied = {'subject': '/@typed-query-owner'}
        normalized = {'subject': owner}
    elif kind == 'org':
        group = await call(app, 'group.create', {'name': 'typed-query-org'}, key=key, subject=owner)
        assert group.status == 'ok', group.error
        target = group.resources[0].id
        async with app.metadata.transaction(write=False) as tx:
            supplied = {'org': await tx.path(target)}
        normalized = {'org': target}
    else:
        topic = await call(app, 'content.topic_create', {'parent': '/main', 'name': 'typed-query-topic'},
                           key=key, subject=owner)
        assert topic.status == 'ok', topic.error
        target = topic.resources[0].id
        supplied = {'resource_refs': [{'id': '/main/typed-query-topic'}, {'id': '/main'}]}
        normalized = {'resource_refs': [{'id': item} for item in sorted([target, 't_main'])]}
    args = {'scope': supplied, 'terms': 'typed-query-test'}
    direct = await call(app, 'discovery.lexical_search', args, key=reader_key, subject=reader,
                        contract_version=5)
    assert direct.status == 'ok', direct.error
    token = await make_query_ref(app, reader_key, reader, args, kind='search')
    saved = await call(app, 'query.save', {'query_ref': token}, key=reader_key, subject=reader)
    assert saved.status == 'ok', saved.error
    ref = wire(saved.resources[0])
    got = await call(app, 'query.saved_get', {'ref': ref}, key=reader_key, subject=reader)
    assert got.status == 'ok', got.error
    expected = {'operation': 'discovery.lexical_search', 'contract_version': 5,
                'arguments': {**args, 'scope': normalized}}
    assert wire(got.data) == {**expected, 'digest': digest(expected)}
    assert reader_key.key_id not in str(got.data)
    assert 'principal' not in got.data
    assert saved.data['descriptor_digest'] == digest(expected)
    raw = await call(app, 'discovery.get', {'id': ref['id']}, key=reader_key, subject=reader)
    assert raw.status == 'error'
    # Saving a legal search does not silently broaden the ReadQuery-only watch.
    watch = await call(app, 'communication.watch_create',
        {'query_ref': ref, 'event_types': ['content.post_create'], 'delivery': 'inbox'},
        key=reader_key, subject=reader, contract_version=2)
    assert watch.error.code == 'watch_query_unsupported'
    async with app.metadata.transaction(write=False) as tx:
        generation = (await tx.resource(target)).generation
    revoked = await call(app, 'content.chmod', {'id': target, 'mode': '0700'},
                          key=key, subject=owner, expected=((target, generation),))
    assert revoked.status == 'ok', revoked.error
    denied = await call(app, 'query.saved_get', {'ref': ref}, key=reader_key, subject=reader)
    assert denied.status == 'error'
    assert 'principal' not in str(wire(denied))
    # A new save must recheck every typed member; no silently narrowed union.
    resave = await call(app, 'query.save', {'query_ref': token}, key=reader_key, subject=reader)
    assert resave.status == 'error'
