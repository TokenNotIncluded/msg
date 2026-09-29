"""Old paths locate stable resources only through current read authorization."""

from datetime import timedelta

import httpx
from test_route_effect_matrix import database_snapshot
from test_service import NOW, call, register

from msg.core.codec import b64, canonical, wire
from msg.core.requests import request_for
from msg.transports.http import create_app


def signed(app, key, subject, rid, view=None, revision=None):
    args = {'id': rid}
    if view in {'meta', 'history'}:
        args['view'] = view
    if revision:
        args['revision'] = revision
    packet = request_for(
        'discovery.raw' if view == 'raw' else 'discovery.get',
        args,
        app.settings.service_url,
        signer=key,
        subject=subject,
        expires_at=NOW + timedelta(seconds=90),
    )
    return {'x-msg-request': b64(canonical(packet))}


async def test_moved_paths_preserve_views_and_current_authority(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'path-migration')
    made = await call(
        app,
        'file.create',
        {
            'parent': '/main',
            'name': 'old.txt',
            'data': b64(b'private payload'),
            'media_type': 'text/plain',
        },
        key=key,
        subject=owner,
    )
    rid = made.resources[0].id
    moved = await call(
        app,
        'file.move',
        {'id': rid, 'parent': '/main', 'name': 'hidden-new.txt'},
        key=key,
        subject=owner,
        expected=((rid, made.data['generation']),),
    )
    assert moved.status == 'ok', wire(moved)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)),
        base_url='http://testserver',
        follow_redirects=False,
    ) as http:
        for suffix in (
            '',
            '/json',
            '/meta',
            '/history',
            '/raw',
            '/revisions/' + made.resources[0].revision + '/raw',
        ):
            response = await http.get('/main/old.txt' + suffix)
            assert response.status_code == 308, response.text
            assert response.headers['location'] == '/main/hidden-new.txt' + suffix
            assert response.headers['cache-control'] == 'no-store'
        private = await call(
            app,
            'content.chmod',
            {'id': rid, 'mode': '0600'},
            key=key,
            subject=owner,
            expected=((rid, moved.data['generation']),),
        )
        assert private.status == 'ok'
        before = await database_snapshot(app)
        for method in ('GET', 'HEAD'):
            for view in ('json', 'meta', 'history', 'raw'):
                response = await http.request(
                    method,
                    '/main/old.txt/' + view,
                    headers={'if-none-match': '"warm"', 'range': 'bytes=0-2'},
                )
                assert response.status_code == 403, response.text
                assert 'location' not in response.headers and 'etag' not in response.headers
                assert 'hidden-new' not in response.text and 'private payload' not in response.text
                allowed = await http.request(
                    method, '/main/old.txt/' + view, headers=signed(app, key, owner, rid, view)
                )
                assert allowed.status_code == 308, allowed.text
                assert allowed.headers['location'] == '/main/hidden-new.txt/' + view
        old_signed = await http.get(
            '/main/old.txt/json', headers=signed(app, key, owner, '/main/old.txt', 'json')
        )
        assert old_signed.status_code == 308, old_signed.text
        assert await database_snapshot(app) == before
        denied_write = await call(
            app,
            'file.write',
            {
                'id': '/main/old.txt',
                'base_revision': made.resources[0].revision,
                'data': b64(b'wrong'),
            },
            key=key,
            subject=owner,
            expected=((rid, private.data['generation']),),
        )
        assert denied_write.status == 'error' and denied_write.error.code == 'not_found', wire(
            denied_write
        )


async def test_parent_and_child_moves_and_path_reuse(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'path-tree')
    directory = await call(
        app, 'file.mkdir', {'parent': '/main', 'name': 'old-dir'}, key=key, subject=owner
    )
    did = directory.resources[0].id
    made = await call(
        app,
        'file.create',
        {'parent': did, 'name': 'leaf.txt', 'data': b64(b'original')},
        key=key,
        subject=owner,
    )
    rid = made.resources[0].id
    renamed = await call(
        app,
        'file.move',
        {'id': did, 'parent': '/main', 'name': 'new-dir'},
        key=key,
        subject=owner,
        expected=((did, directory.data['generation']),),
    )
    assert renamed.status == 'ok', wire(renamed)
    moved = await call(
        app,
        'file.move',
        {'id': rid, 'parent': did, 'name': 'moved.txt'},
        key=key,
        subject=owner,
        expected=((rid, made.data['generation']),),
    )
    assert moved.status == 'ok', wire(moved)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        response = await http.get('/main/old-dir/leaf.txt/raw')
        assert response.status_code == 308, response.text
        assert response.headers['location'] == '/main/new-dir/moved.txt/raw'
        fresh = await call(
            app, 'file.mkdir', {'parent': '/main', 'name': 'old-dir'}, key=key, subject=owner
        )
        assert fresh.status == 'ok', wire(fresh)
        # A recreated current namespace shadows its historic namespace completely.
        assert (await http.get('/main/old-dir/leaf.txt/raw')).status_code == 404
        assert (await http.get('/main/new-dir/leaf.txt/raw')).status_code == 308


