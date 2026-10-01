from dataclasses import replace

import httpx
import pytest
from test_service import call, register

from msg.core.errors import Failure
from msg.core.identifiers import hex_id
from msg.core.models import BlobRef, ResourceRef, Revision
from msg.storage.sqlite import SqliteMetadataStore
from msg.transports.http import create_app


@pytest.mark.asyncio
async def test_sqlite_hex_references_preserve_legacy_ids_and_revision_scope(tmp_path):
    from test_foundation import item

    store = SqliteMetadataStore(tmp_path / 'references.db')
    try:
        async with store.transaction(write=True) as tx:
            await tx.insert(item('root'))
            for rid in ('9legacy_' + 'a' * 32, 'r_' + 'b' * 32):
                resource = item(rid, 'root')
                await tx.insert(resource)
                vid = '9revision_' + rid[-32:]
                await tx.append_revision(
                    Revision(
                        format_version=1,
                        id=vid,
                        resource_id=rid,
                        parents=(),
                        content=BlobRef(
                            digest='sha256:' + '0' * 64, size=0, media_type='text/plain'
                        ),
                        relations=(),
                        actor='owner',
                        subject='owner',
                        author='owner',
                        created_at=resource.created_at,
                        manifest_digest='',
                    )
                )
                assert (await tx.resource(hex_id(rid))).id == rid
                assert (
                    await tx.revision(ResourceRef(id=hex_id(rid), revision=hex_id(vid)))
                ).id == vid
            with pytest.raises(Failure, match='revision_not_found'):
                await tx.revision(
                    ResourceRef(
                        id=hex_id('r_' + 'b' * 32),
                        revision=hex_id('9revision_' + 'a' * 32),
                    )
                )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_hex_lookup_matches_legacy_conversion_and_rejects_collisions(installed):
    app, _ = installed
    key, uid, cert = await register(app, 'hex-legacy-owner')
    created = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'body': 'legacy content'},
        key=key,
        subject=uid,
        certs=(cert,),
    )
    assert created.status == 'ok', created.error
    async with app.metadata.transaction(write=True) as tx:
        original = await tx.resource(created.resources[0].id)
        revision = await tx.revision(created.resources[0])
        # Only a letter-led identifier prefix uses its hexadecimal suffix.
        legacy_id = '9legacy_' + 'a' * 32
        legacy_revision = '9revision_' + 'b' * 32
        await tx.insert(replace(original, id=legacy_id, name='legacy.md', revision=legacy_revision))
        await tx.append_revision(
            replace(
                revision,
                id=legacy_revision,
                resource_id=legacy_id,
                parents=(),
            )
        )
        assert (await tx.resource(hex_id(legacy_id))).id == legacy_id
        assert (
            await tx.revision(
                ResourceRef(
                    id=hex_id(legacy_id),
                    revision=hex_id(legacy_revision),
                )
            )
        ).id == legacy_revision
        suffix = 'c' * 32
        for prefix in ('r', 'old'):
            await tx.insert(
                replace(
                    original,
                    id=prefix + '_' + suffix,
                    name=prefix + '.md',
                    revision=None,
                )
            )
        with pytest.raises(Failure, match='ambiguous_resource_id'):
            await tx.resource(suffix)
        for prefix in ('v', 'old'):
            await tx.append_revision(
                replace(
                    revision,
                    id=prefix + '_' + suffix,
                    resource_id=legacy_id,
                    parents=(),
                )
            )
        with pytest.raises(Failure, match='ambiguous_revision_id'):
            await tx.revision(ResourceRef(id=legacy_id, revision=suffix))


