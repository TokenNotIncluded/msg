"""Copying a retained reference must verify its payload before publication."""

import pytest
from test_service import call, register

from msg.core.codec import b64, wire
from msg.core.models import ResourceRef


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
async def test_binary_copy_rejects_unavailable_source_without_publication(installed, damage):
    app, _ = installed
    key, owner, _ = await register(app, 'copy-integrity')
    parent = '/@copy-integrity/files'
    created = await call(
        app,
        'content.file_put',
        {'parent': parent, 'name': 'source.bin', 'data': b64(b'original')},
        key=key,
        subject=owner,
    )
    assert created.status == 'ok', wire(created)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=created.resources[0].id))
        before = tuple(
            tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('resources', 'revisions', 'events')
        )
    path = app.contents.binary / revision.content.digest.removeprefix('sha256:')
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'tampered')
    copied = await call(
        app,
        'content.file_put',
        {'parent': parent, 'name': 'copy.bin', 'source': {'id': created.resources[0].id}},
        key=key,
        subject=owner,
    )
    assert copied.status == 'error', wire(copied)
    assert copied.error.code == (
        'content_missing' if damage == 'missing' else 'content_digest_mismatch'
    )
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tuple(
                tx.one('SELECT COUNT(*) FROM ' + table)[0]
                for table in ('resources', 'revisions', 'events')
            )
            == before
        )


@pytest.mark.asyncio
async def test_reupload_over_damaged_binary_is_not_published(installed):
    app, _ = installed
    key, owner, _ = await register(app, 'reupload-integrity')
    parent = '/@reupload-integrity/files'
    created = await call(
        app,
        'file.create',
        {'parent': parent, 'name': 'first.bin', 'data': b64(b'original')},
        key=key,
        subject=owner,
    )
    assert created.status == 'ok', wire(created)
    async with app.metadata.transaction(write=False) as tx:
        revision = await tx.revision(ResourceRef(id=created.resources[0].id))
        before = tuple(
            tx.one('SELECT COUNT(*) FROM ' + table)[0]
            for table in ('resources', 'revisions', 'events')
        )
    # Same size, different bytes: an existing content-addressed path is not
    # evidence that the payload still matches its digest.
    path = app.contents.binary / revision.content.digest.removeprefix('sha256:')
    path.write_bytes(b'tampered')
    again = await call(
        app,
        'file.create',
        {'parent': parent, 'name': 'second.bin', 'data': b64(b'original')},
        key=key,
        subject=owner,
    )
    assert again.status == 'error' and again.error.code == 'content_digest_mismatch', wire(again)
    async with app.metadata.transaction(write=False) as tx:
        assert (
            tuple(
                tx.one('SELECT COUNT(*) FROM ' + table)[0]
                for table in ('resources', 'revisions', 'events')
            )
            == before
        )
    path.write_bytes(b'original')
    repaired = await call(
        app,
        'file.create',
        {'parent': parent, 'name': 'second.bin', 'data': b64(b'original')},
        key=key,
        subject=owner,
    )
    assert repaired.status == 'ok', wire(repaired)