async def test_migrated_extensionless_post_and_failed_move_rollback(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'migration-atomic')
    first = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'former', 'body': 'retained'},
        key=key,
        subject=owner,
    )
    second = await call(
        app,
        'content.post_create',
        {'parent': '/main', 'name': 'occupied', 'body': 'other'},
        key=key,
        subject=owner,
    )
    rid = first.resources[0].id
    assert second.status == 'ok'
    failed = await call(
        app,
        'file.move',
        {'id': rid, 'parent': '/main', 'name': 'occupied.md'},
        key=key,
        subject=owner,
        expected=((rid, first.data['generation']),),
    )
    assert failed.status == 'error', wire(failed)
    async with app.metadata.transaction(write=False) as tx:
        assert tx.one('SELECT 1 FROM resource_path_aliases WHERE resource_id=?', (rid,)) is None
        assert await tx.path(rid) == '/main/former.md'
    moved = await call(
        app,
        'file.move',
        {'id': rid, 'parent': '/main', 'name': 'latest.md'},
        key=key,
        subject=owner,
        expected=((rid, first.data['generation']),),
    )
    assert moved.status == 'ok', wire(moved)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get('/main/former')
        assert response.status_code == 308, response.text
        assert response.headers['location'] == '/main/latest.md'


async def test_alias_reuse_between_route_resolution_and_signed_read_never_leaks_location(
    installed, monkeypatch
):
    app, _ = installed
    key, owner, _ = await register(app, 'alias-race-owner')
    other, reader, _ = await register(app, 'alias-race-reader')
    made = await call(
        app,
        'file.create',
        {'parent': '/main', 'name': 'old-race.txt', 'data': b64(b'secret')},
        key=key,
        subject=owner,
    )
    rid = made.resources[0].id
    moved = await call(
        app,
        'file.move',
        {'id': rid, 'parent': '/main', 'name': 'private-target-name.txt'},
        key=key,
        subject=owner,
        expected=((rid, made.data['generation']),),
    )
    private = await call(
        app,
        'content.chmod',
        {'id': rid, 'mode': '0600'},
        key=key,
        subject=owner,
        expected=((rid, moved.data['generation']),),
    )
    assert private.status == 'ok'
    execute = app.executor.execute
    replaced = False

    async def concurrent_reuse(packet, **kwargs):
        nonlocal replaced
        if (
            not replaced
            and packet.operation == 'discovery.get'
            and packet.arguments.get('id') == '/main/old-race.txt'
        ):
            replaced = True
            new = await call(
                app,
                'file.create',
                {'parent': '/main', 'name': 'old-race.txt', 'data': b64(b'public replacement')},
                key=key,
                subject=owner,
            )
            assert new.status == 'ok', wire(new)
        return await execute(packet, **kwargs)

    monkeypatch.setattr(app.executor, 'execute', concurrent_reuse)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get(
            '/main/old-race.txt/json',
            headers=signed(app, other, reader, '/main/old-race.txt', 'json'),
        )
        assert response.status_code == 400, response.text
        assert response.json()['error']['code'] == 'resource_mismatch'
        assert 'location' not in response.headers and 'private-target-name' not in response.text


async def test_migration_location_preserves_subject_namespace_sigil(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'path-sigil')
    parent = '/@path-sigil/files'
    created = await call(
        app,
        'file.create',
        {'parent': parent, 'name': 'old.txt', 'data': b64(b'bytes')},
        key=key,
        subject=owner,
    )
    rid = created.resources[0].id
    moved = await call(
        app,
        'file.move',
        {'id': rid, 'parent': parent, 'name': 'new.txt'},
        key=key,
        subject=owner,
        expected=((rid, created.data['generation']),),
    )
    assert moved.status == 'ok', wire(moved)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url=app.settings.service_url
    ) as http:
        response = await http.get(
            parent + '/old.txt/raw', headers=signed(app, key, owner, rid, 'raw')
        )
        assert response.status_code == 308, response.text
        assert response.headers['location'] == parent + '/new.txt/raw'
        followed = await http.get(
            response.headers['location'], headers=signed(app, key, owner, rid, 'raw')
        )
        assert followed.status_code == 200 and followed.content == b'bytes'