@pytest.mark.asyncio
async def test_hex_references_survive_edits_and_expose_history_diff_and_visible_references(
    installed,
):
    app, _ = installed
    key, uid, cert = await register(app, 'hex-owner')

    async def invoke(op, args, **kw):
        result = await call(app, op, args, key=key, subject=uid, certs=(cert,), **kw)
        assert result.status == 'ok', result.error
        return result

    created = await invoke(
        'content.post_create', {'parent': '/main', 'name': 'Original title', 'body': 'before\n'}
    )
    rid, revision = created.resources[0].id, created.resources[0].revision
    edited = await invoke(
        'content.post_edit',
        {'id': rid, 'expected_revision': revision, 'body': 'after\n'},
        expected=((rid, created.data['generation']),),
    )
    await invoke(
        'content.post_edit_metadata',
        {'id': rid, 'name': 'New title'},
        expected=((rid, edited.data['generation']),),
    )
    reply = await invoke('discussion.reply', {'target': {'id': rid}, 'body': 'visible reference'})
    nested = await invoke(
        'discussion.reply', {'target': {'id': reply.resources[0].id}, 'body': 'nested reply'}
    )
    hidden_reply = await invoke(
        'discussion.reply', {'target': {'id': rid}, 'body': 'secret reference'}
    )
    hidden_id = hidden_reply.resources[0].id
    async with app.metadata.transaction(write=True) as tx:
        hidden = await tx.resource(hidden_id)
        await tx.replace(
            replace(hidden, mode=0o600, generation=hidden.generation + 1), hidden.generation
        )
    short = '/*' + hex_id(rid)
    async with app.metadata.transaction(write=False) as tx:
        assert await tx.resolve(short) == rid
        assert await tx.resolve('/main' + short) == rid
        assert (await tx.resource(hex_id(uid))).id == uid
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        thread = await http.get('/*' + hex_id(nested.resources[0].id) + '/thread?limit=2')
        assert thread.status_code == 200, thread.text
        tree = thread.json()
        assert tree['root'] == hex_id(rid)
        assert tree['items'][0]['id'] == hex_id(rid)
        assert all('content' not in item and 'links' not in item for item in tree['items'])
        assert [item['id'] for item in tree['ancestors']] == [
            hex_id(reply.resources[0].id),
            hex_id(rid),
        ]
        rest = await http.get(tree['next'])
        assert rest.status_code == 200
        items = {item['id']: item for item in tree['items'] + rest.json()['items']}
        assert set(items) == {
            hex_id(rid),
            hex_id(reply.resources[0].id),
            hex_id(nested.resources[0].id),
        }
        assert items[hex_id(reply.resources[0].id)]['reply_to'] == hex_id(rid)
        assert items[hex_id(nested.resources[0].id)]['reply_to'] == hex_id(reply.resources[0].id)
        assert hex_id(hidden_id) not in thread.text + rest.text
        topic = await http.get('/*' + hex_id('t_main') + '/meta')
        assert topic.status_code == 200 and topic.json()['id'] == hex_id('t_main')
        for prefix in (short, '/main' + short):
            page = await http.get(prefix)
            assert page.status_code == 200, page.text
            assert (
                page.text.startswith('---\n')
                and 'title: "New title"' in page.text
                and 'after' in page.text
            )
            assert 'date: "2026-' in page.text and f'id: "{hex_id(rid)}"' in page.text
            assert 'revision: "' in page.text and '/diff)' in page.text
            assert '[self]' not in page.text and '/main/New' not in page.text
            assert (await http.head(prefix)).content == b''
            assert (
                await http.get(prefix, headers={'If-None-Match': page.headers['etag']})
            ).status_code == 304
            meta = await http.get(prefix + '/meta')
            assert meta.status_code == 200 and meta.json()['id'] == hex_id(rid)
            assert meta.json()['owner'] == hex_id(uid)
            assert meta.json()['path'] == short
            history = await http.get(prefix + '/history')
            assert len(history.json()['revisions']) == 2
            old = await http.get(prefix + '/rev/' + hex_id(revision))
            assert old.status_code == 200 and 'before' in old.text
            diff = await http.get(prefix + '/diff')
            assert diff.status_code == 200, diff.text
            assert '-before' in diff.json()['diff'] and '+after' in diff.json()['diff']
            explicit_diff = await http.get(
                prefix + '/diff/' + hex_id(revision) + '/' + hex_id(edited.resources[0].revision)
            )
            assert (
                explicit_diff.status_code == 200
                and explicit_diff.json()['diff'] == diff.json()['diff']
            )
            refs = await http.get(prefix + '/references')
            assert set(refs.json()['ids']) == {
                hex_id(reply.resources[0].id),
                hex_id(nested.resources[0].id),
            }
            assert (await http.post(prefix)).status_code == 405
        assert (await http.get('/wiki' + short)).status_code == 404
        assert (await http.get(short.replace('*', '%2A'))).status_code == 404
        # Existing signed IDs and title paths continue to work.
        assert (await http.get('/_id/' + rid)).status_code == 200
        assert (await http.get('/main/New%20title.md')).status_code == 200
        async with app.metadata.transaction(write=True) as tx:
            resource = await tx.resource(rid)
            await tx.replace(
                replace(resource, mode=0o600, generation=resource.generation + 1),
                resource.generation,
            )
        for suffix in (
            '',
            '/meta',
            '/history',
            '/diff',
            '/references',
            '/thread',
            '/rev/' + hex_id(revision),
            '/raw',
        ):
            for method in (http.get, http.head):
                response = await method(
                    short + suffix, headers={'If-None-Match': page.headers['etag']}
                )
                assert response.status_code == 403, (suffix, response.text)
                assert 'etag' not in response.headers
