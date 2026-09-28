"""Authenticated upload failures must not leave permanent chunk/output pins."""
import asyncio

import pytest
from test_security_patch_regressions import fresh_call
from test_service import call, register

from msg.core.codec import b64, decode, digest, wire
from msg.core.errors import Failure
from msg.core.models import BlobRef


async def pins(app):
    binary = sorted(str(p.relative_to(app.contents.path))
                    for p in (app.contents.path / 'pins').glob('*/*') if p.is_file())
    git = await asyncio.to_thread(app.contents._run, 'for-each-ref',
                                  '--format=%(refname)', 'refs/pins')
    return set(binary) | set(git.splitlines())


@pytest.mark.parametrize('media_type', ['application/octet-stream', 'text/plain'])
@pytest.mark.parametrize('operation', ['transfer.part_put', 'transfer.seal'])
@pytest.mark.parametrize('failure', ['projection', 'partial_pin'])
async def test_failed_upload_removes_only_its_new_pins(installed, monkeypatch, operation, failure, media_type):
    app, _ = installed
    key, subject, _ = await register(app, 'rollback-transfer')
    # The sealed digest differs from either chunk, so no shared chunk can hide a leak.
    data = b'first chunk' + b'second chunk'
    opened = await call(app, 'transfer.open', {'direction': 'upload', 'size': len(data), 'media_type': media_type},
                        key=key, subject=subject)
    assert opened.status == 'ok', wire(opened)
    tid = opened.data['transfer_id']
    args = {'transfer_id': tid, 'offset': 0, 'data': b64(data), 'digest': digest(data)}
    lease = tid + ':0'
    if operation == 'transfer.seal':
        for offset, chunk in ((0, b'first chunk'), (11, b'second chunk')):
            part = await call(app, 'transfer.part_put', {'transfer_id': tid, 'offset': offset,
                              'data': b64(chunk), 'digest': digest(chunk)},
                              key=key, subject=subject)
            assert part.status == 'ok', wire(part)
        args = {'transfer_id': tid, 'final_size': len(data), 'final_digest': digest(data)}
        lease = tid + ':sealed'
    before = await pins(app)
    async with app.metadata.transaction(write=False) as tx:
        transfer_before = await tx.transfer(tid)
        counts_before = tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                              for table in ('resources', 'revisions'))
        chunks_before = tx.rows('SELECT offset,body FROM chunks WHERE transfer_id=? ORDER BY offset',
                                (tid,))
    original_pin = app.contents.pin

    async def partial_pin(blob, lease_id):
        await original_pin(blob, lease_id)
        if lease_id == lease:
            raise Failure('injected_pin_failure')

    with monkeypatch.context() as patch:
        if failure == 'partial_pin':
            patch.setattr(app.contents, 'pin', partial_pin)
        for n in range(2):
            result = await fresh_call(app, operation, args, key=key, subject=subject,
                                      rid=f'failed-upload-{n}',
                                      return_fields=('invalid_field',) if failure == 'projection' else ())
            projection_error = ('projection_unavailable' if operation == 'transfer.part_put'
                                else 'unknown_projection_field')
            error = projection_error if failure == 'projection' else 'injected_pin_failure'
            assert result.status == 'error' and result.error.code == error, wire(result)
            after = await pins(app)
            changes = {'extra_pins': after - before, 'lost_pins': before - after}
            assert not any(changes.values()), changes
            async with app.metadata.transaction(write=False) as tx:
                assert await tx.transfer(tid) == transfer_before
                assert tuple(tx.one(f'SELECT COUNT(*) FROM {table}')[0]
                             for table in ('resources', 'revisions')) == counts_before
                assert tx.rows('SELECT offset,body FROM chunks WHERE transfer_id=? ORDER BY offset',
                                (tid,)) == chunks_before
    accepted = await fresh_call(app, operation, args, key=key, subject=subject, rid='accepted-upload')
    assert accepted.status == 'ok', wire(accepted)
    if operation == 'transfer.part_put':
        blob = decode(BlobRef, accepted.data['chunk']['content'])
    else:
        async with app.metadata.transaction(write=False) as tx:
            blob = (await tx.revision(accepted.resources[0])).content
    assert await app.contents.pinned(blob, lease)
    for rid in ('accepted-upload', 'fresh-idempotent-upload'):
        replay = await fresh_call(app, operation, args, key=key, subject=subject, rid=rid)
        assert replay.status == 'ok', wire(replay)
        assert await app.contents.pinned(blob, lease)
    assert await app.contents.read_bytes(blob) == data
