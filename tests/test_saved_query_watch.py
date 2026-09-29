"""Pinned saved-query watches match only current authorized Event references."""
from datetime import timedelta

import pytest

from msg.core.codec import b64, canonical, digest, wire
from test_service import NOW, call, register


async def save_query(app, key, subject, arguments):
    payload = canonical({'version': 1, 'kind': 'read', 'arguments': arguments})
    opened = await call(app, 'transfer.open', {'direction': 'upload', 'size': len(payload),
        'digest': digest(payload), 'media_type': 'application/vnd.msg.read-query+json'}, key=key, subject=subject)
    assert opened.status == 'ok', wire(opened)
    tid = opened.data['transfer_id']
    part = await call(app, 'transfer.part_put', {'transfer_id': tid, 'offset': 0, 'data': b64(payload), 'digest': digest(payload)}, key=key, subject=subject)
    assert part.status == 'ok', wire(part)
    sealed = await call(app, 'transfer.seal', {'transfer_id': tid, 'final_size': len(payload), 'final_digest': digest(payload)}, key=key, subject=subject)
    assert sealed.status == 'ok', wire(sealed)
    token = await call(app, 'transfer.query_seal', {'transfer_id': tid}, key=key, subject=subject)
    assert token.status == 'ok', wire(token)
    saved = await call(app, 'query.save', {'query_ref': token.data['query_ref']}, key=key, subject=subject)
    assert saved.status == 'ok', wire(saved)
    return saved


async def messages(app, recipient):
    async with app.metadata.transaction(write=False) as tx:
        return [row[0] for row in tx.rows('SELECT resource FROM messages WHERE recipient=? ORDER BY resource', (recipient,))]


@pytest.mark.asyncio
async def test_saved_watch_matches_shared_read_predicates_and_source_revoke(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'saved-watch-author')
    rk, reader, _ = await register(app, 'saved-watch-reader')
    saved = await save_query(app, rk, reader, {'parent': '/main', 'type': 'post', 'author': author})
    ref = wire(saved.resources[0])
    args = {'query_ref': ref, 'event_types': ['content.post_create', 'content.post_edit'], 'delivery': 'inbox'}
    created = await call(app, 'communication.watch_create', args, key=rk, subject=reader, contract_version=2)
    assert created.status == 'ok', wire(created)
    chosen = await call(app, 'content.post_create', {'parent': '/main', 'body': 'match'}, key=ak, subject=author)
    skipped = await call(app, 'content.post_create', {'parent': '/main', 'body': 'different author'}, key=rk, subject=reader)
    assert chosen.status == skipped.status == 'ok'
    assert await messages(app, reader) == [chosen.resources[0].id]
    hidden = await call(app, 'content.chmod', {'id': chosen.resources[0].id, 'mode': '0600'},
                        key=ak, subject=author, expected=((chosen.resources[0].id, chosen.data['generation']),))
    assert hidden.status == 'ok', wire(hidden)
    edited = await call(app, 'content.post_edit', {'id': chosen.resources[0].id,
        'expected_revision': chosen.resources[0].revision, 'body': 'private change'},
        key=ak, subject=author, expected=((chosen.resources[0].id, hidden.data['generation']),))
    assert edited.status == 'ok', wire(edited)
    assert await messages(app, reader) == [chosen.resources[0].id]
    async with app.metadata.transaction(write=False) as tx:
        source = await tx.resource(ref['id'])
    archived = await call(app, 'query.saved_archive', {'ref': ref}, key=rk, subject=reader,
                          expected=((source.id, source.generation),))
    assert archived.status == 'ok', wire(archived)
    after = await call(app, 'content.post_create', {'parent': '/main', 'body': 'source revoked'}, key=ak, subject=author)
    assert after.status == 'ok', wire(after)
    assert await messages(app, reader) == [chosen.resources[0].id]


@pytest.mark.asyncio
async def test_saved_watch_rejects_unsupported_predicates_and_missing_revision(installed):
    app, _ = installed
    key, user, _ = await register(app, 'saved-watch-reject')
    saved = await save_query(app, key, user, {'parent': '/main', 'limit': 1})
    args = {'query_ref': wire(saved.resources[0]), 'event_types': ['content.post_create'], 'delivery': 'inbox'}
    rejected = await call(app, 'communication.watch_create', args, key=key, subject=user, contract_version=2)
    assert rejected.error.code == 'watch_query_unsupported', wire(rejected)
    args['query_ref'] = {'id': saved.resources[0].id}
    rejected = await call(app, 'communication.watch_create', args, key=key, subject=user, contract_version=2)
    assert rejected.error.code == 'watch_query_revision_required', wire(rejected)


