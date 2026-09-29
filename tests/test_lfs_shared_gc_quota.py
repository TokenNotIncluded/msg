"""Shared binary CAS keeps ACL roots and bounds publishes across PostgreSQL workers."""

import hashlib
import os
import time
from datetime import timedelta

import httpx
import pytest
from test_service import NOW, call, register

from msg.core.codec import b64, canonical
from msg.core.requests import request_for
from msg.storage.git import LFSObjectStore
from msg.transports.http import create_app
from msg.workers.maintenance import _collect


@pytest.mark.asyncio
async def test_lfs_uses_shared_binary_inode_and_repo_acl_root(installed):
    app, _ = installed
    key, user, _ = await register(app, 'shared-lfs')
    one = await call(
        app, 'git.create', {'parent': '/@shared-lfs', 'name': 'one.git'}, key=key, subject=user
    )
    two = await call(
        app, 'git.create', {'parent': '/@shared-lfs', 'name': 'two.git'}, key=key, subject=user
    )
    assert one.status == two.status == 'ok'
    data = b'shared lfs bytes\n'
    oid = hashlib.sha256(data).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        for created in (one, two):
            rid = created.resources[0].id
            packet = request_for(
                'git.lfs_publish',
                {'id': rid, 'oid': oid, 'size': len(data)},
                app.settings.service_url,
                signer=key,
                subject=user,
                expires_at=NOW + timedelta(seconds=120),
            )
            sent = await http.put(
                f'/-/git/{rid}/info/lfs/objects/{oid}/{len(data)}',
                content=data,
                headers={'X-Msg-Request': b64(canonical(packet))},
            )
            assert sent.status_code == 200, sent.text
        read = one.data['read_url'].removeprefix(app.settings.service_url)
        assert (await http.get(read + '/info/lfs/objects/' + oid)).content == data
    canonical_path = app.settings.server.blob_dir / oid
    paths = [
        LFSObjectStore(
            app.settings.server.repositories_dir / (created.resources[0].id + '.git')
        ).path(oid)
        for created in (one, two)
    ]
    assert canonical_path.stat().st_ino == paths[0].stat().st_ino == paths[1].stat().st_ino
    assert canonical_path.stat().st_nlink == 3
    blob = await app.contents.put_bytes(data, 'application/octet-stream')
    assert blob.digest == 'sha256:' + oid
    index = app.contents.index / oid
    old = time.time() - 7200
    os.utime(index, (old, old))
    async with app.metadata.transaction(write=True) as tx:
        await _collect(app, tx, grace_seconds=0)
    assert not index.exists() and canonical_path.exists()
    store = LFSObjectStore(
        app.settings.server.repositories_dir / (one.resources[0].id + '.git'),
        app.settings.server.blob_dir,
    )
    assert store.collect_unlinked(older_than=time.time() + 1, index=app.contents.index) == (0, 0)
    paths[0].unlink()
    assert store.collect_unlinked(older_than=time.time() + 1, index=app.contents.index) == (0, 0)
    paths[1].unlink()
    assert store.collect_unlinked(older_than=time.time() + 1, index=app.contents.index) == (
        1,
        len(data),
    )
    assert not canonical_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('media_type', ('application/octet-stream', 'text/plain'))
async def test_content_gc_respects_explicit_pin_before_revision_commit(installed, media_type):
    app, _ = installed
    blob = await app.contents.put_bytes(b'pending pinned content', media_type)
    await app.contents.pin(blob, 'in-flight-job')
    index = app.contents.index / blob.digest[7:]
    old = time.time() - 7200
    os.utime(index, (old, old))
    async with app.metadata.transaction(write=True) as tx:
        result = await _collect(app, tx, grace_seconds=0)
    assert result['unreferenced_contents_removed'] == 0
    assert await app.contents.pinned(blob, 'in-flight-job')
    await app.contents.unpin(blob, 'in-flight-job')
    async with app.metadata.transaction(write=True) as tx:
        result = await _collect(app, tx, grace_seconds=0)
    assert result['unreferenced_contents_removed'] == 1
    assert not index.exists()


@pytest.mark.asyncio
async def test_lfs_capacity_and_shared_volume_id_are_deployment_wide(installed, monkeypatch):
    app, _ = installed
    key, user, _ = await register(app, 'quota-lfs')
    created = await call(
        app, 'git.create', {'parent': '/@quota-lfs', 'name': 'code.git'}, key=key, subject=user
    )
    rid = created.resources[0].id
    first = b'first object'
    second = b'second object'
    monkeypatch.setenv('MSG_LFS_DEPLOYMENT_MAX_BYTES', str(len(first)))

    async def put(http, body):
        oid = hashlib.sha256(body).hexdigest()
        packet = request_for(
            'git.lfs_publish',
            {'id': rid, 'oid': oid, 'size': len(body)},
            app.settings.service_url,
            signer=key,
            subject=user,
            expires_at=NOW + timedelta(seconds=120),
        )
        return await http.put(
            f'/-/git/{rid}/info/lfs/objects/{oid}/{len(body)}',
            content=body,
            headers={'X-Msg-Request': b64(canonical(packet))},
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url='http://testserver'
    ) as http:
        assert (await put(http, first)).status_code == 200
        full = await put(http, second)
        assert full.status_code == 400 and 'lfs_deployment_capacity_exceeded' in full.text
        assert not (app.settings.server.blob_dir / hashlib.sha256(second).hexdigest()).exists()
        # A worker with a different quota cannot silently expand the deployment.
        monkeypatch.setenv('MSG_LFS_DEPLOYMENT_MAX_BYTES', str(len(first) + len(second)))
        mismatch = await put(http, second)
        assert mismatch.status_code == 400 and 'lfs_deployment_capacity_mismatch' in mismatch.text
        monkeypatch.setenv('MSG_LFS_DEPLOYMENT_MAX_BYTES', str(len(first)))
        sentinel = app.settings.server.blob_dir / '.msg-shared-store-id'
        old = sentinel.read_bytes()
        sentinel.write_text('0' * 32 + '\n')
        try:
            other_volume = await put(http, second)
            assert (
                other_volume.status_code == 400
                and 'lfs_shared_volume_required' in other_volume.text
            )
        finally:
            sentinel.write_bytes(old)
