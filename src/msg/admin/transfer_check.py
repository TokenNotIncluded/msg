"""Bounded TransferSession checks in the disposable selftest installation."""
from msg.core.codec import b64, digest, unb64, wire
from msg.core.errors import require


async def check_transfer(app, call, register):
    require(bool(app.selftest_run_id), 'selftest_namespace_required')
    key, owner = await register('transfer-selftest')
    other_key, other = await register('transfer-outsider')

    async def invoke(operation, arguments):
        result = await call(operation, arguments, key, owner)
        require(result.status == 'ok', 'selftest_transfer_operation_failed',
                details={'operation': operation})
        return result

    async def snapshot():
        async with app.metadata.transaction(write=False) as tx:
            return {table: tuple(sorted(tx.rows(f'SELECT * FROM {table}'), key=repr))
                    for table in ('resources', 'revisions', 'events', 'audit', 'jobs',
                                  'results', 'transfers', 'chunks', 'settings', 'credentials')}

    payload = b'isolated transfer selftest\n' + bytes(range(256)) * 2
    opened = await invoke('transfer.open', {'direction': 'upload', 'size': len(payload),
        'digest': digest(payload), 'requested_part_bytes': 128})
    transfer = opened.data['transfer_id']
    part_bytes = opened.data['part_bytes']
    require(0 < part_bytes <= 128, 'selftest_transfer_part_size')
    # The wrong principal must not be able to write a valid chunk or read state.
    before = await snapshot()
    for operation, arguments in (
            ('transfer.status', {'transfer_id': transfer}),
            ('transfer.part_put', {'transfer_id': transfer, 'offset': 0,
                                  'data': b64(payload[:1]), 'digest': digest(payload[:1])})):
        denied = await call(operation, arguments, other_key, other)
        require(denied.error is not None and denied.error.code == 'transfer_owner_required',
                'selftest_transfer_owner_not_enforced')
    bad = await call('transfer.part_put', {'transfer_id': transfer, 'offset': 0,
        'data': b64(payload[:1]), 'digest': digest(b'wrong')}, key, owner)
    require(bad.error is not None and bad.error.code == 'chunk_digest_mismatch',
            'selftest_transfer_bad_digest_accepted')
    require(await snapshot() == before, 'selftest_transfer_rejection_mutated')
    for offset in reversed(range(0, len(payload), part_bytes)):
        block = payload[offset:offset + part_bytes]
        await invoke('transfer.part_put', {'transfer_id': transfer, 'offset': offset,
                                          'data': b64(block), 'digest': digest(block)})
    status = await invoke('transfer.status', {'transfer_id': transfer})
    require(not status.data['missing'], 'selftest_transfer_missing_parts')
    sealed = await invoke('transfer.seal', {'transfer_id': transfer,
        'final_size': len(payload), 'final_digest': digest(payload)})
    require(len(sealed.resources) == 1, 'selftest_transfer_output_missing')
    async with app.metadata.transaction(write=False) as tx:
        resource = await tx.resource(sealed.resources[0].id)
        require(resource.type == 'file' and resource.mode == 0o600 and resource.owner == owner,
                'selftest_transfer_output_not_private')
    downloaded = await invoke('transfer.open', {'direction': 'download',
        'target': wire(sealed.resources[0]), 'requested_part_bytes': 128})
    download = downloaded.data['transfer_id']
    download_part_bytes = downloaded.data['part_bytes']
    require(0 < download_part_bytes <= 128, 'selftest_transfer_part_size')
    before = await snapshot()
    received = bytearray()
    for offset in range(0, len(payload), download_part_bytes):
        part = await invoke('transfer.part_get', {'transfer_id': download, 'offset': offset,
                                                'length': min(download_part_bytes, len(payload) - offset)})
        block = unb64(part.data['data'])
        require(digest(block) == part.data['chunk']['content']['digest'],
                'selftest_transfer_chunk_digest_mismatch')
        received.extend(block)
    await invoke('transfer.status', {'transfer_id': transfer})
    await invoke('transfer.status', {'transfer_id': download})
    denied = await call('transfer.part_get', {'transfer_id': download, 'offset': 0, 'length': 1},
                        other_key, other)
    require(denied.error is not None and denied.error.code == 'transfer_owner_required',
            'selftest_transfer_download_owner_not_enforced')
    require(bytes(received) == payload and digest(bytes(received)) == digest(payload),
            'selftest_transfer_download_mismatch')
    require(await snapshot() == before, 'selftest_transfer_read_mutated')
    return True