@pytest.mark.asyncio
async def test_saved_watch_cli_signs_pinned_v2_and_expiry_stops_delivery(installed, tmp_path, monkeypatch, capsys):
    import httpx
    from msg import cli
    from msg.client import ClientState, MsgClient
    from msg.core.codec import loads
    from msg.transports.client import HTTPTransport
    from msg.transports.http import create_app
    app, _ = installed
    ak, author, _ = await register(app, 'saved-watch-cli-author')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url) as http:
        monkeypatch.setitem(cli.TRANSPORTS, 'http', lambda server: HTTPTransport(server, http=http))
        directory = tmp_path/'client'
        client = MsgClient(ClientState(directory, server=app.settings.service_url), HTTPTransport(app.settings.service_url, http=http), clock=lambda: NOW)
        assert (await client.register('saved-watch-cli')).status == 'ok'
        saved = await save_query(app, client.state.signer, client.state.subject, {'parent': '/main', 'type': 'post'})
        ref = saved.resources[0]
        monkeypatch.setattr(cli, 'MsgClient', lambda state, transport: MsgClient(state, transport, clock=lambda: NOW))
        args = cli.parser().parse_args(['--config-dir', str(directory), '--server', app.settings.service_url,
            'watch', 'create', '--query-ref', ref.id, '--query-revision', ref.revision,
            '--event', 'content.post_create', '--expires-at', wire(NOW+timedelta(seconds=1))])
        status = await cli.run(args)
        output = loads(capsys.readouterr().out.encode().strip())
        assert status == 0 and output['status'] == 'ok', output
        before = await call(app, 'content.post_create', {'parent': '/main', 'body': 'live'}, key=ak, subject=author)
        assert before.status == 'ok'
        assert await messages(app, client.state.subject) == [before.resources[0].id]
        app.clock = lambda: NOW+timedelta(seconds=2)
        after = await call(app, 'content.post_create', {'parent': '/main', 'body': 'expired'}, key=ak, subject=author)
        assert after.status == 'ok', wire(after)
        assert await messages(app, client.state.subject) == [before.resources[0].id]


@pytest.mark.asyncio
async def test_saved_watch_delivers_new_tag_match_without_query_execution(installed):
    app, _ = installed
    ak, author, _ = await register(app, 'saved-watch-tag-author')
    rk, reader, _ = await register(app, 'saved-watch-tag-reader')
    saved = await save_query(app, rk, reader, {'parent': '/main', 'tag': 'research'})
    watched = await call(app, 'communication.watch_create', {
        'query_ref': wire(saved.resources[0]), 'event_types': ['content.post_create', 'content.tags_set'],
        'delivery': 'inbox'}, key=rk, subject=reader, contract_version=2)
    assert watched.status == 'ok', wire(watched)
    post = await call(app, 'content.post_create', {'parent': '/main', 'body': 'untagged'}, key=ak, subject=author)
    assert post.status == 'ok'
    assert await messages(app, reader) == []
    tagged = await call(app, 'content.tags_set', {'id': post.resources[0].id, 'tags': ['research']},
                        key=ak, subject=author, expected=((post.resources[0].id, post.data['generation']),))
    assert tagged.status == 'ok', wire(tagged)
    assert await messages(app, reader) == [post.resources[0].id]


@pytest.mark.asyncio
async def test_saved_watch_stops_when_source_key_revoked_but_watch_key_remains_valid(installed):
    from msg.security.crypto import Ed25519Signer
    app, _ = installed
    ak, author, _ = await register(app, 'saved-watch-dual-author')
    first, reader, _ = await register(app, 'saved-watch-dual-reader')
    saved = await save_query(app, first, reader, {'parent': '/main', 'type': 'post'})
    second = Ed25519Signer.generate()
    public = b64(second.public_key)
    async with app.metadata.transaction(write=False) as tx:
        ceiling = wire((await tx.credential(first.key_id)).ceiling)
    proof = second.sign(canonical({'subject_id': reader, 'public_key': public}), purpose='key-add')
    added = await call(app, 'identity.key_add', {'public_key': public, 'possession_proof': wire(proof),
        'ceiling': ceiling}, key=first, subject=reader)
    assert added.status == 'ok', wire(added)
    watched = await call(app, 'communication.watch_create', {
        'query_ref': wire(saved.resources[0]), 'event_types': ['content.post_create'], 'delivery': 'inbox'},
        key=second, subject=reader, contract_version=2)
    assert watched.status == 'ok', wire(watched)
    revoked = await call(app, 'identity.key_revoke', {'key_id': first.key_id}, key=second, subject=reader)
    assert revoked.status == 'ok', wire(revoked)
    alive = await call(app, 'communication.watch_list', {}, key=second, subject=reader)
    assert alive.status == 'ok', wire(alive)
    posted = await call(app, 'content.post_create', {'parent': '/main', 'body': 'source key gone'}, key=ak, subject=author)
    assert posted.status == 'ok', wire(posted)
    assert await messages(app, reader) == []
